import { expect, test } from "@playwright/test";

const organizationId = "5ce17d2f-8365-4b1f-a80b-34d10425d51c";
const eventId = "3d8b2b21-c378-4574-9e46-9338c81305ef";
const userId = "a6a58bd6-214e-49af-8fae-e5f974bf8e08";

test("an administrator explicitly confirms a guarded online event archive", async ({
  page,
}) => {
  await page.addInitScript(() => {
    window.COOKOPS_RUNTIME_CONFIG = { authentication: { provider: "dummy" } };
  });
  await page.route("**/auth/**", async (route) => {
    if (new URL(route.request().url()).pathname === "/auth/session")
      return route.fulfill({
        contentType: "application/json",
        body: JSON.stringify({
          id: "a6a58bd6-214e-49af-8fae-e5f974bf8e08",
          display_name: "Alice Admin",
          verified_email: "alice@example.test",
        }),
      });
    await route.fulfill({ status: 404 });
  });
  await page.route("**/api/v1/sync/bootstrap", (route) =>
    route.fulfill({
      contentType: "application/json",
      body: JSON.stringify({
        sync_schema_version: 1,
        server_time: "2026-08-07T12:00:00.000Z",
        cursor: "opaque-cursor",
        records: [
          {
            organization_id: organizationId,
            entity_id: organizationId,
            entity_kind: "organization",
            operation: "upsert",
            payload: {
              record_schema_version: 1,
              record: { id: organizationId, default_currency: "CZK" },
            },
          },
          {
            organization_id: organizationId,
            entity_id: organizationId,
            entity_kind: "organization_capabilities",
            operation: "upsert",
            payload: {
              record_schema_version: 1,
              record: {
                id: organizationId,
                actor_user_id: userId,
                can_manage_organization: true,
              },
            },
          },
          {
            organization_id: organizationId,
            entity_id: eventId,
            entity_kind: "event",
            operation: "upsert",
            payload: {
              record_schema_version: 1,
              record: {
                id: eventId,
                organization_id: organizationId,
                name: "Letní vaření",
                start_date: "2026-08-10",
                end_date: "2026-08-10",
                base_expected_attendance: 24,
                budget_amount: "0",
                currency: "CZK",
                lifecycle: "active",
                archived_at: null,
              },
            },
          },
        ],
      }),
    }),
  );
  await page.goto(`/organizations/${organizationId}/events`);
  const card = page.getByRole("article");
  await card.getByRole("button", { name: "Archivovat akci" }).click();
  await expect(
    card.getByText(
      "Archivace vytvoří neměnný historický záznam. Aktivní plán už nepůjde upravovat.",
    ),
  ).toBeVisible();
  await card.getByRole("button", { name: "Potvrdit archivaci" }).click();
  await expect(card.getByText("Aktivní", { exact: true })).toBeVisible();
  await expect(card.getByLabel("Očekávaná účast")).toBeVisible();
  await expect
    .poll(() =>
      page.evaluate(
        () => document.documentElement.scrollWidth <= window.innerWidth,
      ),
    )
    .toBe(true);
});

