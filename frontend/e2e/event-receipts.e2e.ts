import { expect, test } from "@playwright/test";

const organizationId = "5ce17d2f-8365-4b1f-a80b-34d10425d51c";
const eventId = "3d8b2b21-c378-4574-9e46-9338c81305ef";

test("keeps a normalized receipt photo queued offline", async ({ page }) => {
  let serverReceipt:
    | { id: string; title: string; totalAmount: string }
    | undefined;
  let attachmentReady = false;
  let putAttempts = 0;
  let mediaUploadBytes = 0;
  let serverAttachmentId = "";
  const createdAttachmentIds: string[] = [];
  const createdMutationIds: string[] = [];
  let uploadTicketCalls = 0;
  const finalizedAttachmentIds: string[] = [];
  await page.addInitScript(() => {
    window.COOKOPS_RUNTIME_CONFIG = { authentication: { provider: "dummy" } };
  });
  await page.route("**/auth/**", async (route) => {
    if (new URL(route.request().url()).pathname === "/auth/session") {
      await route.fulfill({
        contentType: "application/json",
        body: JSON.stringify({
          id: "a6a58bd6-214e-49af-8fae-e5f974bf8e08",
          display_name: "Alice Member",
          verified_email: "alice@example.test",
        }),
      });
      return;
    }
    await route.fulfill({ status: 404 });
  });
  await page.route("**/api/v1/organizations", (route) =>
    route.fulfill({
      contentType: "application/json",
      body: JSON.stringify({
        organizations: [
          { id: organizationId, name: "CookOps test organization" },
        ],
      }),
    }),
  );
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
              record: {
                id: organizationId,
                default_currency: "CZK",
                retired_at: null,
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
                base_expected_attendance: 12,
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
  await page.route("**/api/v1/sync/push", async (route) => {
    const body = route.request().postDataJSON() as {
      commands?: {
        mutation_id?: string;
        command_kind?: string;
        payload?: Record<string, unknown>;
      }[];
    };
    const command = body.commands?.find(
      (item) => item.command_kind === "receipt.create",
    );
    if (command?.payload) {
      serverReceipt = {
        id: String(command.payload.receipt_id),
        title: String(command.payload.title),
        totalAmount: String(command.payload.total_amount),
      };
    }
    await route.fulfill({
      contentType: "application/json",
      body: JSON.stringify({
        sync_schema_version: 1,
        server_time: "2026-08-07T12:00:01.000Z",
        change_cursor: "cursor-after-create",
        clock_skew_warning: null,
        outcomes: (body.commands ?? []).map((item, index) => ({
          mutation_id: String(body.commands?.[index]?.mutation_id),
          command_kind: item.command_kind,
          status: "accepted",
          error: null,
        })),
      }),
    });
  });
  await page.route("**/api/v1/sync/pull", async (route) => {
    const records = serverReceipt
      ? [
          {
            organization_id: organizationId,
            entity_id: serverReceipt.id,
            entity_kind: "receipt",
            operation: "upsert",
            sequence: 1,
            payload: {
              record_schema_version: 1,
              record: {
                id: serverReceipt.id,
                organization_id: organizationId,
                event_id: eventId,
                title: serverReceipt.title,
                total_amount: serverReceipt.totalAmount,
                receipt_date: null,
                note: null,
                currency: "CZK",
                retired_at: null,
              },
            },
          },
          ...(attachmentReady
            ? [
                {
                  organization_id: organizationId,
                  entity_id: serverAttachmentId,
                  entity_kind: "receipt_attachment",
                  operation: "upsert",
                  sequence: 2,
                  payload: {
                    record_schema_version: 1,
                    record: {
                      id: serverAttachmentId,
                      organization_id: organizationId,
                      event_id: eventId,
                      receipt_id: serverReceipt.id,
                      storage_state: "ready",
                      media_type: "image/jpeg",
                      retired_at: null,
                    },
                  },
                },
              ]
            : []),
        ]
      : [];
    await route.fulfill({
      contentType: "application/json",
      body: JSON.stringify({
        sync_schema_version: 1,
        server_time: "2026-08-07T12:00:02.000Z",
        status: "ok",
        next_cursor: attachmentReady
          ? "cursor-after-media"
          : "cursor-after-push",
        transaction_groups: records.length ? [{ records }] : [],
      }),
    });
  });
  await page.route("**/media/receipt-attachments", async (route) => {
    if (route.request().method() !== "POST") return route.continue();
    const body = route.request().postDataJSON() as {
      attachment_id?: string;
      mutation_id?: string;
    };
    if (serverAttachmentId && body.attachment_id !== serverAttachmentId)
      return route.fulfill({ status: 409 });
    if (!createdAttachmentIds.includes(String(body.attachment_id))) {
      createdAttachmentIds.push(String(body.attachment_id));
    }
    createdMutationIds.push(String(body.mutation_id));
    serverAttachmentId = String(body.attachment_id);
    await route.fulfill({
      contentType: "application/json",
      body: JSON.stringify({
        attachment_id: body.attachment_id,
        ticket_secret: "ticket-secret-for-test",
      }),
    });
  });
  await page.route("**/media/receipt-attachments/*/upload-ticket", (route) => {
    uploadTicketCalls += 1;
    return route.fulfill({ status: 404 });
  });
  await page.route("**/media/receipt-attachments/*/status**", async (route) => {
    const attachmentId = route.request().url().split("/").at(-2);
    await route.fulfill({
      contentType: "application/json",
      body: JSON.stringify({
        attachment_id: attachmentId,
        storage_state: attachmentReady ? "ready" : "pending",
        content_hash: null,
        source_content_hash: attachmentReady
          ? await page.evaluate(async () => {
              const rows = await new Promise<Record<string, unknown>[]>(
                (resolve, reject) => {
                  const request = indexedDB.open("cookops");
                  request.onsuccess = () => {
                    const database = request.result;
                    const get = database
                      .transaction("pendingUploads")
                      .objectStore("pendingUploads")
                      .getAll();
                    get.onsuccess = async () => {
                      const row = get.result[0] as { blob?: Blob } | undefined;
                      database.close();
                      resolve(
                        row?.blob
                          ? [
                              Array.from(
                                new Uint8Array(
                                  await crypto.subtle.digest(
                                    "SHA-256",
                                    await row.blob.arrayBuffer(),
                                  ),
                                ),
                              )
                                .map((byte) =>
                                  byte.toString(16).padStart(2, "0"),
                                )
                                .join(""),
                            ]
                          : [],
                      );
                    };
                    get.onerror = () => reject(get.error);
                  };
                  request.onerror = () => reject(request.error);
                },
              );
              return rows[0];
            })
          : null,
        byte_size: attachmentReady ? mediaUploadBytes : null,
        source_byte_size: attachmentReady ? mediaUploadBytes : null,
        media_type: "image/jpeg",
        retired: false,
      }),
    });
  });
  await page.route("**/media/receipt-attachments/*", async (route) => {
    if (route.request().method() !== "PUT") return route.continue();
    putAttempts += 1;
    const attachmentId = route.request().url().split("/").at(-1);
    if (attachmentId) finalizedAttachmentIds.push(attachmentId);
    mediaUploadBytes = route.request().postDataBuffer()?.length ?? 0;
    if (putAttempts === 1) return route.fulfill({ status: 503 });
    attachmentReady = true;
    await route.fulfill({
      contentType: "application/json",
      body: JSON.stringify({ attachment_id: serverAttachmentId }),
    });
  });
  await page.route("**/media/receipt-attachments/*?*", (route) => {
    const requestUrl = new URL(route.request().url());
    if (
      route.request().method() !== "GET" ||
      requestUrl.pathname.endsWith("/status")
    )
      return route.continue();
    return route.fulfill({
      contentType: "image/jpeg",
      body: Buffer.from(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVQIHWP4z8DwHwAFgAI/ScL3hwAAAABJRU5ErkJggg==",
        "base64",
      ),
    });
  });

  await page.goto(`/organizations/${organizationId}/events`);
  await page.getByRole("button", { name: "Otevřít plán" }).click();
  await page.getByRole("link", { name: "Účtenky" }).click();
  await expect(page.getByRole("heading", { name: "Účtenky" })).toBeVisible();
  await page.context().setOffline(true);
  await page.evaluate(() => window.dispatchEvent(new Event("offline")));
  await page.getByLabel("Obchod nebo stručný název").fill("Pekárna");
  await page.getByLabel("Celková částka").fill("12.50");
  await page.getByRole("button", { name: "Uložit účtenku" }).click();
  await expect(page.getByRole("heading", { name: "Pekárna" })).toBeVisible();
  await expect(
    page
      .getByRole("listitem")
      .filter({ has: page.getByRole("heading", { name: "Pekárna" }) })
      .getByText("12.50 CZK", { exact: true }),
  ).toBeVisible();
  const picker = page.getByLabel("Přidat fotografii účtenky");
  await expect(picker).toBeVisible();
  await picker.setInputFiles({
    name: "receipt.png",
    mimeType: "image/png",
    buffer: Buffer.from(
      "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVQIHWP4z8DwHwAFgAI/ScL3hwAAAABJRU5ErkJggg==",
      "base64",
    ),
  });
  await expect(
    page.getByRole("button", { name: "Odstranit fotografii" }),
  ).toBeVisible();
  await expect
    .poll(() =>
      page.evaluate(
        () => document.documentElement.scrollWidth <= window.innerWidth,
      ),
    )
    .toBe(true);

  const identity = await page.evaluate(async () => {
    const database = await new Promise<IDBDatabase>((resolve, reject) => {
      const request = indexedDB.open("cookops");
      request.onsuccess = () => resolve(request.result);
      request.onerror = () => reject(request.error);
    });
    const rows = await new Promise<Record<string, unknown>[]>(
      (resolve, reject) => {
        const request = database
          .transaction("pendingUploads")
          .objectStore("pendingUploads")
          .getAll();
        request.onsuccess = () => resolve(request.result);
        request.onerror = () => reject(request.error);
      },
    );
    database.close();
    return {
      receiptId: document
        .querySelector(".receipt-item")
        ?.getAttribute("data-receipt-id"),
      attachmentId: rows[0]?.attachmentId as string | undefined,
      blobType: (rows[0]?.blob as Blob | undefined)?.type,
      blobSize: (rows[0]?.blob as Blob | undefined)?.size,
      createMutationId: rows[0]?.createMutationId as string | undefined,
    };
  });
  expect(identity.receiptId).toBeTruthy();
  expect(identity.attachmentId).toMatch(/^[0-9a-f-]{36}$/i);
  expect(identity.blobType).toBe("image/jpeg");
  expect(identity.blobSize).toBeGreaterThan(0);

  await page.reload();
  await expect(page.getByRole("heading", { name: "Pekárna" })).toBeVisible();
  await expect
    .poll(() =>
      page.evaluate(async () => {
        const database = await new Promise<IDBDatabase>((resolve, reject) => {
          const request = indexedDB.open("cookops");
          request.onsuccess = () => resolve(request.result);
          request.onerror = () => reject(request.error);
        });
        const rows = await new Promise<Record<string, unknown>[]>(
          (resolve, reject) => {
            const request = database
              .transaction("pendingUploads")
              .objectStore("pendingUploads")
              .getAll();
            request.onsuccess = () => resolve(request.result);
            request.onerror = () => reject(request.error);
          },
        );
        database.close();
        return rows.map((row) => [row.receiptId, row.attachmentId]);
      }),
    )
    .toEqual([[identity.receiptId, identity.attachmentId]]);

  await page.context().setOffline(false);
  await page.evaluate(() => window.dispatchEvent(new Event("online")));
  await expect.poll(() => putAttempts).toBe(1);
  expect(uploadTicketCalls).toBe(0);
  await expect
    .poll(() =>
      page.evaluate(async () => {
        const database = await new Promise<IDBDatabase>((resolve, reject) => {
          const request = indexedDB.open("cookops");
          request.onsuccess = () => resolve(request.result);
          request.onerror = () => reject(request.error);
        });
        const rows = await new Promise<Record<string, unknown>[]>(
          (resolve, reject) => {
            const request = database
              .transaction("pendingUploads")
              .objectStore("pendingUploads")
              .getAll();
            request.onsuccess = () => resolve(request.result);
            request.onerror = () => reject(request.error);
          },
        );
        database.close();
        return rows[0]?.state;
      }),
    )
    .toBe("pending");

  await page.evaluate(() => window.dispatchEvent(new Event("online")));
  await expect.poll(() => putAttempts).toBe(2);
  expect(createdAttachmentIds).toEqual([identity.attachmentId]);
  expect(createdMutationIds).toEqual([
    identity.createMutationId,
    identity.createMutationId,
  ]);
  expect(finalizedAttachmentIds).toEqual([
    identity.attachmentId,
    identity.attachmentId,
  ]);
  await expect(
    page.getByRole("img", { name: "Fotografie účtenky" }),
  ).toBeVisible();
  await expect
    .poll(() =>
      page.evaluate(async () => {
        const database = await new Promise<IDBDatabase>((resolve, reject) => {
          const request = indexedDB.open("cookops");
          request.onsuccess = () => resolve(request.result);
          request.onerror = () => reject(request.error);
        });
        const [uploads, attachments] = await Promise.all(
          ["pendingUploads", "canonicalRecords"].map(
            (store) =>
              new Promise<Record<string, unknown>[]>((resolve, reject) => {
                const request = database
                  .transaction(store)
                  .objectStore(store)
                  .getAll();
                request.onsuccess = () => resolve(request.result);
                request.onerror = () => reject(request.error);
              }),
          ),
        );
        database.close();
        return {
          uploads: uploads.length,
          attachmentIds: attachments
            .filter((row) => row.entityType === "receipt_attachment")
            .map((row) => row.entityId),
          blobUrls: [...document.querySelectorAll("img")].map(
            (image) => image.src,
          ),
          ticketText: document.body.innerText.includes(
            "ticket-secret-for-test",
          ),
        };
      }),
    )
    .toEqual({
      uploads: 0,
      attachmentIds: [identity.attachmentId],
      blobUrls: [expect.stringContaining("/media/receipt-attachments/")],
      ticketText: false,
    });
});
