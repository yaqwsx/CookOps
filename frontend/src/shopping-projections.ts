import type { CanonicalRecord } from "./local-db";
import { readVisibleRecords } from "./visible-records";

const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

export type ShoppingListSummary = {
  id: string;
  name: string;
  sourceCount: number;
  createdAt: string;
};

export type ShoppingRow = {
  id: string;
  ingredientName: string;
  sectionName: string | null;
  availableSupply: string;
  manualPurchaseTarget: string | null;
  target: string;
  remaining: string;
  unit: string;
  fulfilled: boolean;
  notRequired: boolean;
  contributions: ShoppingContribution[];
};

export type ShoppingContribution = {
  id: string;
  generated: string;
  fulfilled: boolean;
  retired: boolean;
  source: string | null;
};

export type ShoppingListProjection = ShoppingListSummary & {
  currentGenerationRevisionId: string;
  sourceRecipeIds: string[];
  rows: ShoppingRow[];
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
  return { value: BigInt(`${whole}${fraction}`), scale: fraction.length };
}

function atScale(value: Decimal, scale: number): bigint {
  return value.value * 10n ** BigInt(scale - value.scale);
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
    readVisibleRecords(userId, organizationId, "shopping_list"),
    readVisibleRecords(userId, organizationId, "shopping_revision_source"),
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
  const [lists, rows, contributions, snapshots, sections, units, sources] =
    await Promise.all([
      readShoppingLists(userId, organizationId, eventId),
      readVisibleRecords(userId, organizationId, "shopping_ingredient_row"),
      readVisibleRecords(userId, organizationId, "shopping_contribution", true),
      readVisibleRecords(
        userId,
        organizationId,
        "shopping_contribution_snapshot",
      ),
      readVisibleRecords(userId, organizationId, "store_section"),
      readVisibleRecords(userId, organizationId, "unit_definition"),
      readVisibleRecords(userId, organizationId, "shopping_revision_source"),
    ]);
  const summary = lists.find((list) => list.id === shoppingListId);
  if (!summary) return undefined;
  const list = (
    await readVisibleRecords(userId, organizationId, "shopping_list")
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
      .filter((unit) => value(unit, "id") === unit.entityId)
      .map((unit) => [
        unit.entityId,
        value(unit, "custom_name") ?? value(unit, "code"),
      ]),
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
    rows: rows
      .filter(
        (row) =>
          value(row, "shopping_list_id") === shoppingListId &&
          value(row, "organization_id") === organizationId &&
          value(row, "event_id") === eventId &&
          uuid.test(row.entityId) &&
          value(row, "id") === row.entityId,
      )
      .map((row) => {
        const rowContributions = contributions.filter(
          (contribution) =>
            value(contribution, "shopping_list_id") === shoppingListId &&
            value(contribution, "shopping_ingredient_row_id") ===
              row.entityId &&
            value(contribution, "organization_id") === organizationId &&
            value(contribution, "event_id") === eventId &&
            value(contribution, "id") === contribution.entityId,
        );
        const contributionIds = rowContributions.map(
          (contribution) => contribution.entityId,
        );
        const generated = add(
          snapshots
            .filter(
              (snapshot) =>
                value(snapshot, "shopping_list_id") === shoppingListId &&
                value(snapshot, "organization_id") === organizationId &&
                value(snapshot, "event_id") === eventId &&
                value(snapshot, "generation_revision_id") ===
                  currentRevisionId &&
                snapshot.fields.active_in_revision === true &&
                contributionIds.includes(
                  value(snapshot, "shopping_contribution_id") ?? "",
                ),
            )
            .map((snapshot) => decimal(snapshot.fields.generated_quantity))
            .filter((amount): amount is Decimal => amount !== undefined),
        );
        const credit = add(
          contributions
            .filter((contribution) =>
              contributionIds.includes(contribution.entityId),
            )
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
          notRequired: target.value === 0n,
          contributions: rowContributions.map((contribution) => {
            const snapshot = snapshots.find(
              (item) =>
                value(item, "generation_revision_id") === currentRevisionId &&
                value(item, "shopping_contribution_id") ===
                  contribution.entityId,
            );
            const details = snapshot?.fields.source_details;
            const source =
              details && typeof details === "object" && !Array.isArray(details)
                ? (details as Record<string, unknown>).recipe_name
                : undefined;
            const amount = snapshot
              ? decimal(snapshot.fields.generated_quantity)
              : undefined;
            return {
              id: contribution.entityId,
              generated: print(amount ?? add([])),
              fulfilled:
                (decimal(contribution.fields.fulfilment_credit)?.value ?? 0n) >
                0n,
              retired:
                contribution.lifecycle === "retired" ||
                snapshot?.fields.active_in_revision !== true,
              source: typeof source === "string" ? source : null,
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
