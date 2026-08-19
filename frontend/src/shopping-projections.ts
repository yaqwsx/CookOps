import type { CanonicalRecord } from "./local-db";
import { readEventScopedRecords } from "./archive-cache";
import { divide, money, multiply, type Fraction } from "./exact-decimal";

const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const maxDecimalDigits = 38;

export type ShoppingListSummary = {
  id: string;
  name: string;
  sourceCount: number;
  createdAt: string;
};

export type ShoppingRow = {
  id: string;
  ingredientName: string;
  note: string | null;
  storeSectionOverrideId: string | null;
  sectionName: string | null;
  availableSupply: string;
  manualPurchaseTarget: string | null;
  target: string;
  remaining: string;
  unit: string;
  fulfilled: boolean;
  partial: boolean;
  notRequired: boolean;
  contributions: ShoppingContribution[];
};

export type ShoppingContribution = {
  id: string;
  generated: string;
  requiredQuantity: string;
  fulfilled: boolean;
  partial: boolean;
  retired: boolean;
  source: string | null;
  recipeDescription: string | null;
  day: string | null;
  mealRole: string | null;
  lineNotes: string[];
  recipeNotes: string[];
  ingredientNotes: string[];
  estimatedUnitPrice: string | null;
  expectedCost: string | null;
};

export type AdHocShoppingItem = {
  id: string;
  name: string;
  target: string;
  unitId: string;
  unit: string;
  sectionId: string;
  sectionName: string | null;
  note: string | null;
  fulfilled: boolean;
  retired: boolean;
};

export type ShoppingInputOption = { id: string; name: string };

export type ShoppingListProjection = ShoppingListSummary & {
  currentGenerationRevisionId: string;
  sourceRecipeIds: string[];
  rows: ShoppingRow[];
  adHocItems: AdHocShoppingItem[];
  storeSections: ShoppingInputOption[];
  quantityUnits: ShoppingInputOption[];
};

function value(record: CanonicalRecord, key: string): string | undefined {
  const item = record.fields[key];
  return typeof item === "string" ? item : undefined;
}

export type Decimal = { value: bigint; scale: number };

export function decimal(value: unknown): Decimal | undefined {
  if (typeof value !== "string" || !/^\d+(?:\.\d+)?$/.test(value))
    return undefined;
  const [whole, fraction = ""] = value.split(".");
  if (whole.length + fraction.length > maxDecimalDigits) return undefined;
  return { value: BigInt(`${whole}${fraction}`), scale: fraction.length };
}

function atScale(value: Decimal, scale: number): bigint {
  return value.value * 10n ** BigInt(scale - value.scale);
}

function fraction(value: Decimal): Fraction {
  return { numerator: value.value, denominator: 10n ** BigInt(value.scale) };
}

function detailsObject(value: unknown): Record<string, unknown> | undefined {
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : undefined;
}

function detailText(
  details: Record<string, unknown> | undefined,
  key: string,
): string | null {
  const value = details?.[key];
  return typeof value === "string" && value.trim() ? value : null;
}

function detailTexts(
  details: Record<string, unknown> | undefined,
  ...keys: string[]
): string[] {
  const result: string[] = [];
  for (const key of keys) {
    const value = details?.[key];
    if (typeof value === "string" && value.trim()) result.push(value);
    else if (Array.isArray(value))
      result.push(
        ...value.filter(
          (item): item is string => typeof item === "string" && item.trim().length > 0,
        ),
      );
  }
  return [...new Set(result)];
}

type UnitInfo = { dimension: string; factor: Decimal };

function validContribution(
  contribution: CanonicalRecord,
  rowId: string,
  rowIngredientId: string | undefined,
  shoppingListId: string,
  organizationId: string,
  eventId: string,
): boolean {
  const ingredientId = value(contribution, "ingredient_id");
  return (
    uuid.test(contribution.entityId) &&
    value(contribution, "id") === contribution.entityId &&
    uuid.test(ingredientId ?? "") &&
    uuid.test(rowIngredientId ?? "") &&
    ingredientId === rowIngredientId &&
    value(contribution, "shopping_ingredient_row_id") === rowId &&
    value(contribution, "shopping_list_id") === shoppingListId &&
    value(contribution, "organization_id") === organizationId &&
    value(contribution, "event_id") === eventId
  );
}

