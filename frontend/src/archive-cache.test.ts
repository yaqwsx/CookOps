import { beforeEach, describe, expect, it, vi } from "vitest";

import { ensureArchivedEventCached } from "./archive-cache";
import { localDb } from "./local-db";

const userId = "11111111-1111-4111-8111-111111111111";
const organizationId = "22222222-2222-4222-8222-222222222222";
const eventId = "33333333-3333-4333-8333-333333333333";
const snapshotId = "44444444-4444-4444-8444-444444444444";
const dayId = "55555555-5555-4555-8555-555555555555";
const revisionId = "66666666-6666-4666-8666-666666666666";
const scheduledId = "77777777-7777-4777-8777-777777777777";
const recipeVersionId = "88888888-8888-4888-8888-888888888888";
const recipeTagId = "99999999-9999-4999-8999-999999999999";
const ingredientVersionId = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
const dietaryTagId = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb";
const listId = "cccccccc-cccc-4ccc-8ccc-cccccccccccc";
const recipeId = "dddddddd-dddd-4ddd-8ddd-dddddddddddd";
const ingredientId = "ffffffff-ffff-4fff-8fff-ffffffffffff";
const overrideId = "12121212-1212-4121-8121-121212121212";
const priceId = "13131313-1313-4131-8131-131313131313";
const missingId = "15151515-1515-4151-8151-151515151515";

const emptyCollections = Object.fromEntries(
  [
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
  ].map((key) => [key, []]),
);

const payload = {
  schema_version: 1,
  event: {
    id: eventId,
    organization_id: organizationId,
    lifecycle: "active",
  },
  ...emptyCollections,
  event_days: [
    { id: dayId, event_id: eventId, calendar_date: "2026-08-16", note: null },
  ],
  shopping_revision_sources: [
    {
      generation_revision_id: revisionId,
      scheduled_recipe_id: scheduledId,
      shopping_list_id: listId,
      event_id: eventId,
      organization_id: organizationId,
    },
  ],
  shopping_lists: [
    { id: listId, event_id: eventId, organization_id: organizationId },
  ],
  shopping_generation_revisions: [
    {
      id: revisionId,
      shopping_list_id: listId,
      event_id: eventId,
      organization_id: organizationId,
    },
  ],
  scheduled_recipes: [
    {
      id: scheduledId,
      event_id: eventId,
      organization_id: organizationId,
      recipe_id: recipeId,
      recipe_version_id: recipeVersionId,
    },
  ],
  recipes: [{ id: recipeId, organization_id: organizationId }],
  ingredients: [{ id: ingredientId, organization_id: organizationId }],
  dietary_tags: [{ id: dietaryTagId, organization_id: organizationId }],
  recipe_tags: [{ id: recipeTagId, organization_id: organizationId }],
  recipe_versions: [
    {
      id: recipeVersionId,
      recipe_id: recipeId,
      organization_id: organizationId,
    },
  ],
  ingredient_versions: [
    {
      id: ingredientVersionId,
      ingredient_id: ingredientId,
      organization_id: organizationId,
    },
  ],
  recipe_version_tags: [
    {
      recipe_version_id: recipeVersionId,
      recipe_tag_id: recipeTagId,
      organization_id: organizationId,
    },
  ],
  ingredient_version_dietary_tags: [
    {
      ingredient_version_id: ingredientVersionId,
      dietary_tag_id: dietaryTagId,
      organization_id: organizationId,
    },
  ],
};

function response(body: unknown, ok = true) {
  return new Response(JSON.stringify(body), {
    status: ok ? 200 : 503,
    headers: { "content-type": "application/json" },
  });
}

async function hash(value: unknown) {
  const stable = (item: unknown): string =>
    Array.isArray(item)
      ? `[${item.map(stable).join(",")}]`
      : item && typeof item === "object"
        ? `{${Object.keys(item)
            .sort()
            .map(
              (key) =>
                `${JSON.stringify(key)}:${stable((item as Record<string, unknown>)[key])}`,
            )
            .join(",")}}`
        : JSON.stringify(item);
  const text = stable(value);
  const digest = await crypto.subtle.digest(
    "SHA-256",
    new TextEncoder().encode(text),
  );
  return [...new Uint8Array(digest)]
    .map((part) => part.toString(16).padStart(2, "0"))
    .join("");
}

async function seed() {
  await localDb.canonicalRecords.put({
    userId,
    organizationId,
    entityType: "event",
    entityId: eventId,
    recordSchemaVersion: 1,
    lifecycle: "retired",
    fields: {
      id: eventId,
      organization_id: organizationId,
      lifecycle: "archived",
      current_archive_snapshot_id: snapshotId,
      base_expected_attendance: 4,
      name: "Archive",
      start_date: "2026-08-16",
      end_date: "2026-08-16",
    },
    fieldClocks: {},
    immutable: false,
    updatedAt: "2026-08-16T00:00:00Z",
  });
}

