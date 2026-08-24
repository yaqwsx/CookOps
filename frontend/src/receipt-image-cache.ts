import { localDb } from "./local-db";

const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const hash = /^[0-9a-f]{64}$/i;
const mediaTypes = new Set(["image/jpeg", "image/webp"]);

export type ReceiptImageAttachment = {
  id: string;
  mediaType: string;
  contentHash?: string;
  retired: boolean;
};

function validScope(
  userId: string,
  organizationId: string,
  attachment: ReceiptImageAttachment,
): attachment is ReceiptImageAttachment & {
  contentHash: string;
  mediaType: "image/jpeg" | "image/webp";
} {
  return (
    uuid.test(userId) &&
    uuid.test(organizationId) &&
    uuid.test(attachment.id) &&
    !attachment.retired &&
    mediaTypes.has(attachment.mediaType) &&
    typeof attachment.contentHash === "string" &&
    hash.test(attachment.contentHash)
  );
}

export async function readCachedReceiptImage(
  userId: string,
  organizationId: string,
  attachment: ReceiptImageAttachment,
) {
  if (!validScope(userId, organizationId, attachment)) return undefined;
  const key = [
    userId,
    organizationId,
    attachment.id,
    attachment.contentHash.toLowerCase(),
  ] as [string, string, string, string];
  const cached = await localDb.receiptImageCache.get(key);
  if (
    !cached ||
    cached.mediaType !== attachment.mediaType ||
    cached.blob.type !== attachment.mediaType ||
    cached.blob.size === 0
  ) {
    if (cached) await localDb.receiptImageCache.delete(key);
    return undefined;
  }
  const digest = Array.from(
    new Uint8Array(
      await crypto.subtle.digest(
        "SHA-256",
        new Uint8Array(await new Response(cached.blob).arrayBuffer()),
      ),
    ),
  )
    .map((byte) => byte.toString(16).padStart(2, "0"))
    .join("");
  if (digest !== attachment.contentHash.toLowerCase()) {
    await localDb.receiptImageCache.delete(key);
    return undefined;
  }
  return cached;
}

export async function cacheReceiptImage(
  userId: string,
  organizationId: string,
  attachment: ReceiptImageAttachment,
  blob: Blob,
) {
  if (
    !validScope(userId, organizationId, attachment) ||
    blob.type !== attachment.mediaType ||
    blob.size === 0
  )
    return false;
  const digest = Array.from(
    new Uint8Array(
      await crypto.subtle.digest(
        "SHA-256",
        new Uint8Array(await new Response(blob).arrayBuffer()),
      ),
    ),
  )
    .map((byte) => byte.toString(16).padStart(2, "0"))
    .join("");
  if (digest !== attachment.contentHash.toLowerCase()) return false;
  await localDb.receiptImageCache.put({
    userId,
    organizationId,
    attachmentId: attachment.id,
    contentHash: digest,
    mediaType: attachment.mediaType,
    blob,
    updatedAt: new Date().toISOString(),
  });
  return true;
}

export async function loadReceiptImage(
  userId: string,
  organizationId: string,
  attachment: ReceiptImageAttachment,
  send: typeof fetch = fetch,
) {
  const cached = await readCachedReceiptImage(
    userId,
    organizationId,
    attachment,
  );
  if (cached) return cached.blob;
  if (!validScope(userId, organizationId, attachment) || !navigator.onLine)
    return undefined;
  const response = await send(
    `/media/receipt-attachments/${attachment.id}?organization_id=${organizationId}`,
    { credentials: "same-origin" },
  );
  if (!response.ok) return undefined;
  const blob = await response.blob();
  return (await cacheReceiptImage(userId, organizationId, attachment, blob))
    ? blob
    : undefined;
}