test("reactivates an archived event only after the server confirms it", async ({
  page,
}) => {
  await page.addInitScript(() => {
    window.COOKOPS_RUNTIME_CONFIG = { authentication: { provider: "dummy" } };
  });
  await page.route("**/auth/**", async (route) => {
    if (new URL(route.request().url()).pathname === "/auth/session")
      return route.fulfill({
        contentType: "application/json",
        body: JSON.stringify({
          id: userId,
          display_name: "Alice Admin",
          verified_email: "alice@example.test",
        }),
      });
    await route.fulfill({ status: 404 });
  });
  const snapshotId = "archive-snapshot-2026-08-10";
  const archived = {
    id: eventId,
    organization_id: organizationId,
    name: "Letní vaření",
    start_date: "2026-08-10",
    end_date: "2026-08-10",
    base_expected_attendance: 24,
    budget_amount: "0",
    currency: "CZK",
    lifecycle: "archived",
    archived_at: "2026-08-09T12:00:00.000Z",
    current_archive_snapshot_id: snapshotId,
  };
  const active = { ...archived, lifecycle: "active", archived_at: null };
  const record = (value: object, sequence = 1) => ({
    organization_id: organizationId,
    entity_id: eventId,
    entity_kind: "event",
    operation: "upsert",
    sequence,
    payload: { record_schema_version: 1, record: value },
  });
  await page.route("**/api/v1/sync/bootstrap", (route) =>
    route.fulfill({
      contentType: "application/json",
      body: JSON.stringify({
        sync_schema_version: 1,
        server_time: "2026-08-10T12:00:00.000Z",
        cursor: "opaque-cursor",
        records: [
          {
            organization_id: organizationId,
            entity_id: organizationId,
            entity_kind: "organization",
            operation: "upsert",
            payload: {
              record_schema_version: 1,
              record: { id: organizationId, default_currency: "CZK" },
            },
          },
          {
            organization_id: organizationId,
            entity_id: organizationId,
            entity_kind: "organization_capabilities",
            operation: "upsert",
            payload: {
              record_schema_version: 1,
              record: {
                id: organizationId,
                actor_user_id: userId,
                can_manage_organization: true,
              },
            },
          },
          record(archived),
        ],
      }),
    }),
  );
  let serverResolved = false;
  const requestTrace: string[] = [];
  let pushMutationId: string | undefined;
  await page.route("**/api/v1/sync/pull", async (route) => {
    requestTrace.push(serverResolved ? "pull:active" : "pull:archived");
    await route.fulfill({
      contentType: "application/json",
      body: JSON.stringify({
        sync_schema_version: 1,
        server_time: "2026-08-10T12:00:01.000Z",
        status: "ok",
        next_cursor: "active-cursor",
        transaction_groups: [
          { records: [record(serverResolved ? active : archived, 2)] },
        ],
      }),
    });
  });

  let releasePush!: () => void;
  let resolvePushSeen!: () => void;
  const pushSeen = new Promise<void>((resolve) => {
    resolvePushSeen = resolve;
  });
  await page.route("**/api/v1/sync/push", async (route) => {
    requestTrace.push("push:received");
    const body = route.request().postDataJSON() as {
      commands: Array<{
        mutation_id: string;
        command_kind: string;
        payload: Record<string, unknown>;
      }>;
    };
    expect(body.commands).toHaveLength(1);
    pushMutationId = body.commands[0].mutation_id;
    expect(body.commands[0]).toMatchObject({
      command_kind: "event.lifecycle",
      payload: { event_id: eventId, operation: "reactivate" },
    });
    expect(body.commands[0].payload).not.toHaveProperty("actor_user_id");
    expect(body.commands[0].payload).not.toHaveProperty("role");
    resolvePushSeen();
    await new Promise<void>((done) => {
      releasePush = done;
    });
    await route.fulfill({
      contentType: "application/json",
      body: JSON.stringify({
        sync_schema_version: 1,
        server_time: "2026-08-10T12:00:01.000Z",
        change_cursor: "active-cursor",
        clock_skew_warning: null,
        outcomes: [
          {
            mutation_id: body.commands[0].mutation_id,
            command_kind: "event.lifecycle",
            status: "accepted",
            error: null,
          },
        ],
      }),
    });
    requestTrace.push("push:accepted");
    serverResolved = true;
  });

  await page.goto(`/organizations/${organizationId}/events`);
  const card = page.getByRole("article");
  await expect(card.getByText("Archivovaná", { exact: true })).toBeVisible();
  await expect(
    card.getByRole("button", { name: "Znovu aktivovat akci" }),
  ).toBeVisible();
  await expect(card.getByLabel("Očekávaná účast")).toHaveCount(0);
  await card.getByRole("button", { name: "Znovu aktivovat akci" }).click();
  await card.getByRole("button", { name: "Potvrdit obnovení" }).click();
  await pushSeen;

  const pending = await page.evaluate(async () => {
    const request = indexedDB.open("cookops");
    return await new Promise<unknown>((resolve, reject) => {
      request.onerror = () => reject(request.error);
      request.onsuccess = () => {
        const transaction = request.result.transaction("outbox");
        const getAll = transaction.objectStore("outbox").getAll();
        getAll.onerror = () => reject(getAll.error);
        getAll.onsuccess = () => resolve(getAll.result);
      };
    });
  });
  const pendingCommands = (pending as Array<Record<string, unknown>>).filter(
    (command) =>
      command.commandType === "event.lifecycle" &&
      command.userId === userId &&
      command.organizationId === organizationId &&
      command.state === "pending" &&
      JSON.stringify(command.payload) ===
        JSON.stringify({ event_id: eventId, operation: "reactivate" }),
  );
  expect(pendingCommands).toHaveLength(1);
  const pendingCommandId = pendingCommands[0].id;
  expect(pendingCommandId).toMatch(/^[0-9a-f-]{36}$/);
  expect(pushMutationId).toBe(pendingCommandId);
  const beforeResolution = await page.evaluate(
    async ({ userId, organizationId, eventId }) => {
      const request = indexedDB.open("cookops");
      return await new Promise<unknown>((resolve, reject) => {
        request.onerror = () => reject(request.error);
        request.onsuccess = () => {
          const transaction = request.result.transaction([
            "canonicalRecords",
            "optimisticOverlays",
          ]);
          const canonical = transaction
            .objectStore("canonicalRecords")
            .get([userId, organizationId, "event", eventId]);
          const overlays = transaction
            .objectStore("optimisticOverlays")
            .getAll();
          transaction.oncomplete = () =>
            resolve({ canonical: canonical.result, overlays: overlays.result });
          transaction.onerror = () => reject(transaction.error);
        };
      });
    },
    { userId, organizationId, eventId },
  );
  expect(beforeResolution).toMatchObject({
    canonical: {
      lifecycle: "retired",
      fields: {
        lifecycle: "archived",
        current_archive_snapshot_id: snapshotId,
      },
    },
    overlays: [],
  });
  await expect(card.getByText("Archivovaná", { exact: true })).toBeVisible();
  await expect(card.getByLabel("Očekávaná účast")).toHaveCount(0);

  releasePush();
  await expect(card.getByText("Aktivní", { exact: true })).toBeVisible();
  await expect(card.getByLabel("Očekávaná účast")).toBeVisible();
  const canonical = await page.evaluate(
    async ({ userId, organizationId, eventId }) => {
      const request = indexedDB.open("cookops");
      return await new Promise<unknown>((resolve, reject) => {
        request.onerror = () => reject(request.error);
        request.onsuccess = () => {
          const transaction = request.result.transaction("canonicalRecords");
          const get = transaction
            .objectStore("canonicalRecords")
            .get([userId, organizationId, "event", eventId]);
          get.onerror = () => reject(get.error);
          get.onsuccess = () => resolve(get.result);
        };
      });
    },
    { userId, organizationId, eventId },
  );
  expect(canonical).toMatchObject({
    fields: { lifecycle: "active", current_archive_snapshot_id: snapshotId },
  });
  const acceptedAt = requestTrace.indexOf("push:accepted");
  const activePullAt = requestTrace.indexOf("pull:active");
  expect(acceptedAt).toBeGreaterThanOrEqual(0);
  expect(activePullAt).toBeGreaterThan(acceptedAt);
  const remaining = await page.evaluate(async () => {
    const request = indexedDB.open("cookops");
    return await new Promise<unknown>((resolve, reject) => {
      request.onerror = () => reject(request.error);
      request.onsuccess = () => {
        const getAll = request.result
          .transaction("outbox")
          .objectStore("outbox")
          .getAll();
        getAll.onerror = () => reject(getAll.error);
        getAll.onsuccess = () => resolve(getAll.result);
      };
    });
  });
  expect(
    (remaining as Array<Record<string, unknown>>).filter(
      (command) => command.id === pendingCommandId,
    ),
  ).toHaveLength(0);
  await expect
    .poll(() =>
      page.evaluate(
        () => document.documentElement.scrollWidth <= window.innerWidth,
      ),
    )
    .toBe(true);
});
