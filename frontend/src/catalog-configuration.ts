import { appendOutboxCommand, localDb, type CanonicalRecord } from "./local-db";
import { timestampNanoseconds } from "./timestamp";

export type CatalogKind =
  | "store_section"
  | "recipe_tag"
  | "dietary_tag"
  | "unit_definition"
  | "organization_meal_role_preset";
export type CatalogOperation = "create" | "update" | "retire" | "restore";

function clockFields(
  entityType: CatalogKind,
  operation: CatalogOperation,
): string[] {
  if (operation === "create")
    return [...clockFields(entityType, "update"), "lifecycle"];
  if (operation === "retire" || operation === "restore") return ["lifecycle"];
  if (
    entityType === "store_section" ||
    entityType === "organization_meal_role_preset"
  )
    return entityType === "store_section"
      ? ["name", "position_key"]
      : ["built_in_translation_key", "custom_name", "position_key"];
  if (entityType === "unit_definition") return ["custom_name"];
  return ["name", "color"];
}

function clocks(
  entityType: CatalogKind,
  operation: CatalogOperation,
  id: string,
  actionAt: string,
): Record<string, unknown> {
  return Object.fromEntries(
    clockFields(entityType, operation).map((field) => [
      field,
      { mutationId: id, actionAt },
    ]),
  );
}

function wins(actionAt: string, mutationId: string, current: unknown): boolean {
  if (!current || typeof current !== "object" || Array.isArray(current))
    return true;
  const clock = current as Record<string, unknown>;
  const currentAt =
    typeof clock.actionAt === "string"
      ? clock.actionAt
      : typeof clock.winning_client_wall_time === "string"
        ? clock.winning_client_wall_time
        : undefined;
  const currentId =
    typeof clock.mutationId === "string"
      ? clock.mutationId
      : typeof clock.winning_mutation_id === "string"
        ? clock.winning_mutation_id
        : undefined;
  if (!currentAt || !currentId) return true;
  const candidateTime = timestampNanoseconds(actionAt);
  const currentTime = timestampNanoseconds(currentAt);
  if (candidateTime === undefined || currentTime === undefined) return false;
  return (
    candidateTime > currentTime ||
    (candidateTime === currentTime && mutationId > currentId)
  );
}

const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const position = /^[0-9A-Za-z]{1,255}$/;
function validText(value: unknown): value is string {
  return (
    typeof value === "string" &&
    value.normalize("NFC").trim() === value &&
    value.length > 0 &&
    value.length <= 200 &&
    !value.includes("\0") &&
    !/[\uD800-\uDFFF]/.test(value)
  );
}

function fieldValues(
  entityType: CatalogKind,
  fields: Record<string, unknown>,
): Record<string, unknown> {
  if (entityType === "unit_definition")
    return {
      custom_name: fields.name,
      allows_ingredient_quantity: fields.allows_ingredient_quantity,
      allows_recipe_scaling: fields.allows_recipe_scaling,
    };
  if (entityType === "organization_meal_role_preset")
    return {
      ...fields,
      custom_name: fields.name ?? fields.custom_name ?? null,
      built_in_translation_key:
        fields.name !== undefined || fields.custom_name !== undefined
          ? null
          : fields.built_in_translation_key,
    };
  return fields;
}

export async function queueCatalogConfiguration(
  userId: string,
  organizationId: string,
  entityType: CatalogKind,
  operation: CatalogOperation,
  fields: Record<string, unknown>,
  entityId: string = crypto.randomUUID(),
) {
  const actionAt = new Date().toISOString();
  const id = crypto.randomUUID();
  if (!uuid.test(entityId)) return;
  const retired = operation === "retire";
  const restored = operation === "restore";
  await localDb.transaction(
    "rw",
    localDb.canonicalRecords,
    localDb.optimisticOverlays,
    localDb.outbox,
    async () => {
      const current =
        (await localDb.optimisticOverlays.get([
          userId,
          organizationId,
          entityType,
          entityId,
        ])) ??
        (await localDb.canonicalRecords.get([
          userId,
          organizationId,
          entityType,
          entityId,
        ]));
      const record: CanonicalRecord = {
        userId,
        organizationId,
        entityType,
        entityId,
        recordSchemaVersion: 1,
        lifecycle: retired ? "retired" : "active",
        immutable: false,
        updatedAt: actionAt,
        fields: {
          ...current?.fields,
          id: entityId,
          organization_id: organizationId,
          ...fields,
          retired_at: retired ? actionAt : null,
        },
        fieldClocks: {
          ...current?.fieldClocks,
          ...clocks(entityType, operation, id, actionAt),
        },
      };
      if (
        entityType === "organization_meal_role_preset" &&
        !retired &&
        !restored
      ) {
        Object.assign(record.fields, fieldValues(entityType, fields));
      }
      if (restored) record.fields.retired_at = null;
      await localDb.optimisticOverlays.put(record);
      await appendOutboxCommand({
        id,
        userId,
        organizationId,
        commandType: "catalog_configuration.mutate",
        payload: {
          entity_id: entityId,
          entity_kind: entityType,
          operation,
          ...fields,
        },
        actionAt,
        createdAt: actionAt,
        state: "pending",
      });
    },
  );
  return entityId;
}

