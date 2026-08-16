import type { CanonicalRecord } from "./local-db";
import { decimal as parseDecimal } from "./shopping-projections";
import { readVisibleRecords } from "./visible-records";
import { readEventScopedRecords } from "./archive-cache";

const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

export type Fraction = { numerator: bigint; denominator: bigint };
export const zeroFraction: Fraction = { numerator: 0n, denominator: 1n };
type ResolvedLine = { ingredientVersionId: string; quantity: Fraction };

export type EventCostsProjection = {
  currency: string;
  total: string;
  budget: string;
  expectedShopping: string;
  actual: string;
  remaining: string;
  missingIngredients: string[];
  scheduled: Map<
    string,
    { total: string; perDiner: string | null; missing: boolean }
  >;
};

function text(record: CanonicalRecord, key: string): string | undefined {
  const value = record.fields[key];
  return typeof value === "string" ? value : undefined;
}

export function decimal(value: unknown): Fraction | undefined {
  const parsed = parseDecimal(value);
  if (!parsed) return undefined;
  return {
    numerator: parsed.value,
    denominator: 10n ** BigInt(parsed.scale),
  };
}

export function multiply(left: Fraction, right: Fraction): Fraction {
  return {
    numerator: left.numerator * right.numerator,
    denominator: left.denominator * right.denominator,
  };
}

export function divide(left: Fraction, right: Fraction): Fraction | undefined {
  if (right.numerator === 0n) return undefined;
  return {
    numerator: left.numerator * right.denominator,
    denominator: left.denominator * right.numerator,
  };
}

export function add(left: Fraction, right: Fraction): Fraction {
  return {
    numerator:
      left.numerator * right.denominator + right.numerator * left.denominator,
    denominator: left.denominator * right.denominator,
  };
}

function subtract(left: Fraction, right: Fraction): Fraction {
  return {
    numerator:
      left.numerator * right.denominator - right.numerator * left.denominator,
    denominator: left.denominator * right.denominator,
  };
}

function maxZeroSubtract(left: Fraction, right: Fraction): Fraction {
  const difference = subtract(left, right);
  return difference.numerator > 0n
    ? difference
    : { numerator: 0n, denominator: 1n };
}

/** Display a rounded advisory monetary value without converting it to a JS number. */
export function money(value: Fraction): string {
  const sign = value.numerator < 0n ? "-" : "";
  const absolute =
    value.numerator < 0n ? { ...value, numerator: -value.numerator } : value;
  const scale = 100n;
  const rounded =
    (absolute.numerator * scale * 2n + absolute.denominator) /
    (absolute.denominator * 2n);
  return `${sign}${rounded / scale}.${(rounded % scale).toString().padStart(2, "0")}`;
}

function fieldId(record: CanonicalRecord, key: string): string | undefined {
  const value = text(record, key);
  return value && uuid.test(value) ? value : undefined;
}

