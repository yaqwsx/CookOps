import { beforeEach, describe, expect, it, vi } from "vitest";

import { localDb } from "./local-db";
import { dispatchReceiptUploads, removeReceiptUpload } from "./receipt-media";

const userId = "a6a58bd6-214e-49af-8fae-e5f974bf8e08";
const organizationId = "5ce17d2f-8365-4b1f-a80b-34d10425d51c";
const receiptId = "6ce17d2f-8365-4b1f-a80b-34d10425d51c";
const attachmentId = "7ce17d2f-8365-4b1f-a80b-34d10425d51c";

async function upload() {
  const blob = await new Response("image", {
    headers: { "content-type": "image/jpeg" },
  }).blob();
  await localDb.pendingUploads.add({
    id: "upload",
    userId,
    organizationId,
    receiptId,
    attachmentId,
    createMutationId: "8ce17d2f-8365-4b1f-a80b-34d10425d51c",
    finalizeMutationId: "9ce17d2f-8365-4b1f-a80b-34d10425d51c",
    positionKey: "a",
    blob,
    createdAt: new Date().toISOString(),
    serverCreated: true,
    state: "pending",
  });
}

function status(
  state: "absent" | "pending" | "ready" | "failed",
  values: Partial<Record<"hash" | "size", string | number>> = {},
) {
  return Response.json({
    attachment_id: attachmentId,
    storage_state: state,
    content_hash: null,
    source_content_hash: values.hash ?? null,
    byte_size: null,
    source_byte_size: values.size ?? null,
    pixel_width: null,
    pixel_height: null,
    media_type: state === "absent" ? null : "image/jpeg",
    retired: false,
  });
}

describe("receipt upload removal", () => {
  beforeEach(() =>
    Promise.all([
      localDb.pendingUploads.clear(),
      localDb.browserInstallation.clear(),
    ]),
  );

  it("keeps the local photo on a rejected retirement", async () => {
    await upload();
    await expect(
      removeReceiptUpload(userId, "upload", {
        fetch: async (input) =>
          String(input).includes("/status?")
            ? status("pending")
            : (new Response(null, { status: 422 }) as Response),
      }),
    ).rejects.toThrow("422");
    await expect(localDb.pendingUploads.get("upload")).resolves.toMatchObject({
      state: "failed",
      failureReason: "removal_rejected",
    });
  });

  it("deletes only after authorized retirement", async () => {
    await upload();
    await removeReceiptUpload(userId, "upload", {
      fetch: async (input) =>
        String(input).includes("/status?")
          ? status("pending")
          : (new Response(null, { status: 204 }) as Response),
    });
    await expect(localDb.pendingUploads.get("upload")).resolves.toBeUndefined();
  });

  it("retires a server attachment after a create response was lost", async () => {
    await upload();
    await localDb.pendingUploads.update("upload", { serverCreated: false });
    const send = vi.fn(async (input: RequestInfo | URL) =>
      String(input).includes("/status?")
        ? status("ready")
        : (new Response(null, { status: 204 }) as Response),
    );

    await removeReceiptUpload(userId, "upload", { fetch: send });

    await expect(localDb.pendingUploads.get("upload")).resolves.toBeUndefined();
    expect(send).toHaveBeenCalledTimes(2);
  });

  it("retains bytes when the authorized status response is malformed", async () => {
    await upload();

    await expect(
      removeReceiptUpload(userId, "upload", {
        fetch: async () => new Response("not json"),
      }),
    ).rejects.toThrow("removal reconciliation required");

    await expect(localDb.pendingUploads.get("upload")).resolves.toMatchObject({
      state: "failed",
      failureReason: "removal_reconciliation_required",
    });
  });
});

async function hash(blob: Blob) {
  const digest = await crypto.subtle.digest(
    "SHA-256",
    await blob.arrayBuffer(),
  );
  return Array.from(new Uint8Array(digest), (byte) =>
    byte.toString(16).padStart(2, "0"),
  ).join("");
}

