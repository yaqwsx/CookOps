import {
  localDb,
  readOrCreateBrowserInstallationId,
  type PendingUpload,
} from "./local-db";

const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const mediaTypes = new Set(["image/jpeg", "image/webp"]);
const maximumBytes = 2_000_000;

/** Decode, orient, resize, and re-encode before the original can enter IndexedDB. */
export async function prepareReceiptImage(file: File): Promise<Blob> {
  if (!file.type.startsWith("image/")) throw new Error("image");
  const bitmap = await decodeImage(file);
  try {
    const scale = Math.min(1, 2000 / Math.max(bitmap.width, bitmap.height));
    const canvas = document.createElement("canvas");
    canvas.width = Math.max(1, Math.round(bitmap.width * scale));
    canvas.height = Math.max(1, Math.round(bitmap.height * scale));
    const context = canvas.getContext("2d");
    if (!context) throw new Error("image");
    context.drawImage(bitmap, 0, 0, canvas.width, canvas.height);
    for (let quality = 0.92; quality >= 0.4; quality -= 0.1) {
      const image = await new Promise<Blob | null>((resolve) =>
        canvas.toBlob(resolve, "image/jpeg", quality),
      );
      if (image && image.size > 0 && image.size <= maximumBytes) return image;
    }
    throw new Error("image");
  } finally {
    bitmap.close?.();
  }
}

async function decodeImage(file: File): Promise<
  CanvasImageSource & {
    width: number;
    height: number;
    close?: () => void;
  }
> {
  if (typeof createImageBitmap === "function") {
    try {
      return await createImageBitmap(file, { imageOrientation: "from-image" });
    } catch {
      // Some WebKit and headless builds only decode through HTMLImageElement.
    }
  }
  const url = URL.createObjectURL(file);
  try {
    const image = await new Promise<HTMLImageElement>((resolve, reject) => {
      const element = new Image();
      element.onload = () => resolve(element);
      element.onerror = reject;
      element.src = url;
    });
    return image;
  } finally {
    URL.revokeObjectURL(url);
  }
}

type CreateResponse = {
  attachment_id: string;
  ticket_secret: string;
};

type AttachmentStatus = {
  attachment_id: string;
  storage_state: string;
  content_hash: string | null;
  source_content_hash: string | null;
  byte_size: number | null;
  source_byte_size: number | null;
  media_type: string | null;
  retired: boolean;
};

/** Keep already-processed local receipt bytes durable until the media endpoint accepts them. */
export async function queueReceiptAttachment(
  userId: string,
  organizationId: string,
  receiptId: string,
  blob: Blob,
  replaceAttachmentId?: string,
) {
  if (
    ![userId, organizationId, receiptId].every((value) => uuid.test(value)) ||
    !mediaTypes.has(blob.type) ||
    blob.size === 0 ||
    blob.size > maximumBytes
  )
    throw new Error("image");
  const existing = await localDb.pendingUploads
    .where("[userId+organizationId+state]")
    .equals([userId, organizationId, "pending"])
    .toArray();
  const attachmentId = crypto.randomUUID();
  const pending = {
    id: crypto.randomUUID(),
    userId,
    organizationId,
    receiptId,
    attachmentId,
    createMutationId: crypto.randomUUID(),
    finalizeMutationId: crypto.randomUUID(),
    replaceAttachmentId,
    serverCreated: false,
    blob,
    positionKey: String(existing.length + 1),
    createdAt: new Date().toISOString(),
    state: "pending" as const,
  };
  await localDb.pendingUploads.add(pending);
  return pending;
}

type UploadCandidate = {
  receiptId?: string;
  positionKey?: string;
  createMutationId?: string;
  finalizeMutationId?: string;
  replaceAttachmentId?: string;
  attachmentId: string;
  blob: Blob;
};

function validUpload(upload: UploadCandidate): upload is UploadCandidate & {
  receiptId: string;
  positionKey: string;
  createMutationId: string;
  finalizeMutationId: string;
} {
  return (
    typeof upload.receiptId === "string" &&
    typeof upload.positionKey === "string" &&
    uuid.test(upload.receiptId) &&
    uuid.test(upload.attachmentId) &&
    typeof upload.createMutationId === "string" &&
    typeof upload.finalizeMutationId === "string" &&
    uuid.test(upload.createMutationId) &&
    uuid.test(upload.finalizeMutationId) &&
    (upload.replaceAttachmentId === undefined ||
      uuid.test(upload.replaceAttachmentId)) &&
    /^[0-9A-Za-z]{1,255}$/.test(upload.positionKey) &&
    mediaTypes.has(upload.blob.type) &&
    upload.blob.size > 0 &&
    upload.blob.size <= maximumBytes
  );
}