function validSnapshot(
  snapshot: CanonicalRecord,
  contribution: CanonicalRecord,
  currentRevisionId: string,
  shoppingListId: string,
  organizationId: string,
  eventId: string,
  ingredientVersionById: Map<string, CanonicalRecord>,
): boolean {
  const ingredientId = value(contribution, "ingredient_id");
  const version = ingredientVersionById.get(
    value(snapshot, "ingredient_version_id") ?? "",
  );
  return (
    uuid.test(snapshot.entityId) &&
    value(snapshot, "id") === snapshot.entityId &&
    value(snapshot, "shopping_contribution_id") === contribution.entityId &&
    value(snapshot, "shopping_list_id") === shoppingListId &&
    value(snapshot, "organization_id") === organizationId &&
    value(snapshot, "event_id") === eventId &&
    value(snapshot, "generation_revision_id") === currentRevisionId &&
    snapshot.fields.active_in_revision !== undefined &&
    typeof snapshot.fields.active_in_revision === "boolean" &&
    value(snapshot, "ingredient_id") === ingredientId &&
    version !== undefined &&
    value(version, "ingredient_id") === ingredientId
  );
}

function priceDetails(
  snapshot: CanonicalRecord | undefined,
  ingredientVersion: CanonicalRecord | undefined,
  quantity: Decimal | undefined,
  calculationUnitId: string | undefined,
  units: Map<string, UnitInfo>,
  unitNames: Map<string, string | undefined>,
): { estimatedUnitPrice: string; expectedCost: string } | undefined {
  if (!snapshot || !ingredientVersion) return undefined;
  const snapshotIngredientId = value(snapshot, "ingredient_id");
  const versionIngredientId = value(ingredientVersion, "ingredient_id");
  if (
    !snapshotIngredientId ||
    !versionIngredientId ||
    snapshotIngredientId !== versionIngredientId ||
    !uuid.test(snapshotIngredientId)
  )
    return undefined;
  const amount = decimal(snapshot.fields.price_amount);
  const pricedQuantity = decimal(snapshot.fields.priced_quantity);
  const pricedUnitId = value(snapshot, "priced_unit_id");
  const currency = value(snapshot, "currency");
  const canonicalUnitId = value(ingredientVersion, "canonical_unit_id");
  const calculationUnit = calculationUnitId
    ? units.get(calculationUnitId)
    : undefined;
  const sourceUnit = canonicalUnitId ? units.get(canonicalUnitId) : undefined;
  const targetUnit = pricedUnitId ? units.get(pricedUnitId) : undefined;
  const pricedUnitName = pricedUnitId ? unitNames.get(pricedUnitId) : undefined;
  if (
    !amount ||
    amount.value < 0n ||
    !quantity ||
    quantity.value < 0n ||
    !pricedQuantity ||
    pricedQuantity.value <= 0n ||
    !pricedUnitId ||
    !calculationUnit ||
    !currency ||
    !/^[A-Z]{3}$/.test(currency) ||
    !sourceUnit ||
    !targetUnit ||
    !pricedUnitName ||
    calculationUnit.dimension !== sourceUnit.dimension ||
    sourceUnit.dimension !== targetUnit.dimension ||
    calculationUnit.factor.value <= 0n ||
    sourceUnit.factor.value <= 0n ||
    targetUnit.factor.value <= 0n
  )
    return undefined;
  const converted = divide(
    multiply(fraction(quantity), fraction(calculationUnit.factor)),
    fraction(targetUnit.factor),
  );
  const cost = converted
    ? divide(
        multiply(fraction(amount), converted),
        fraction(pricedQuantity),
      )
    : undefined;
  if (!cost) return undefined;
  return {
    estimatedUnitPrice: `${print(amount)} / ${print(pricedQuantity)} ${pricedUnitName} (${currency})`,
    expectedCost: `${money(cost)} ${currency}`,
  };
}

export function add(values: Decimal[]): Decimal {
  const scale = Math.max(0, ...values.map((value) => value.scale));
  return {
    value: values.reduce((sum, value) => sum + atScale(value, scale), 0n),
    scale,
  };
}

export function maxZeroSubtract(left: Decimal, right: Decimal): Decimal {
  const scale = Math.max(left.scale, right.scale);
  return {
    value:
      atScale(left, scale) - atScale(right, scale) > 0n
        ? atScale(left, scale) - atScale(right, scale)
        : 0n,
    scale,
  };
}

export function print(value: Decimal): string {
  const digits = value.value.toString().padStart(value.scale + 1, "0");
  if (!value.scale) return digits;
  return `${digits.slice(0, -value.scale)}.${digits.slice(-value.scale)}`
    .replace(/\.0+$/, "")
    .replace(/(\.\d*?)0+$/, "$1");
}