describe("receipt upload lost finalization recovery", () => {
  beforeEach(() =>
    Promise.all([
      localDb.pendingUploads.clear(),
      localDb.browserInstallation.clear(),
    ]),
  );

  it("discards the local blob after a successful finalized status reconciliation", async () => {
    await upload();
    const blob = (await localDb.pendingUploads.get("upload"))?.blob;
    const send = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url === "/media/receipt-attachments")
        return Response.json({
          attachment_id: attachmentId,
          ticket_secret: "ticket",
        });
      if (init?.method === "PUT")
        return Response.json({ storage_state: "ready" });
      if (url.startsWith(`/media/receipt-attachments/${attachmentId}/status?`))
        return Response.json({
          attachment_id: attachmentId,
          storage_state: "ready",
          content_hash: "0".repeat(64),
          source_content_hash: await hash(blob as Blob),
          byte_size: 1,
          source_byte_size: blob?.size,
          pixel_width: 1,
          pixel_height: 1,
          media_type: "image/jpeg",
          retired: false,
        });
      throw new Error(`unexpected ${url}`);
    });

    await dispatchReceiptUploads(userId, organizationId, { fetch: send });

    await expect(localDb.pendingUploads.get("upload")).resolves.toBeUndefined();
    expect(send).toHaveBeenCalledTimes(3);
  });

  it("removes only the exact blob after a create replay reports it ready", async () => {
    await upload();
    const blob = (await localDb.pendingUploads.get("upload"))?.blob;
    expect(blob).toBeDefined();
    const send = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url === "/media/receipt-attachments")
        return new Response(null, { status: 409 });
      if (url.startsWith(`/media/receipt-attachments/${attachmentId}/status?`))
        return Response.json({
          attachment_id: attachmentId,
          storage_state: "ready",
          content_hash: await hash(blob as Blob),
          source_content_hash: await hash(blob as Blob),
          byte_size: 1,
          source_byte_size: blob?.size,
          pixel_width: 1,
          pixel_height: 1,
          media_type: "image/jpeg",
          retired: false,
        });
      throw new Error(`unexpected ${url}`);
    });

    await dispatchReceiptUploads(userId, organizationId, { fetch: send });

    await expect(localDb.pendingUploads.get("upload")).resolves.toBeUndefined();
    expect(send).toHaveBeenCalledTimes(2);
  });

  it("keeps a mismatched ready blob failed for explicit retry", async () => {
    await upload();
    const send = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url === "/media/receipt-attachments")
        return new Response(null, { status: 409 });
      if (url.startsWith(`/media/receipt-attachments/${attachmentId}/status?`))
        return Response.json({
          attachment_id: attachmentId,
          storage_state: "ready",
          content_hash: "0".repeat(64),
          source_content_hash: "0".repeat(64),
          byte_size: 5,
          source_byte_size: 5,
          pixel_width: 1,
          pixel_height: 1,
          media_type: "image/jpeg",
          retired: false,
        });
      throw new Error(`unexpected ${url}`);
    });

    await dispatchReceiptUploads(userId, organizationId, { fetch: send });

    await expect(localDb.pendingUploads.get("upload")).resolves.toMatchObject({
      state: "failed",
      failureReason: "reconciliation_mismatch",
    });
  });

  it("reconciles a ready attachment after ticket issuance is rejected", async () => {
    await upload();
    const blob = (await localDb.pendingUploads.get("upload"))?.blob;
    let statusRequests = 0;
    const send = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url === "/media/receipt-attachments")
        return new Response(null, { status: 409 });
      if (url.endsWith("/upload-ticket"))
        return new Response(null, { status: 422 });
      if (
        url.startsWith(`/media/receipt-attachments/${attachmentId}/status?`)
      ) {
        statusRequests += 1;
        if (statusRequests === 1) return status("pending");
        return Response.json({
          attachment_id: attachmentId,
          storage_state: "ready",
          content_hash: await hash(blob as Blob),
          source_content_hash: await hash(blob as Blob),
          byte_size: 1,
          source_byte_size: blob?.size,
          pixel_width: 1,
          pixel_height: 1,
          media_type: "image/jpeg",
          retired: false,
        });
      }
      throw new Error(`unexpected ${url}`);
    });

    await dispatchReceiptUploads(userId, organizationId, { fetch: send });

    await expect(localDb.pendingUploads.get("upload")).resolves.toBeUndefined();
    expect(send).toHaveBeenCalledTimes(4);
  });
});
