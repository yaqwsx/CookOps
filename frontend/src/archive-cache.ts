import { type ArchiveRecord, type CanonicalRecord, localDb } from "./local-db";
import { readVisibleRecords } from "./visible-records";

const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const MAX_ARCHIVE_BYTES = 8 * 1024 * 1024;
const MAX_ARCHIVE_RECORDS = 20_000;
export const dietaryTagSeedKeys: ReadonlySet<string> = new Set(["vegetarian", "vegan", "gluten", "lactose"]);
export type ArchivedDietaryTagDescriptor = { id: string; seedKey?: string; name?: string };

export function parseArchivedDietaryTagDescriptor(value: unknown): ArchivedDietaryTagDescriptor | undefined {
  if (!object(value) || typeof value.id !== "string" || !uuid.test(value.id)) return undefined;
  const hasName = typeof value.name === "string" && value.name.length > 0;
  const hasSeed = typeof value.seed_key === "string" && dietaryTagSeedKeys.has(value.seed_key);
  if (hasName === hasSeed || (value.name !== undefined && value.name !== null && !hasName) || (value.seed_key !== undefined && value.seed_key !== null && !hasSeed)) return undefined;
  return hasName
    ? { id: value.id, name: value.name as string }
    : { id: value.id, seedKey: value.seed_key as string };
}
const archiveKinds: Record<string, string> = {
  event_days: "event_day",
  event_meal_roles: "event_meal_role",
  scheduled_recipes: "scheduled_recipe",
  scheduled_ingredient_overrides: "scheduled_ingredient_override",
  event_ingredient_prices: "event_ingredient_price",
  event_ingredient_price_snapshots: "event_ingredient_price_snapshot",
  shopping_lists: "shopping_list",
  shopping_generation_revisions: "shopping_generation_revision",
  shopping_revision_sources: "shopping_revision_source",
  shopping_ingredient_rows: "shopping_ingredient_row",
  shopping_contributions: "shopping_contribution",
  shopping_contribution_snapshots: "shopping_contribution_snapshot",
  ad_hoc_shopping_items: "ad_hoc_shopping_item",
  receipts: "receipt",
  receipt_attachments: "receipt_attachment",
  recipe_versions: "recipe_version",
  recipes: "recipe",
  recipe_version_lines: "recipe_ingredient_line",
  recipe_version_tags: "recipe_version_tag",
  recipe_tags: "recipe_tag",
  ingredients: "ingredient",
  ingredient_versions: "ingredient_version",
  ingredient_version_dietary_tags: "ingredient_version_dietary_tag",
  units: "unit_definition",
  dietary_tags: "dietary_tag",
  store_sections: "store_section",
  dietary_exceptions: "event_dietary_exception",
  resolved_dietary_warnings: "resolved_dietary_warning",
  attribution_users: "user",
};
const requiredCollections = new Set([
  "event_days",
  "event_meal_roles",
  "scheduled_recipes",
  "scheduled_ingredient_overrides",
  "event_ingredient_prices",
  "event_ingredient_price_snapshots",
  "shopping_lists",
  "shopping_generation_revisions",
  "shopping_revision_sources",
  "shopping_ingredient_rows",
  "shopping_contributions",
  "shopping_contribution_snapshots",
  "ad_hoc_shopping_items",
  "receipts",
  "receipt_attachments",
  "recipe_versions",
  "recipes",
  "recipe_version_lines",
  "recipe_version_tags",
  "recipe_tags",
  "ingredients",
  "ingredient_versions",
  "ingredient_version_dietary_tags",
  "units",
  "dietary_tags",
  "store_sections",
  "dietary_exceptions",
  "resolved_dietary_warnings",
  "field_clocks",
  "attribution_users",
]);
const eventOwnedCollections = new Set([
  "event_days",
  "event_meal_roles",
  "scheduled_recipes",
  "scheduled_ingredient_overrides",
  "event_ingredient_prices",
  "shopping_lists",
  "ad_hoc_shopping_items",
  "receipts",
  "resolved_dietary_warnings",
]);
const organizationRequiredKinds = new Set([
  "scheduled_recipe",
  "scheduled_ingredient_override",
  "event_ingredient_price",
  "event_ingredient_price_snapshot",
  "shopping_list",
  "shopping_generation_revision",
  "shopping_revision_source",
  "shopping_ingredient_row",
  "shopping_contribution",
  "shopping_contribution_snapshot",
  "recipe_version",
  "recipe",
  "recipe_ingredient_line",
  "recipe_version_tag",
  "recipe_tag",
  "ingredient",
  "ingredient_version",
  "ingredient_version_dietary_tag",
  "dietary_tag",
  "store_section",
  "receipt",
  "receipt_attachment",
  "resolved_dietary_warning",
]);

