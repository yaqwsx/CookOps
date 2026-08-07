import { appendOutboxCommand, localDb, type CanonicalRecord } from "./local-db";

export type CatalogKind =
  | "store_section"
  | "recipe_tag"
  | "dietary_tag"
  | "unit_definition";
export type CatalogOperation = "create" | "update" | "retire" | "restore";

function clockFields(
  entityType: CatalogKind,
  operation: CatalogOperation,
): string[] {
  if (operation === "create")
    return [...clockFields(entityType, "update"), "lifecycle"];
  if (operation === "retire" || operation === "restore") return ["lifecycle"];
  if (entityType === "store_section") return ["name", "position_key"];
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

function wins(
  actionAt: string,
  mutationId: string,
  current: unknown,
): boolean {
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
  const candidateTime = Date.parse(actionAt);
  const currentTime = Date.parse(currentAt);
  if (!Number.isFinite(candidateTime) || !Number.isFinite(currentTime)) return true;
  return (
    candidateTime > currentTime ||
    (candidateTime === currentTime && mutationId > currentId)
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
  const {
    entity_id: entityId,
    entity_kind: entityType,
    operation,
    ...fields
  } = command.payload;
  if (
    typeof entityId !== "string" ||
    !["store_section", "recipe_tag", "dietary_tag", "unit_definition"].includes(
      String(entityType),
    ) ||
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
      nextFields.retired_at = catalogOperation === "retire" ? command.actionAt : null;
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