async function blobHash(blob: Blob): Promise<string> {
  const digest = await crypto.subtle.digest(
    "SHA-256",
    await blob.arrayBuffer(),
  );
  return Array.from(new Uint8Array(digest), (byte) =>
    byte.toString(16).padStart(2, "0"),
  ).join("");
}

async function receiptAttachmentStatus(
  upload: Pick<PendingUpload, "attachmentId" | "organizationId" | "receiptId">,
  send: typeof fetch,
): Promise<AttachmentStatus | undefined> {
  if (!upload.receiptId) return undefined;
  const query = new URLSearchParams({
    organization_id: upload.organizationId,
    receipt_id: upload.receiptId,
  });
  const response = await send(
    `/media/receipt-attachments/${upload.attachmentId}/status?${query}`,
    { credentials: "same-origin" },
  );
  if (!response.ok) return undefined;
  try {
    const status = (await response.json()) as AttachmentStatus;
    return status.attachment_id === upload.attachmentId ? status : undefined;
  } catch {
    return undefined;
  }
}

/** Reclaim bytes only when the authenticated server proves it finalized this exact blob. */
async function reconcileReceiptUpload(
  upload: PendingUpload & {
    receiptId: string;
    positionKey: string;
    createMutationId: string;
    finalizeMutationId: string;
  },
  send: typeof fetch,
): Promise<"continue" | "failed" | "synchronized"> {
  const status = await receiptAttachmentStatus(upload, send);
  if (!status) return "continue";
  if (status.storage_state === "pending") return "continue";
  if (
    status.storage_state === "ready" &&
    status.media_type === upload.blob.type &&
    status.source_byte_size === upload.blob.size &&
    typeof status.source_content_hash === "string" &&
    /^[0-9a-f]{64}$/i.test(status.source_content_hash) &&
    status.source_content_hash.toLowerCase() === (await blobHash(upload.blob))
  ) {
    await localDb.pendingUploads.delete(upload.id);
    return "synchronized";
  }
  await localDb.pendingUploads.update(upload.id, {
    state: "failed",
    failureReason: "reconciliation_mismatch",
  });
  return "failed";
}

/** Send durable binary work after ordinary sync has made the target receipt authoritative. */
export async function dispatchReceiptUploads(
  userId: string,
  organizationId: string,
  { fetch: send = fetch }: { fetch?: typeof fetch } = {},
) {
  const uploads = await localDb.pendingUploads
    .where("[userId+organizationId+state]")
    .equals([userId, organizationId, "pending"])
    .sortBy("createdAt");
  const installationId = await readOrCreateBrowserInstallationId(userId);
  for (const upload of uploads) {
    if (!validUpload(upload)) {
      await localDb.pendingUploads.update(upload.id, {
        state: "failed",
        failureReason: "invalid_image",
      });
      continue;
    }
    try {
      await localDb.pendingUploads.update(upload.id, {
        state: "uploading",
        failureReason: undefined,
      });
      const created = await send("/media/receipt-attachments", {
        method: "POST",
        credentials: "same-origin",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({
          mutation_id: upload.createMutationId,
          attachment_id: upload.attachmentId,
          organization_id: organizationId,
          receipt_id: upload.receiptId,
          client_installation_id: installationId,
          media_type: upload.blob.type,
          position_key: upload.positionKey,
          client_wall_time: upload.createdAt,
        }),
      });
      let ticket: CreateResponse;
      if (created.ok) ticket = (await created.json()) as CreateResponse;
      else if (created.status === 409) {
        const reconciliation = await reconcileReceiptUpload(upload, send);
        if (reconciliation !== "continue") continue;
        await localDb.pendingUploads.update(upload.id, { serverCreated: true });
        const replacement = await send(
          `/media/receipt-attachments/${upload.attachmentId}/upload-ticket`,
          {
            method: "POST",
            credentials: "same-origin",
            headers: { "content-type": "application/json" },
            body: JSON.stringify({
              mutation_id: crypto.randomUUID(),
              organization_id: organizationId,
              receipt_id: upload.receiptId,
              client_installation_id: installationId,
              client_wall_time: new Date().toISOString(),
            }),
          },
        );
        if (replacement.status === 422) {
          const reconciliation = await reconcileReceiptUpload(upload, send);
          if (reconciliation !== "continue") continue;
        }
        if (!replacement.ok) throw new Error(String(replacement.status));
        ticket = (await replacement.json()) as CreateResponse;
      } else throw new Error(String(created.status));
      if (created.ok)
        await localDb.pendingUploads.update(upload.id, { serverCreated: true });
      if (ticket.attachment_id !== upload.attachmentId || !ticket.ticket_secret)
        throw new Error("invalid ticket");
      const finalized = await send(
        `/media/receipt-attachments/${upload.attachmentId}`,
        {
          method: "PUT",
          credentials: "same-origin",
          headers: {
            "content-type": upload.blob.type,
            "x-cookops-client-installation": installationId,
            "x-cookops-mutation-id": upload.finalizeMutationId,
            "x-cookops-organization-id": organizationId,
            "x-cookops-receipt-id": upload.receiptId,
            "x-cookops-upload-ticket": ticket.ticket_secret,
            ...(upload.replaceAttachmentId
              ? {
                  "x-cookops-replace-attachment-id": upload.replaceAttachmentId,
                }
              : {}),
          },
          body: upload.blob,
        },
      );
      if (!finalized.ok) throw new Error(String(finalized.status));
      const reconciliation = await reconcileReceiptUpload(upload, send);
      if (reconciliation === "synchronized") continue;
      if (reconciliation === "failed") continue;
      throw new Error("finalization reconciliation required");
    } catch (error) {
      if (error instanceof Error && /^4(?!09)\d\d$/.test(error.message))
        await localDb.pendingUploads.update(upload.id, {
          state: "failed",
          failureReason: "upload_rejected",
        });
      else {
        await localDb.pendingUploads.update(upload.id, { state: "pending" });
        throw error;
      }
    }
  }
}