export async function replayCatalogConfiguration(
  userId: string,
  organizationId: string,
  command: { id: string; actionAt: string; payload: Record<string, unknown> },
) {
  if (
    !uuid.test(command.id) ||
    timestampNanoseconds(command.actionAt) === undefined
  )
    return;
  const {
    entity_id: entityId,
    entity_kind: entityType,
    operation,
    ...fields
  } = command.payload;
  if (
    typeof entityId !== "string" ||
    !uuid.test(entityId) ||
    ![
      "store_section",
      "recipe_tag",
      "dietary_tag",
      "unit_definition",
      "organization_meal_role_preset",
    ].includes(String(entityType)) ||
    !["create", "update", "retire", "restore"].includes(String(operation))
  )
    return;
  const current =
    (await localDb.optimisticOverlays.get([
      userId,
      organizationId,
      entityType as CatalogKind,
      entityId,
    ])) ??
    (await localDb.canonicalRecords.get([
      userId,
      organizationId,
      entityType as CatalogKind,
      entityId,
    ]));
  const entityKind = entityType as CatalogKind;
  const catalogOperation = operation as CatalogOperation;
  const keys = Object.keys(fields).sort();
  const expected =
    catalogOperation === "retire" || catalogOperation === "restore"
      ? []
      : entityKind === "store_section"
        ? ["name", "position_key"]
        : entityKind === "recipe_tag" || entityKind === "dietary_tag"
          ? ["color", "name"]
          : entityKind === "unit_definition"
            ? catalogOperation === "create"
              ? ["allows_ingredient_quantity", "allows_recipe_scaling", "name"]
              : ["name"]
            : entityKind === "organization_meal_role_preset" &&
                fields.built_in_translation_key !== undefined
              ? ["built_in_translation_key", "position_key"]
              : ["name", "position_key"];
  if (keys.join("\0") !== expected.slice().sort().join("\0")) return;
  if (catalogOperation !== "retire" && catalogOperation !== "restore") {
    if ("name" in fields && !validText(fields.name)) return;
    if (
      "position_key" in fields &&
      (typeof fields.position_key !== "string" ||
        !position.test(fields.position_key))
    )
      return;
    if (
      "color" in fields &&
      (typeof fields.color !== "string" ||
        !/^#[0-9A-Fa-f]{6}$/.test(fields.color))
    )
      return;
    if (
      "built_in_translation_key" in fields &&
      (typeof fields.built_in_translation_key !== "string" ||
        !/^[a-z][a-z0-9_.-]*$/.test(fields.built_in_translation_key))
    )
      return;
    if (entityKind === "recipe_tag" && fields.color === undefined) return;
    if (
      entityKind === "unit_definition" &&
      catalogOperation === "create" &&
      (typeof fields.allows_ingredient_quantity !== "boolean" ||
        typeof fields.allows_recipe_scaling !== "boolean")
    )
      return;
    if (
      entityKind === "organization_meal_role_preset" &&
      "name" in fields === "built_in_translation_key" in fields
    )
      return;
  }
  const changedFields = fieldValues(entityKind, fields);
  const winning = clockFields(entityKind, catalogOperation).filter((field) =>
    wins(command.actionAt, command.id, current?.fieldClocks[field]),
  );
  if (!winning.length) return;
  const nextFields: Record<string, unknown> = {
    ...current?.fields,
    id: entityId,
    organization_id: organizationId,
  };
  for (const field of winning) {
    if (field === "lifecycle") {
      nextFields.retired_at =
        catalogOperation === "retire" ? command.actionAt : null;
    } else {
      nextFields[field] = changedFields[field];
    }
  }
  await localDb.optimisticOverlays.put({
    userId,
    organizationId,
    entityType: entityKind,
    entityId,
    recordSchemaVersion: 1,
    lifecycle: winning.includes("lifecycle")
      ? catalogOperation === "retire"
        ? "retired"
        : "active"
      : (current?.lifecycle ?? "active"),
    immutable: false,
    fields: nextFields,
    fieldClocks: {
      ...current?.fieldClocks,
      ...Object.fromEntries(
        winning.map((field) => [
          field,
          { mutationId: command.id, actionAt: command.actionAt },
        ]),
      ),
    },
    updatedAt: command.actionAt,
  });
}