/** Derive advisory costs exclusively from cached immutable event price snapshots. */
export async function readEventCosts(
  userId: string,
  organizationId: string,
  eventId: string,
): Promise<EventCostsProjection | undefined> {
  if (![userId, organizationId, eventId].every((value) => uuid.test(value)))
    return undefined;
  const kinds = [
    "event",
    "scheduled_recipe",
    "scheduled_ingredient_override",
    "recipe_version",
    "recipe_ingredient_line",
    "ingredient_version",
    "unit_definition",
    "event_ingredient_price",
    "event_ingredient_price_snapshot",
    "shopping_list",
    "shopping_ingredient_row",
    "shopping_contribution",
    "shopping_contribution_snapshot",
    "receipt",
  ] as const;
  const records = await Promise.all(
    kinds.map((kind) =>
      kind === "event"
        ? readVisibleRecords(userId, organizationId, kind, true)
        : readEventScopedRecords(userId, organizationId, eventId, kind, true),
    ),
  );
  const byKind = new Map(kinds.map((kind, index) => [kind, records[index]]));
  const values = (kind: (typeof kinds)[number]): CanonicalRecord[] =>
    byKind.get(kind) ?? [];
  const event = byKind
    .get("event")
    ?.find(
      (record) =>
        record.entityId === eventId &&
        text(record, "organization_id") === organizationId,
    );
  const currency = event && text(event, "currency");
  const budget = event && decimal(event.fields.budget_amount);
  if (!event || !currency || !budget || !/^[A-Z]{3}$/.test(currency))
    return undefined;
  const ingredientVersions = new Map(
    values("ingredient_version")
      .filter((record) => text(record, "organization_id") === organizationId)
      .map((record) => [
        record.entityId,
        {
          ingredientId: fieldId(record, "ingredient_id"),
          unitId: fieldId(record, "canonical_unit_id"),
          name: text(record, "name"),
        },
      ]),
  );
  const units = new Map(
    values("unit_definition").map((record) => [
      record.entityId,
      {
        dimension: text(record, "dimension"),
        factor: decimal(record.fields.base_unit_factor),
      },
    ]),
  );
  const snapshots = new Map(
    values("event_ingredient_price_snapshot")
      .filter((record) => fieldId(record, "event_id") === eventId)
      .map((record) => [record.entityId, record]),
  );
  const prices = new Map(
    values("event_ingredient_price")
      .filter((record) => fieldId(record, "event_id") === eventId)
      .map((record) => [
        fieldId(record, "ingredient_id"),
        snapshots.get(fieldId(record, "current_snapshot_id") ?? ""),
      ]),
  );
  const versions = new Map(
    values("recipe_version").map((record) => [record.entityId, record]),
  );
  const lines = values("recipe_ingredient_line");
  const overrides = values("scheduled_ingredient_override");
  const scheduled = new Map<
    string,
    { total: string; perDiner: string | null; missing: boolean }
  >();
  const missing = new Set<string>();
  let total = { numerator: 0n, denominator: 1n };
  for (const item of values("scheduled_recipe")) {
    if (fieldId(item, "event_id") !== eventId || item.lifecycle !== "active")
      continue;
    const version = versions.get(fieldId(item, "recipe_version_id") ?? "");
    const selected = decimal(item.fields.selected_scale_amount);
    const base = version && decimal(version.fields.base_scaling_amount);
    const diners = item.fields.diner_count;
    if (
      !version ||
      !selected ||
      !base ||
      typeof diners !== "number" ||
      !Number.isSafeInteger(diners)
    )
      continue;
    const proportional = divide(selected, base);
    if (!proportional) continue;
    const replacements = new Map(
      overrides
        .filter(
          (override) =>
            override.lifecycle === "active" &&
            fieldId(override, "event_id") === eventId &&
            fieldId(override, "scheduled_recipe_id") === item.entityId &&
            override.fields.override_kind === "replace",
        )
        .map((override) => [text(override, "target_line_key"), override]),
    );
    const resolved: ResolvedLine[] = lines
      .filter((line) => fieldId(line, "recipe_version_id") === version.entityId)
      .map((line) => {
        const replacement = replacements.get(text(line, "line_key"));
        const quantity = decimal(
          replacement?.fields.quantity ?? line.fields.base_quantity,
        );
        const ingredientVersionId = fieldId(
          replacement ?? line,
          "ingredient_version_id",
        );
        const behavior = line.fields.scaling_behavior;
        return quantity && ingredientVersionId
          ? {
              ingredientVersionId,
              quantity:
                behavior === "fixed"
                  ? quantity
                  : multiply(quantity, proportional),
            }
          : undefined;
      })
      .filter((line): line is ResolvedLine => line !== undefined);
    for (const override of overrides)
      if (
        override.lifecycle === "active" &&
        fieldId(override, "event_id") === eventId &&
        fieldId(override, "scheduled_recipe_id") === item.entityId &&
        override.fields.override_kind === "add"
      ) {
        const quantity = decimal(override.fields.quantity);
        const ingredientVersionId = fieldId(override, "ingredient_version_id");
        if (quantity && ingredientVersionId)
          resolved.push({ ingredientVersionId, quantity });
      }
    let itemTotal = { numerator: 0n, denominator: 1n };
    let itemMissing = false;
    for (const line of resolved) {
      if (line.quantity.numerator === 0n) continue;
      const ingredient = ingredientVersions.get(line.ingredientVersionId);
      const snapshot = ingredient?.ingredientId
        ? prices.get(ingredient.ingredientId)
        : undefined;
      const amount = snapshot && decimal(snapshot.fields.price_amount);
      const pricedQuantity =
        snapshot && decimal(snapshot.fields.priced_quantity);
      const pricedUnit = snapshot && fieldId(snapshot, "priced_unit_id");
      const canonicalUnit = ingredient?.unitId;
      const sourceUnit = canonicalUnit ? units.get(canonicalUnit) : undefined;
      const targetUnit = pricedUnit ? units.get(pricedUnit) : undefined;
      if (
        !ingredient ||
        !snapshot ||
        snapshot.fields.state !== "available" ||
        snapshot.fields.currency !== currency ||
        !amount ||
        !pricedQuantity ||
        !sourceUnit?.factor ||
        !targetUnit?.factor ||
        sourceUnit.dimension !== targetUnit.dimension
      ) {
        itemMissing = true;
        missing.add(ingredient?.name ?? line.ingredientVersionId);
        continue;
      }
      const converted = divide(
        multiply(line.quantity, sourceUnit.factor),
        targetUnit.factor,
      );
      const cost =
        converted && divide(multiply(amount, converted), pricedQuantity);
      if (!cost) {
        itemMissing = true;
        missing.add(ingredient.name ?? line.ingredientVersionId);
        continue;
      }
      itemTotal = add(itemTotal, cost);
    }
    total = add(total, itemTotal);
    const perDiner =
      diners > 0
        ? divide(itemTotal, { numerator: BigInt(diners), denominator: 1n })
        : undefined;
    scheduled.set(item.entityId, {
      total: money(itemTotal),
      perDiner: perDiner ? money(perDiner) : null,
      missing: itemMissing,
    });
  }
  let expectedShopping = { numerator: 0n, denominator: 1n };
  const currentRevisions = new Map(
    values("shopping_list")
      .filter(
        (item) =>
          item.lifecycle === "active" && fieldId(item, "event_id") === eventId,
      )
      .map((item) => [
        item.entityId,
        fieldId(item, "current_generation_revision_id"),
      ]),
  );
  const contributions = new Map(
    values("shopping_contribution")
      .filter((item) => item.lifecycle === "active")
      .map((item) => [item.entityId, item]),
  );
  for (const row of values("shopping_ingredient_row")) {
    const listId = fieldId(row, "shopping_list_id");
    const revisionId = listId ? currentRevisions.get(listId) : undefined;
    if (
      row.lifecycle !== "active" ||
      !listId ||
      fieldId(row, "event_id") !== eventId ||
      !revisionId
    )
      continue;
    const snapshots = values("shopping_contribution_snapshot").filter(
      (snapshot) => {
        const contribution = contributions.get(
          fieldId(snapshot, "shopping_contribution_id") ?? "",
        );
        return (
          snapshot.fields.active_in_revision === true &&
          snapshot.lifecycle === "active" &&
          fieldId(snapshot, "shopping_list_id") === listId &&
          fieldId(snapshot, "generation_revision_id") === revisionId &&
          contribution !== undefined &&
          fieldId(contribution, "shopping_ingredient_row_id") === row.entityId
        );
      },
    );
    const generated = snapshots
      .map((snapshot) => decimal(snapshot.fields.generated_quantity))
      .filter((value): value is Fraction => value !== undefined)
      .reduce(add, { numerator: 0n, denominator: 1n });
    const supply = decimal(row.fields.available_supply_quantity);
    const manual =
      row.fields.manual_purchase_target === null
        ? undefined
        : decimal(row.fields.manual_purchase_target);
    const quantity =
      manual ?? (supply ? maxZeroSubtract(generated, supply) : generated);
    if (quantity.numerator === 0n) continue;
    const snapshot = snapshots[0];
    const ingredient = ingredientVersions.get(
      snapshot ? (fieldId(snapshot, "ingredient_version_id") ?? "") : "",
    );
    const amount = snapshot && decimal(snapshot.fields.price_amount);
    const pricedQuantity = snapshot && decimal(snapshot.fields.priced_quantity);
    const pricedUnit = snapshot && fieldId(snapshot, "priced_unit_id");
    const sourceUnit = ingredient?.unitId
      ? units.get(ingredient.unitId)
      : undefined;
    const targetUnit = pricedUnit ? units.get(pricedUnit) : undefined;
    if (
      !quantity ||
      !ingredient ||
      !amount ||
      !pricedQuantity ||
      !sourceUnit?.factor ||
      !targetUnit?.factor ||
      snapshot?.fields.currency !== currency ||
      sourceUnit.dimension !== targetUnit.dimension
    ) {
      missing.add(
        ingredient?.name ?? text(row, "ingredient_name") ?? row.entityId,
      );
      continue;
    }
    const converted = divide(
      multiply(quantity, sourceUnit.factor),
      targetUnit.factor,
    );
    const cost =
      converted && divide(multiply(amount, converted), pricedQuantity);
    if (cost) expectedShopping = add(expectedShopping, cost);
    else missing.add(ingredient.name ?? row.entityId);
  }
  const actual = values("receipt")
    .filter(
      (receipt) =>
        receipt.lifecycle === "active" &&
        fieldId(receipt, "event_id") === eventId &&
        receipt.fields.currency === currency,
    )
    .map((receipt) => decimal(receipt.fields.total_amount))
    .filter((amount): amount is Fraction => amount !== undefined)
    .reduce(add, { numerator: 0n, denominator: 1n });
  return {
    currency,
    total: money(total),
    budget: money(budget),
    expectedShopping: money(expectedShopping),
    actual: money(actual),
    remaining: money(subtract(budget, actual)),
    missingIngredients: [...missing].sort((left, right) =>
      left.localeCompare(right),
    ),
    scheduled,
  };
}