describe("archived event cache", () => {
  beforeEach(async () => {
    await localDb.canonicalRecords.clear();
    await localDb.archiveRecords.clear();
    await localDb.optimisticOverlays.clear();
    await seed();
  });

  it("fetches once, verifies, and atomically caches immutable records", async () => {
    const fetcher = vi.fn().mockResolvedValue(
      response({
        archive_schema_version: 1,
        content_hash: await hash(payload),
        payload,
      }),
    );
    await expect(
      ensureArchivedEventCached(userId, organizationId, eventId, fetcher),
    ).resolves.toBe(true);
    await expect(
      localDb.archiveRecords.get([
        userId,
        organizationId,
        eventId,
        snapshotId,
        "event_day",
        dayId,
      ]),
    ).resolves.toMatchObject({
      immutable: true,
      fields: payload.event_days[0],
    });
    for (const [kind, id] of [
      ["shopping_revision_source", `${revisionId}:${scheduledId}`],
      ["recipe_version_tag", `${recipeVersionId}:${recipeTagId}`],
      [
        "ingredient_version_dietary_tag",
        `${ingredientVersionId}:${dietaryTagId}`,
      ],
    ]) {
      await expect(
        localDb.archiveRecords.get([
          userId,
          organizationId,
          eventId,
          snapshotId,
          kind,
          id,
        ]),
      ).resolves.toBeDefined();
    }
    fetcher.mockRejectedValue(new Error("offline"));
    await expect(
      ensureArchivedEventCached(userId, organizationId, eventId, fetcher),
    ).resolves.toBe(true);
    expect(fetcher).toHaveBeenCalledTimes(1);
    await localDb.canonicalRecords.clear();
    await expect(
      localDb.archiveRecords.get([
        userId,
        organizationId,
        eventId,
        snapshotId,
        "event_day",
        dayId,
      ]),
    ).resolves.toBeDefined();
  });

  it("rejects a bad hash and leaves the local database unchanged", async () => {
    const fetcher = vi.fn().mockResolvedValue(
      response({
        archive_schema_version: 1,
        content_hash: "0".repeat(64),
        payload,
      }),
    );
    await expect(
      ensureArchivedEventCached(userId, organizationId, eventId, fetcher),
    ).rejects.toThrow("integrity");
    await expect(
      localDb.archiveRecords.get([
        userId,
        organizationId,
        eventId,
        snapshotId,
        "event_day",
        dayId,
      ]),
    ).resolves.toBeUndefined();
  });

  it("rejects missing collections without leaving a marker", async () => {
    const invalidPayload = { schema_version: 1, event: payload.event };
    const fetcher = vi.fn().mockResolvedValue(
      response({
        archive_schema_version: 1,
        content_hash: await hash(invalidPayload),
        payload: invalidPayload,
      }),
    );
    await expect(
      ensureArchivedEventCached(userId, organizationId, eventId, fetcher),
    ).rejects.toThrow("Invalid archive response");
    await expect(
      localDb.archiveRecords
        .toArray()
        .then(
          (records) =>
            records.filter(
              (record) => record.entityType === "event_archive_snapshot",
            ).length,
        ),
    ).resolves.toBe(0);
  });

  it("rejects a missing organization scope before writing", async () => {
    const invalidPayload = { ...payload, recipes: [{ id: recipeId }] };
    const fetcher = vi
      .fn()
      .mockResolvedValue(
        response({
          archive_schema_version: 1,
          content_hash: await hash(invalidPayload),
          payload: invalidPayload,
        }),
      );
    await expect(
      ensureArchivedEventCached(userId, organizationId, eventId, fetcher),
    ).rejects.toThrow("Invalid archive");
    await expect(localDb.archiveRecords.toArray()).resolves.toHaveLength(0);
  });

  it.each([
    [
      "override parent graph",
      {
        ...payload,
        scheduled_ingredient_overrides: [
          {
            id: overrideId,
            event_id: eventId,
            organization_id: organizationId,
            scheduled_recipe_id: missingId,
            ingredient_id: ingredientId,
            ingredient_version_id: ingredientVersionId,
          },
        ],
      },
    ],
    [
      "price ingredient graph",
      {
        ...payload,
        event_ingredient_prices: [
          {
            id: priceId,
            event_id: eventId,
            organization_id: organizationId,
            ingredient_id: missingId,
          },
        ],
      },
    ],
    [
      "recipe tag graph",
      {
        ...payload,
        recipe_version_tags: [
          {
            recipe_version_id: recipeVersionId,
            recipe_tag_id: missingId,
            organization_id: organizationId,
          },
        ],
      },
    ],
    [
      "optional snapshot pointer",
      {
        ...payload,
        event_ingredient_prices: [
          {
            id: priceId,
            event_id: eventId,
            organization_id: organizationId,
            ingredient_id: ingredientId,
            current_snapshot_id: missingId,
          },
        ],
      },
    ],
    [
      "generation parent pointer",
      {
        ...payload,
        shopping_generation_revisions: [
          {
            ...payload.shopping_generation_revisions[0],
            parent_revision_id: missingId,
          },
        ],
      },
    ],
  ])("rejects %s atomically", async (_name, invalidPayload) => {
    const fetcher = vi.fn().mockResolvedValue(
      response({
        archive_schema_version: 1,
        content_hash: await hash(invalidPayload),
        payload: invalidPayload,
      }),
    );
    await expect(
      ensureArchivedEventCached(userId, organizationId, eventId, fetcher),
    ).rejects.toThrow("Invalid archive");
    await expect(localDb.archiveRecords.toArray()).resolves.toHaveLength(0);
  });

  it("does not replace an active canonical record", async () => {
    await localDb.canonicalRecords.put({
      userId,
      organizationId,
      entityType: "event_day",
      entityId: dayId,
      recordSchemaVersion: 1,
      lifecycle: "active",
      fields: {
        id: dayId,
        event_id: eventId,
        calendar_date: "old",
        note: null,
      },
      fieldClocks: {},
      immutable: false,
      updatedAt: "2026-08-15T00:00:00Z",
    });
    const fetcher = vi.fn().mockResolvedValue(
      response({
        archive_schema_version: 1,
        content_hash: await hash(payload),
        payload,
      }),
    );
    await ensureArchivedEventCached(userId, organizationId, eventId, fetcher);
    await expect(
      localDb.canonicalRecords.get([
        userId,
        organizationId,
        "event_day",
        dayId,
      ]),
    ).resolves.toMatchObject({
      immutable: false,
      fields: { calendar_date: "old" },
    });
  });
});