function object(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

function canonical(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(canonical).join(",")}]`;
  if (object(value))
    return `{${Object.keys(value)
      .sort()
      .map((key) => `${JSON.stringify(key)}:${canonical(value[key])}`)
      .join(",")}}`;
  return JSON.stringify(value);
}

async function sha256(value: string): Promise<string> {
  const digest = await crypto.subtle.digest(
    "SHA-256",
    new TextEncoder().encode(value),
  );
  return [...new Uint8Array(digest)]
    .map((part) => part.toString(16).padStart(2, "0"))
    .join("");
}

function parseArchive(
  value: unknown,
  eventId: string,
  organizationId: string,
): {
  hash: string;
  payload: Record<string, unknown>;
} {
  if (
    !object(value) ||
    value.archive_schema_version !== 1 ||
    typeof value.content_hash !== "string" ||
    !/^[0-9a-f]{64}$/i.test(value.content_hash) ||
    !object(value.payload) ||
    value.payload.schema_version !== 1
  )
    throw new Error("Invalid archive response.");
  const keys = new Set(Object.keys(value.payload));
  if (
    keys.size !== requiredCollections.size + 2 ||
    !keys.has("event") ||
    !keys.has("schema_version") ||
    [...requiredCollections].some((key) => !keys.has(key)) ||
    [...keys].some(
      (key) =>
        key !== "event" &&
        key !== "schema_version" &&
        !requiredCollections.has(key),
    )
  )
    throw new Error("Invalid archive response.");
  const event = value.payload.event;
  if (
    !object(event) ||
    event.id !== eventId ||
    event.organization_id !== organizationId ||
    (event.lifecycle !== "active" && event.lifecycle !== "archived")
  )
    throw new Error("Invalid archive event.");
  for (const key of requiredCollections) {
    if (!Array.isArray(value.payload[key]))
      throw new Error("Invalid archive collection.");
  }
  const records = Object.entries(value.payload).reduce(
    (count, [, item]) => count + (Array.isArray(item) ? item.length : 0),
    0,
  );
  if (
    records > MAX_ARCHIVE_RECORDS ||
    canonical(value.payload).length > MAX_ARCHIVE_BYTES
  )
    throw new Error("Archive response too large.");
  return {
    hash: value.content_hash.toLowerCase(),
    payload: value.payload,
  };
}

function recordFields(
  kind: string,
  value: unknown,
  organizationId: string,
): CanonicalRecord {
  if (!object(value)) throw new Error("Invalid archive record.");
  const identity =
    kind === "shopping_revision_source"
      ? [value.generation_revision_id, value.scheduled_recipe_id]
      : kind === "recipe_version_tag"
        ? [value.recipe_version_id, value.recipe_tag_id]
        : kind === "ingredient_version_dietary_tag"
          ? [value.ingredient_version_id, value.dietary_tag_id]
          : [value.id];
  if (identity.some((id) => typeof id !== "string" || !uuid.test(id)))
    throw new Error("Invalid archive record identity.");
  const id = identity.join(":");
  if (
    organizationRequiredKinds.has(kind) &&
    typeof value.organization_id !== "string"
  )
    throw new Error("Invalid archive organization scope.");
  if (
    typeof value.organization_id === "string" &&
    value.organization_id !== organizationId
  )
    throw new Error("Invalid archive record.");
  const retired = value.retired_at !== null && value.retired_at !== undefined;
  return {
    userId: "",
    organizationId,
    entityType: kind,
    entityId: id,
    recordSchemaVersion: 1,
    lifecycle: retired ? "retired" : "active",
    fields: value,
    fieldClocks: object(value.field_clocks) ? value.field_clocks : {},
    immutable: true,
    updatedAt:
      typeof value.updated_at === "string"
        ? value.updated_at
        : new Date(0).toISOString(),
  };
}

function validateRelations(
  payload: Record<string, unknown>,
  eventId: string,
): void {
  const rows = (key: string) => payload[key] as Record<string, unknown>[];
  const ids = (key: string) =>
    new Set(
      rows(key)
        .map((row) => row.id)
        .filter((id): id is string => typeof id === "string"),
    );
  const requireRef = (
    row: Record<string, unknown>,
    field: string,
    allowed: Set<string>,
  ) => {
    if (typeof row[field] !== "string" || !allowed.has(row[field] as string))
      throw new Error("Invalid archive relation.");
  };
  const byId = (key: string) =>
    new Map(rows(key).map((row) => [row.id as string, row]));
  const sameScope = (
    row: Record<string, unknown>,
    parent: Record<string, unknown> | undefined,
    fields: string[],
  ) => {
    if (!parent || fields.some((field) => row[field] !== parent[field]))
      throw new Error("Invalid archive relation scope.");
  };
  for (const key of eventOwnedCollections)
    for (const row of rows(key))
      if (row.event_id !== eventId)
        throw new Error("Invalid archive event linkage.");
  const ingredientIds = ids("ingredients");
  const ingredients = byId("ingredients");
  const ingredientVersionIds = ids("ingredient_versions");
  const ingredientVersions = byId("ingredient_versions");
  const recipeIds = ids("recipes");
  const recipes = byId("recipes");
  const recipeVersionIds = ids("recipe_versions");
  const recipeVersions = byId("recipe_versions");
  const scheduledIds = ids("scheduled_recipes");
  const scheduled = byId("scheduled_recipes");
  const revisionIds = ids("shopping_generation_revisions");
  const revisions = byId("shopping_generation_revisions");
  const listIds = ids("shopping_lists");
  const lists = byId("shopping_lists");
  const priceIds = ids("event_ingredient_prices");
  const prices = byId("event_ingredient_prices");
  const priceSnapshotIds = ids("event_ingredient_price_snapshots");
  const priceSnapshots = byId("event_ingredient_price_snapshots");
  const dietaryTagIds = ids("dietary_tags");
  for (const row of rows("dietary_exceptions")) {
    if (row.event_id !== eventId || !Array.isArray(row.tag_ids))
      throw new Error("Invalid archive event linkage.");
    for (const tagId of row.tag_ids) {
      if (typeof tagId !== "string" || !dietaryTagIds.has(tagId))
        throw new Error("Invalid archive relation.");
    }
  }
  for (const row of rows("scheduled_ingredient_overrides")) {
    requireRef(row, "scheduled_recipe_id", scheduledIds);
    requireRef(row, "ingredient_id", ingredientIds);
    requireRef(row, "ingredient_version_id", ingredientVersionIds);
    sameScope(row, scheduled.get(row.scheduled_recipe_id as string), ["event_id", "organization_id"]);
    sameScope(row, ingredients.get(row.ingredient_id as string), ["organization_id"]);
    sameScope(row, ingredientVersions.get(row.ingredient_version_id as string), ["organization_id", "ingredient_id"]);
  }
  for (const row of rows("event_ingredient_prices")) {
    requireRef(row, "ingredient_id", ingredientIds);
    sameScope(row, ingredients.get(row.ingredient_id as string), ["organization_id"]);
    if (row.current_snapshot_id != null) {
      requireRef(row, "current_snapshot_id", priceSnapshotIds);
      const snapshot = priceSnapshots.get(row.current_snapshot_id as string);
      if (!snapshot) throw new Error("Invalid archive relation.");
      sameScope(snapshot, row, ["event_id", "organization_id", "ingredient_id"]);
      if (snapshot?.event_ingredient_price_id !== row.id) throw new Error("Invalid archive relation.");
    }
  }
  for (const row of rows("event_ingredient_price_snapshots")) {
    requireRef(row, "event_ingredient_price_id", priceIds);
    sameScope(row, prices.get(row.event_ingredient_price_id as string), [
      "event_id",
      "organization_id",
      "ingredient_id",
    ]);
    if (row.previous_snapshot_id != null) {
      requireRef(row, "previous_snapshot_id", priceSnapshotIds);
      const previous = priceSnapshots.get(row.previous_snapshot_id as string);
      if (!previous) throw new Error("Invalid archive relation.");
      sameScope(previous, row, [
        "event_ingredient_price_id",
        "event_id",
        "organization_id",
        "ingredient_id",
      ]);
    }
  }
  const rowIds = ids("shopping_ingredient_rows");
  const contributionIds = ids("shopping_contributions");
  for (const row of rows("shopping_generation_revisions")) {
    requireRef(row, "shopping_list_id", listIds);
    sameScope(row, lists.get(row.shopping_list_id as string), [
      "event_id",
      "organization_id",
    ]);
    if (row.parent_revision_id != null) {
      requireRef(row, "parent_revision_id", revisionIds);
      sameScope(row, revisions.get(row.parent_revision_id as string), [
        "shopping_list_id",
        "event_id",
        "organization_id",
      ]);
    }
  }
  for (const row of rows("shopping_lists"))
    if (row.current_generation_revision_id != null) {
      requireRef(row, "current_generation_revision_id", revisionIds);
      const revision = revisions.get(row.current_generation_revision_id as string);
      sameScope(row, revision, ["event_id", "organization_id"]);
      if (revision?.shopping_list_id !== row.id) throw new Error("Invalid archive relation scope.");
    }
  for (const row of rows("shopping_revision_sources")) {
    requireRef(row, "generation_revision_id", revisionIds);
    requireRef(row, "shopping_list_id", listIds);
    sameScope(row, lists.get(row.shopping_list_id as string), [
      "event_id",
      "organization_id",
    ]);
    requireRef(row, "scheduled_recipe_id", scheduledIds);
    if (row.event_id !== eventId)
      throw new Error("Invalid archive event linkage.");
    sameScope(row, revisions.get(row.generation_revision_id as string), [
      "shopping_list_id",
      "event_id",
      "organization_id",
    ]);
    sameScope(row, scheduled.get(row.scheduled_recipe_id as string), [
      "event_id",
      "organization_id",
    ]);
  }
  const shoppingRows = byId("shopping_ingredient_rows");
  for (const row of rows("shopping_ingredient_rows")) {
    requireRef(row, "shopping_list_id", listIds);
    sameScope(row, lists.get(row.shopping_list_id as string), [
      "event_id",
      "organization_id",
    ]);
  }
  for (const row of rows("shopping_contributions")) {
    requireRef(row, "shopping_list_id", listIds);
    requireRef(row, "shopping_ingredient_row_id", rowIds);
    requireRef(row, "scheduled_recipe_id", scheduledIds);
    sameScope(row, shoppingRows.get(row.shopping_ingredient_row_id as string), [
      "shopping_list_id",
      "event_id",
      "organization_id",
      "ingredient_id",
    ]);
  }
  for (const row of rows("shopping_contribution_snapshots")) {
    requireRef(row, "generation_revision_id", revisionIds);
    requireRef(row, "shopping_contribution_id", contributionIds);
    const contribution = byId("shopping_contributions").get(
      row.shopping_contribution_id as string,
    );
    const revision = revisions.get(row.generation_revision_id as string);
    sameScope(row, revision, [
      "shopping_list_id",
      "event_id",
      "organization_id",
    ]);
    sameScope(row, contribution, [
      "shopping_list_id",
      "event_id",
      "organization_id",
      "ingredient_id",
    ]);
    if (row.ingredient_id !== contribution?.ingredient_id)
      throw new Error("Invalid archive relation scope.");
  }
  const receiptIds = ids("receipts");
  const receipts = byId("receipts");
  for (const row of rows("receipt_attachments")) {
    requireRef(row, "receipt_id", receiptIds);
    sameScope(row, receipts.get(row.receipt_id as string), [
      "event_id",
      "organization_id",
    ]);
  }
  for (const row of rows("recipe_versions")) {
    requireRef(row, "recipe_id", recipeIds);
    sameScope(row, recipes.get(row.recipe_id as string), ["organization_id"]);
  }
  for (const row of rows("scheduled_recipes")) {
    requireRef(row, "recipe_id", recipeIds);
    requireRef(row, "recipe_version_id", recipeVersionIds);
    if (row.event_day_id != null) {
      requireRef(row, "event_day_id", ids("event_days"));
      sameScope(row, byId("event_days").get(row.event_day_id as string), ["event_id"]);
    }
    if (row.event_meal_role_id != null) {
      requireRef(row, "event_meal_role_id", ids("event_meal_roles"));
      sameScope(row, byId("event_meal_roles").get(row.event_meal_role_id as string), ["event_id"]);
    }
    sameScope(row, recipes.get(row.recipe_id as string), ["organization_id"]);
    sameScope(row, recipeVersions.get(row.recipe_version_id as string), ["organization_id", "recipe_id"]);
  }
  for (const row of rows("recipe_version_lines")) {
    requireRef(row, "recipe_version_id", recipeVersionIds);
    requireRef(row, "recipe_id", recipeIds);
    sameScope(row, recipeVersions.get(row.recipe_version_id as string), ["recipe_id", "organization_id"]);
    sameScope(row, recipes.get(row.recipe_id as string), ["organization_id"]);
  }
  for (const row of rows("recipe_version_tags")) {
    requireRef(row, "recipe_version_id", recipeVersionIds);
    requireRef(row, "recipe_tag_id", ids("recipe_tags"));
    sameScope(row, recipeVersions.get(row.recipe_version_id as string), ["organization_id"]);
    sameScope(row, byId("recipe_tags").get(row.recipe_tag_id as string), ["organization_id"]);
  }
  for (const row of rows("ingredient_versions")) {
    requireRef(row, "ingredient_id", ids("ingredients"));
    sameScope(row, ingredients.get(row.ingredient_id as string), [
      "organization_id",
    ]);
  }
  for (const row of rows("recipe_versions"))
    if (row.based_on_version_id != null) {
      requireRef(row, "based_on_version_id", recipeVersionIds);
      sameScope(row, recipeVersions.get(row.based_on_version_id as string), ["recipe_id", "organization_id"]);
    }
  for (const row of rows("ingredient_version_dietary_tags")) {
    requireRef(row, "ingredient_version_id", ingredientVersionIds);
    sameScope(
      row,
      ingredientVersions.get(row.ingredient_version_id as string),
      ["organization_id"],
    );
    requireRef(row, "dietary_tag_id", ids("dietary_tags"));
    sameScope(row, byId("dietary_tags").get(row.dietary_tag_id as string), [
      "organization_id",
    ]);
  }
  for (const row of rows("resolved_dietary_warnings")) {
    requireRef(row, "scheduled_recipe_id", scheduledIds);
    if (row.id !== row.scheduled_recipe_id || !Array.isArray(row.warnings))
      throw new Error("Invalid archive relation.");
    sameScope(row, scheduled.get(row.scheduled_recipe_id as string), ["event_id", "organization_id"]);
    for (const warning of row.warnings) {
      if (!object(warning) || typeof warning.exception_name !== "string" || !Array.isArray(warning.tag_descriptors) || !warning.tag_descriptors.every((tag) => parseArchivedDietaryTagDescriptor(tag) !== undefined) || !Array.isArray(warning.ingredient_names) || !warning.ingredient_names.every((name) => typeof name === "string"))
        throw new Error("Invalid archive relation.");
      for (const tag of warning.tag_descriptors) {
        const descriptor = parseArchivedDietaryTagDescriptor(tag);
        if (!descriptor) throw new Error("Invalid archive relation.");
        requireRef(tag, "id", dietaryTagIds);
        const archivedTag = byId("dietary_tags").get(descriptor.id);
        if (!archivedTag) throw new Error("Invalid archive relation.");
        if (descriptor.seedKey !== undefined
          ? archivedTag.seed_key !== descriptor.seedKey || archivedTag.name != null
          : archivedTag.name !== descriptor.name || archivedTag.seed_key != null)
          throw new Error("Invalid archive relation.");
      }
    }
  }
}

export async function ensureArchivedEventCached(
  userId: string,
  organizationId: string,
  eventId: string,
  fetcher: typeof fetch = fetch,
): Promise<boolean> {
  if (!uuid.test(eventId) || !uuid.test(organizationId)) return false;
  const event = await localDb.canonicalRecords.get([
    userId,
    organizationId,
    "event",
    eventId,
  ]);
  const snapshotId = event?.fields.current_archive_snapshot_id;
  if (
    event?.fields.lifecycle !== "archived" ||
    typeof snapshotId !== "string" ||
    !uuid.test(snapshotId)
  )
    return false;
  const markerId = `archive:${snapshotId}`;
  if (
    await localDb.archiveRecords.get([
      userId,
      organizationId,
      eventId,
      snapshotId,
      "event_archive_snapshot",
      markerId,
    ])
  )
    return true;
  const response = await fetcher(
    `/api/v1/organizations/${encodeURIComponent(organizationId)}/events/${encodeURIComponent(eventId)}/archive/${encodeURIComponent(snapshotId)}`,
    { credentials: "same-origin", cache: "no-store" },
  );
  if (!response.ok)
    throw new Error(`Archive request failed: ${response.status}`);
  const body = await response.text();
  if (new TextEncoder().encode(body).byteLength > MAX_ARCHIVE_BYTES)
    throw new Error("Archive response too large.");
  const parsed = parseArchive(JSON.parse(body), eventId, organizationId);
  if ((await sha256(canonical(parsed.payload))) !== parsed.hash)
    throw new Error("Archive integrity check failed.");
  validateRelations(parsed.payload, eventId);
  const records: ArchiveRecord[] = [];
  for (const [key, items] of Object.entries(parsed.payload)) {
    if (
      key === "event" ||
      key === "schema_version"
    )
      continue;
    if (key === "field_clocks") {
      for (const item of items as unknown[]) {
        if (
          !object(item) ||
          typeof item.organization_id !== "string" ||
          item.organization_id !== organizationId ||
          typeof item.entity_id !== "string" ||
          !uuid.test(item.entity_id) ||
          typeof item.field_name !== "string"
        )
          throw new Error("Invalid archive field clock.");
      }
      continue;
    }
    const kind = archiveKinds[key];
    if (!kind || !Array.isArray(items))
      throw new Error("Invalid archive collection.");
    for (const item of items) {
      const record = recordFields(kind, item, organizationId);
      if (eventOwnedCollections.has(key) && record.fields.event_id !== eventId)
        throw new Error("Invalid archive event linkage.");
      records.push({ ...record, userId, eventId, snapshotId });
    }
  }
  await localDb.transaction("rw", localDb.archiveRecords, async () => {
    for (const record of records) {
      const existing = await localDb.archiveRecords.get([
        userId,
        organizationId,
        eventId,
        snapshotId,
        record.entityType,
        record.entityId,
      ]);
      if (!existing || existing.lifecycle === "retired")
        await localDb.archiveRecords.put(record);
    }
    await localDb.archiveRecords.put({
      userId,
      organizationId,
      eventId,
      snapshotId,
      entityType: "event_archive_snapshot",
      entityId: markerId,
      recordSchemaVersion: 1,
      lifecycle: "active",
      fields: {
        event_id: eventId,
        snapshot_id: snapshotId,
        content_hash: parsed.hash,
      },
      fieldClocks: {},
      immutable: true,
      updatedAt: new Date().toISOString(),
    });
  });
  return true;
}

export async function readEventScopedRecords(
  userId: string,
  organizationId: string,
  eventId: string,
  entityType: string,
  includeRetired = false,
): Promise<CanonicalRecord[]> {
  const event = await localDb.canonicalRecords.get([
    userId,
    organizationId,
    "event",
    eventId,
  ]);
  const snapshotId = event?.fields.current_archive_snapshot_id;
  if (
    event?.fields.lifecycle !== "archived" ||
    typeof snapshotId !== "string"
  ) {
    return readVisibleRecords(
      userId,
      organizationId,
      entityType,
      includeRetired,
    );
  }
  const records = await localDb.archiveRecords
    .where("[userId+organizationId+eventId+snapshotId]")
    .equals([userId, organizationId, eventId, snapshotId])
    .toArray();
  return records.filter(
    (record) =>
      record.entityType === entityType &&
      (includeRetired || record.lifecycle === "active"),
  );
}
