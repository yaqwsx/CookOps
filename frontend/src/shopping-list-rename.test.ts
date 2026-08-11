import { beforeEach, describe, expect, it } from "vitest";
import { localDb } from "./local-db";
import { queueShoppingList, queueShoppingListRename, replayShoppingListRename } from "./shopping-list";

const userId = "a6a58bd6-214e-49af-8fae-e5f974bf8e08";
const organizationId = "5ce17d2f-8365-4b1f-a80b-34d10425d51c";
const eventId = "6ce17d2f-8365-4b1f-a80b-34d10425d51c";
const listId = "7ce17d2f-8365-4b1f-a80b-34d10425d51c";

beforeEach(async () => {
  await Promise.all([localDb.canonicalRecords.clear(), localDb.optimisticOverlays.clear(), localDb.outbox.clear()]);
  await localDb.canonicalRecords.bulkAdd([
    { userId, organizationId, entityType: "event", entityId: eventId, recordSchemaVersion: 1, lifecycle: "active", fields: { id: eventId, organization_id: organizationId, name: "Event", start_date: "2026-08-10", end_date: "2026-08-10", base_expected_attendance: 1, lifecycle: "active" }, fieldClocks: {}, immutable: false, updatedAt: "2026-08-10T12:00:00.000000Z" },
    { userId, organizationId, entityType: "shopping_list", entityId: listId, recordSchemaVersion: 1, lifecycle: "active", fields: { id: listId, organization_id: organizationId, event_id: eventId, name: "Old" }, fieldClocks: {}, immutable: false, updatedAt: "2026-08-10T12:00:00.000000Z" },
  ]);
});

describe("offline shopping-list rename", () => {
  it("renames a newly created offline list and preserves create-before-rename ordering", async () => {
    await queueShoppingList(userId, organizationId, { eventId, name: "New", scheduledRecipeIds: [] });
    const create = await localDb.outbox.toCollection().first();
    const listId = create?.payload.shopping_list_id;
    expect(typeof listId).toBe("string");
    await queueShoppingListRename(userId, organizationId, { shoppingListId: listId as string, name: "  Café  " });
    const commands = await localDb.outbox.orderBy("createdAt").toArray();
    expect(commands.map((command) => command.commandType)).toEqual(["shopping_list.create", "shopping_list.rename"]);
    await expect(localDb.optimisticOverlays.get([userId, organizationId, "shopping_list", listId as string])).resolves.toMatchObject({ fields: { name: "Café" }, fieldClocks: { name: { mutationId: commands[1].id } } });
  });

  it("replays the ordered rename over the create overlay", async () => {
    await queueShoppingList(userId, organizationId, { eventId, name: "New", scheduledRecipeIds: [] });
    const create = await localDb.outbox.toCollection().first();
    const listId = create?.payload.shopping_list_id as string;
    await queueShoppingListRename(userId, organizationId, { shoppingListId: listId, name: "Café" });
    const rename = (await localDb.outbox.toArray()).find((command) => command.commandType === "shopping_list.rename");
    if (!rename) throw new Error("rename command missing");
    await replayShoppingListRename(userId, organizationId, { id: rename.id, actionAt: rename.actionAt, payload: rename.payload });
    await expect(localDb.optimisticOverlays.get([userId, organizationId, "shopping_list", listId])).resolves.toMatchObject({ fields: { name: "Café" } });
  });

  it("queues the canonical NFC name in the exact persisted shape", async () => {
    await queueShoppingListRename(userId, organizationId, { shoppingListId: listId, name: "  Cafe\u0301  " });
    await expect(localDb.outbox.toCollection().first()).resolves.toMatchObject({ commandType: "shopping_list.rename", payload: { shopping_list_id: listId, name: "Café" }, state: "pending" });
  });

  it("rejects stale, malformed, and archived-event commands", async () => {
    await localDb.canonicalRecords.update([userId, organizationId, "shopping_list", listId], { fieldClocks: { name: { winning_mutation_id: "00000000-0000-4000-8000-000000000001", winning_client_wall_time: "2026-08-10T12:00:00.000001Z" } } });
    await replayShoppingListRename(userId, organizationId, { id: "ffffffff-ffff-4fff-8fff-ffffffffffff", actionAt: "2026-08-10T12:00:00.000000Z", payload: { shopping_list_id: listId, name: "Stale" } });
    await replayShoppingListRename(userId, organizationId, { id: "8ce17d2f-8365-4b1f-a80b-34d10425d51c", actionAt: "not-a-time", payload: { shopping_list_id: listId, name: "Invalid" } });
    expect(await localDb.optimisticOverlays.count()).toBe(0);
    await localDb.canonicalRecords.update([userId, organizationId, "event", eventId], { lifecycle: "retired" });
    await expect(queueShoppingListRename(userId, organizationId, { shoppingListId: listId, name: "Blocked" })).rejects.toThrow("shopping_list");
    await replayShoppingListRename(userId, organizationId, { id: "9ce17d2f-8365-4b1f-a80b-34d10425d51c", actionAt: "2026-08-10T12:00:02.000000Z", payload: { shopping_list_id: listId, name: "Blocked" } });
    expect(await localDb.optimisticOverlays.count()).toBe(0);
  });
});
