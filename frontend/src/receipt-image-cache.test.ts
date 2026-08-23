import { beforeEach, describe, expect, it } from "vitest";

import { localDb } from "./local-db";
import { cacheReceiptImage, readCachedReceiptImage } from "./receipt-image-cache";

const userId = "a6a58bd6-214e-49af-8fae-e5f974bf8e08";
const organizationId = "5ce17d2f-8365-4b1f-a80b-34d10425d51c";
const attachmentId = "6ce17d2f-8365-4b1f-a80b-34d10425d51c";

describe("receipt image cache", () => {
  beforeEach(() => localDb.receiptImageCache.clear());

  it("stores only a scoped, content-addressed ready image", async () => {
    const blob = new Blob(["photo"], { type: "image/jpeg" });
    const hash = Array.from(new Uint8Array(await crypto.subtle.digest("SHA-256", new Uint8Array(await new Response(blob).arrayBuffer())))).map((byte) => byte.toString(16).padStart(2, "0")).join("");
    const attachment = { id: attachmentId, mediaType: "image/jpeg", contentHash: hash, retired: false };
    await expect(cacheReceiptImage(userId, organizationId, attachment, blob)).resolves.toBe(true);
    await expect(readCachedReceiptImage(userId, "bad", attachment)).resolves.toBeUndefined();
  });

  it("rejects retired, malformed, mismatched, and foreign images", async () => {
    const blob = new Blob(["photo"], { type: "image/jpeg" });
    const attachment = { id: attachmentId, mediaType: "image/jpeg", contentHash: "0".repeat(64), retired: true };
    await expect(cacheReceiptImage(userId, organizationId, attachment, blob)).resolves.toBe(false);
    await expect(cacheReceiptImage(userId, organizationId, { ...attachment, retired: false }, blob)).resolves.toBe(false);
    await expect(localDb.receiptImageCache.count()).resolves.toBe(0);
  });

  it("fails closed on a poisoned stored blob", async () => {
    const contentHash = "0".repeat(64);
    await localDb.receiptImageCache.put({ userId, organizationId, attachmentId, contentHash, mediaType: "image/jpeg", blob: new Blob(["wrong"], { type: "image/jpeg" }), updatedAt: new Date().toISOString() });
    const attachment = { id: attachmentId, mediaType: "image/jpeg", contentHash, retired: false };
    await expect(readCachedReceiptImage(userId, organizationId, attachment)).resolves.toBeUndefined();
    await expect(localDb.receiptImageCache.get([userId, organizationId, attachmentId, contentHash])).resolves.toBeUndefined();
  });
});