function listSummary(
  record: CanonicalRecord,
  organizationId: string,
  eventId: string,
  sourceCount: number,
): ShoppingListSummary | undefined {
  const name = value(record, "name");
  const createdAt = value(record, "created_at") ?? record.updatedAt;
  if (
    !uuid.test(record.entityId) ||
    value(record, "id") !== record.entityId ||
    value(record, "organization_id") !== organizationId ||
    value(record, "event_id") !== eventId ||
    !name ||
    !createdAt
  )
    return undefined;
  const optimisticSources = record.fields.scheduled_recipe_ids;
  const optimisticSourceCount = Array.isArray(optimisticSources)
    ? optimisticSources.filter(
        (item) => typeof item === "string" && uuid.test(item),
      ).length
    : 0;
  return {
    id: record.entityId,
    name,
    sourceCount: sourceCount || optimisticSourceCount,
    createdAt,
  };
}

/** Read validated materialized shopping data from the user-and-organization-scoped replica. */
export async function readShoppingLists(
  userId: string,
  organizationId: string,
  eventId: string,
): Promise<ShoppingListSummary[]> {
  if (!uuid.test(eventId)) return [];
  const [lists, sources] = await Promise.all([
    readEventScopedRecords(userId, organizationId, eventId, "shopping_list"),
    readEventScopedRecords(
      userId,
      organizationId,
      eventId,
      "shopping_revision_source",
    ),
  ]);
  const sourceCounts = new Map<string, number>();
  for (const source of sources) {
    const listId = value(source, "shopping_list_id");
    if (
      listId &&
      uuid.test(listId) &&
      value(source, "organization_id") === organizationId &&
      value(source, "event_id") === eventId
    )
      sourceCounts.set(listId, (sourceCounts.get(listId) ?? 0) + 1);
  }
  return lists
    .map((record) =>
      listSummary(
        record,
        organizationId,
        eventId,
        sourceCounts.get(record.entityId) ?? 0,
      ),
    )
    .filter((item): item is ShoppingListSummary => item !== undefined)
    .sort(
      (left, right) =>
        right.createdAt.localeCompare(left.createdAt) ||
        left.id.localeCompare(right.id),
    );
}