export async function retryReceiptUpload(id: string) {
  await localDb.pendingUploads.update(id, {
    state: "pending",
    failureReason: undefined,
  });
}

export async function removeReceiptUpload(
  userId: string,
  id: string,
  { fetch: send = fetch }: { fetch?: typeof fetch } = {},
) {
  const upload = await localDb.pendingUploads.get(id);
  if (
    !upload ||
    !uuid.test(upload.attachmentId) ||
    !uuid.test(upload.organizationId) ||
    !uuid.test(upload.receiptId ?? "")
  )
    throw new Error("invalid upload");
  const status = await receiptAttachmentStatus(upload, send);
  if (!status || status.storage_state === "failed") {
    await localDb.pendingUploads.update(id, {
      state: "failed",
      failureReason: "removal_reconciliation_required",
    });
    throw new Error("removal reconciliation required");
  }
  if (status.storage_state === "absent" || status.retired)
    return localDb.pendingUploads.delete(id);
  if (status.storage_state !== "pending" && status.storage_state !== "ready") {
    await localDb.pendingUploads.update(id, {
      state: "failed",
      failureReason: "removal_reconciliation_required",
    });
    throw new Error("removal reconciliation required");
  }
  const installationId = await readOrCreateBrowserInstallationId(userId);
  const response = await send(
    `/media/receipt-attachments/${upload.attachmentId}/lifecycle`,
    {
      method: "POST",
      credentials: "same-origin",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({
        mutation_id: crypto.randomUUID(),
        organization_id: upload.organizationId,
        receipt_id: upload.receiptId,
        client_installation_id: installationId,
        operation: "retire",
        client_wall_time: new Date().toISOString(),
      }),
    },
  );
  if (!response.ok) {
    await localDb.pendingUploads.update(id, {
      state: "failed",
      failureReason: "removal_rejected",
    });
    throw new Error(String(response.status));
  }
  await localDb.pendingUploads.delete(id);
}

export async function setReceiptAttachmentLifecycle(
  userId: string,
  organizationId: string,
  receiptId: string,
  attachmentId: string,
  operation: "retire" | "restore",
  { fetch: send = fetch }: { fetch?: typeof fetch } = {},
) {
  const installationId = await readOrCreateBrowserInstallationId(userId);
  const response = await send(
    `/media/receipt-attachments/${attachmentId}/lifecycle`,
    {
      method: "POST",
      credentials: "same-origin",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({
        mutation_id: crypto.randomUUID(),
        organization_id: organizationId,
        receipt_id: receiptId,
        client_installation_id: installationId,
        operation,
        client_wall_time: new Date().toISOString(),
      }),
    },
  );
  if (!response.ok) throw new Error(String(response.status));
}