export async function readShoppingList(
  userId: string,
  organizationId: string,
  eventId: string,
  shoppingListId: string,
): Promise<ShoppingListProjection | undefined> {
  if (!uuid.test(shoppingListId)) return undefined;
  const [
    lists,
    rows,
    contributions,
    snapshots,
    sections,
    units,
    ingredientVersions,
    sources,
    adHocItems,
  ] = await Promise.all([
    readShoppingLists(userId, organizationId, eventId),
    readEventScopedRecords(
      userId,
      organizationId,
      eventId,
      "shopping_ingredient_row",
    ),
    readEventScopedRecords(
      userId,
      organizationId,
      eventId,
      "shopping_contribution",
      true,
    ),
    readEventScopedRecords(
      userId,
      organizationId,
      eventId,
      "shopping_contribution_snapshot",
    ),
    readEventScopedRecords(userId, organizationId, eventId, "store_section"),
    readEventScopedRecords(userId, organizationId, eventId, "unit_definition"),
    readEventScopedRecords(
      userId,
      organizationId,
      eventId,
      "ingredient_version",
    ),
    readEventScopedRecords(
      userId,
      organizationId,
      eventId,
      "shopping_revision_source",
    ),
    readEventScopedRecords(
      userId,
      organizationId,
      eventId,
      "ad_hoc_shopping_item",
      true,
    ),
  ]);
  const summary = lists.find((list) => list.id === shoppingListId);
  if (!summary) return undefined;
  const list = (
    await readEventScopedRecords(
      userId,
      organizationId,
      eventId,
      "shopping_list",
    )
  ).find((record) => record.entityId === shoppingListId);
  const currentRevisionId =
    list && value(list, "current_generation_revision_id");
  if (!currentRevisionId || !uuid.test(currentRevisionId)) return undefined;
  const sectionNames = new Map(
    sections
      .filter(
        (section) =>
          value(section, "id") === section.entityId &&
          value(section, "organization_id") === organizationId,
      )
      .map((section) => [section.entityId, value(section, "name")]),
  );
  const unitNames = new Map(
    units
      .filter(
        (unit) =>
          uuid.test(unit.entityId) &&
          value(unit, "id") === unit.entityId &&
          (unit.fields.organization_id === null ||
            value(unit, "organization_id") === organizationId),
      )
      .map((unit) => [
        unit.entityId,
        value(unit, "custom_name") ?? value(unit, "code"),
      ]),
  );
  const unitInfo = new Map(
    units
      .filter(
        (unit) =>
          uuid.test(unit.entityId) &&
          value(unit, "id") === unit.entityId &&
          typeof value(unit, "dimension") === "string" &&
          (unit.fields.organization_id === null ||
            value(unit, "organization_id") === organizationId) &&
          decimal(unit.fields.base_unit_factor),
      )
      .flatMap((unit) => {
        const dimension = value(unit, "dimension");
        const factor = decimal(unit.fields.base_unit_factor);
        return dimension && factor
          ? [[unit.entityId, { dimension, factor }] as const]
          : [];
      }),
  );
  const ingredientVersionById = new Map(
    ingredientVersions
      .filter(
        (version) =>
          uuid.test(version.entityId) &&
          value(version, "id") === version.entityId &&
          value(version, "organization_id") === organizationId &&
          uuid.test(value(version, "ingredient_id") ?? ""),
      )
      .map((version) => [version.entityId, version]),
  );
  return {
    ...summary,
    currentGenerationRevisionId: currentRevisionId,
    sourceRecipeIds: sources
      .filter(
        (source) =>
          value(source, "shopping_list_id") === shoppingListId &&
          value(source, "organization_id") === organizationId &&
          value(source, "event_id") === eventId &&
          value(source, "generation_revision_id") === currentRevisionId,
      )
      .map((source) => value(source, "scheduled_recipe_id"))
      .filter((id): id is string => typeof id === "string" && uuid.test(id))
      .sort(),
    storeSections: sections
      .filter(
        (section) =>
          value(section, "id") === section.entityId &&
          value(section, "organization_id") === organizationId &&
          typeof value(section, "name") === "string",
      )
      .flatMap((section) => {
        const name = value(section, "name");
        return name ? [{ id: section.entityId, name }] : [];
      })
      .sort(
        (left, right) =>
          left.name.localeCompare(right.name) ||
          left.id.localeCompare(right.id),
      ),
    quantityUnits: units
      .filter(
        (unit) =>
          value(unit, "id") === unit.entityId &&
          unit.fields.allows_ingredient_quantity === true &&
          (unit.fields.organization_id === null ||
            value(unit, "organization_id") === organizationId),
      )
      .map((unit) => ({
        id: unit.entityId,
        name: value(unit, "custom_name") ?? value(unit, "code") ?? "",
      }))
      .filter((unit) => Boolean(unit.name))
      .sort(
        (left, right) =>
          left.name.localeCompare(right.name) ||
          left.id.localeCompare(right.id),
      ),
    adHocItems: adHocItems
      .filter(
        (item) =>
          value(item, "id") === item.entityId &&
          value(item, "organization_id") === organizationId &&
          value(item, "event_id") === eventId &&
          value(item, "shopping_list_id") === shoppingListId &&
          value(item, "name") &&
          decimal(item.fields.target_amount) &&
          unitNames.has(value(item, "unit_id") ?? ""),
      )
      .flatMap((item) => {
        const name = value(item, "name");
        const target = decimal(item.fields.target_amount);
        const unitId = value(item, "unit_id");
        const unit = unitId ? unitNames.get(unitId) : undefined;
        if (!name || !target || !unit) return [];
        return [
          {
            id: item.entityId,
            name,
            target: print(target),
            unitId: unitId ?? "",
            unit,
            sectionId: value(item, "store_section_id") ?? "",
            sectionName:
              sectionNames.get(value(item, "store_section_id") ?? "") ?? null,
            note: value(item, "note") ?? null,
            fulfilled:
              (decimal(item.fields.fulfilment_credit)?.value ?? 0n) >=
              target.value,
            retired: item.lifecycle === "retired",
          },
        ];
      })
      .sort(
        (left, right) =>
          (left.sectionName ?? "").localeCompare(right.sectionName ?? "") ||
          left.name.localeCompare(right.name) ||
          left.id.localeCompare(right.id),
      ),
    rows: rows
      .filter(
        (row) =>
          value(row, "shopping_list_id") === shoppingListId &&
          value(row, "organization_id") === organizationId &&
          value(row, "event_id") === eventId &&
          uuid.test(row.entityId) &&
          value(row, "id") === row.entityId &&
          uuid.test(value(row, "ingredient_id") ?? "") &&
          unitNames.has(value(row, "calculation_unit_id") ?? ""),
      )
      .map((row) => {
        const rowContributions = contributions.filter(
          (contribution) =>
            validContribution(
              contribution,
              row.entityId,
              value(row, "ingredient_id"),
              shoppingListId,
              organizationId,
              eventId,
            ),
        );
        const validSnapshots = snapshots.filter((snapshot) =>
          rowContributions.some((contribution) =>
            validSnapshot(
              snapshot,
              contribution,
              currentRevisionId,
              shoppingListId,
              organizationId,
              eventId,
              ingredientVersionById,
            ),
          ),
        );
        const generated = add(
          validSnapshots
            .filter(
              (snapshot) =>
                snapshot.fields.active_in_revision === true,
            )
            .map((snapshot) => decimal(snapshot.fields.generated_quantity))
            .filter((amount): amount is Decimal => amount !== undefined),
        );
        const credit = add(
          rowContributions
            .map((contribution) =>
              decimal(contribution.fields.fulfilment_credit),
            )
            .filter((amount): amount is Decimal => amount !== undefined)
            .concat(decimal(row.fields.aggregate_fulfilment_credit) ?? []),
        );
        const available = decimal(row.fields.available_supply_quantity);
        const manual =
          row.fields.manual_purchase_target === null
            ? undefined
            : decimal(row.fields.manual_purchase_target);
        const unit = unitNames.get(value(row, "calculation_unit_id") ?? "");
        const target =
          manual ?? maxZeroSubtract(generated, available ?? add([]));
        const remaining = maxZeroSubtract(target, credit);
        const override = value(row, "store_section_override_id");
        return {
          id: row.entityId,
          ingredientName: value(row, "ingredient_name"),
          note: typeof row.fields.note === "string" ? row.fields.note : null,
          storeSectionOverrideId: override ?? null,
          sectionName:
            (override && sectionNames.get(override)) ??
            value(row, "default_store_section_name") ??
            null,
          availableSupply: print(available ?? add([])),
          manualPurchaseTarget: manual ? print(manual) : null,
          target: print(target),
          remaining: print(remaining),
          unit,
          fulfilled: target.value > 0n && remaining.value === 0n,
          partial:
            target.value > 0n && remaining.value > 0n && credit.value > 0n,
          notRequired: target.value === 0n,
          contributions: rowContributions.map((contribution) => {
            const snapshot = validSnapshots.find((item) =>
              validSnapshot(
                item,
                contribution,
                currentRevisionId,
                shoppingListId,
                organizationId,
                eventId,
                ingredientVersionById,
              ),
            );
            const details = detailsObject(snapshot?.fields.source_details);
            const source = detailText(details, "recipe_name");
            const amount = snapshot
              ? decimal(snapshot.fields.generated_quantity)
              : undefined;
            const price = priceDetails(
              snapshot,
              ingredientVersionById.get(
                value(snapshot ?? contribution, "ingredient_version_id") ?? "",
              ),
              target,
              value(row, "calculation_unit_id"),
              unitInfo,
              unitNames,
            );
            return {
              id: contribution.entityId,
              generated: print(amount ?? add([])),
              requiredQuantity: print(amount ?? add([])),
              fulfilled:
                amount !== undefined &&
                amount.value > 0n &&
                (decimal(contribution.fields.fulfilment_credit)?.value ?? 0n) >=
                  amount.value,
              partial:
                amount !== undefined &&
                amount.value > 0n &&
                (decimal(contribution.fields.fulfilment_credit)?.value ?? 0n) >
                  0n &&
                (decimal(contribution.fields.fulfilment_credit)?.value ?? 0n) <
                  amount.value,
              retired:
                contribution.lifecycle === "retired" ||
                snapshot?.fields.active_in_revision !== true,
              source,
              recipeDescription: detailText(details, "recipe_description"),
              day: detailText(details, "day"),
              mealRole: detailText(details, "meal_role"),
              lineNotes: detailTexts(details, "line_notes"),
              recipeNotes: detailTexts(details, "recipe_notes", "recipe_note"),
              ingredientNotes: detailTexts(
                details,
                "ingredient_notes",
                "ingredient_note",
              ),
              estimatedUnitPrice: price?.estimatedUnitPrice ?? null,
              expectedCost: price?.expectedCost ?? null,
            };
          }),
        };
      })
      .filter((row): row is ShoppingRow =>
        Boolean(row.ingredientName && row.unit),
      )
      .sort(
        (left, right) =>
          (left.sectionName ?? "").localeCompare(right.sectionName ?? "") ||
          left.ingredientName.localeCompare(right.ingredientName) ||
          left.id.localeCompare(right.id),
      ),
  };
}
