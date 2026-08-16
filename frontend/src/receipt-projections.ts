import { readEventScopedRecords } from "./archive-cache";

export type ReceiptProjection = {
  id: string;
  title: string;
  totalAmount: string;
  currency: string;
  receiptDate: string | null;
  note: string | null;
  retired: boolean;
  attachments?: { id: string; mediaType: string; retired: boolean }[];
};

const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const decimal = /^(?:0|[1-9]\d*)(?:\.\d+)?$/;

/** Read only safe event-owned receipt metadata; binary attachment bytes never enter this projection. */
export async function readEventReceipts(
  userId: string,
  organizationId: string,
  eventId: string,
): Promise<ReceiptProjection[]> {
  if (![userId, organizationId, eventId].every((id) => uuid.test(id)))
    return [];
  const attachments = await readEventScopedRecords(
    userId,
    organizationId,
    eventId,
    "receipt_attachment",
    true,
  );
  return (
    await readEventScopedRecords(
      userId,
      organizationId,
      eventId,
      "receipt",
      true,
    )
  )
    .filter((record) => {
      const fields = record.fields;
      return (
        record.entityId === fields.id &&
        fields.organization_id === organizationId &&
        fields.event_id === eventId &&
        typeof fields.title === "string" &&
        typeof fields.total_amount === "string" &&
        decimal.test(fields.total_amount) &&
        typeof fields.currency === "string" &&
        /^[A-Z]{3}$/.test(fields.currency) &&
        (fields.receipt_date === null ||
          typeof fields.receipt_date === "string") &&
        (fields.note === null || typeof fields.note === "string")
      );
    })
    .map((record) => {
      const readyAttachments = attachments
        .filter(
          (attachment) =>
            attachment.fields.receipt_id === record.entityId &&
            attachment.fields.storage_state === "ready" &&
            typeof attachment.fields.media_type === "string",
        )
        .map((attachment) => ({
          id: attachment.entityId,
          mediaType: attachment.fields.media_type as string,
          retired: attachment.lifecycle === "retired",
        }));
      return {
        id: record.entityId,
        title: record.fields.title as string,
        totalAmount: record.fields.total_amount as string,
        currency: record.fields.currency as string,
        receiptDate: record.fields.receipt_date as string | null,
        note: record.fields.note as string | null,
        retired: record.lifecycle === "retired",
        ...(readyAttachments.length ? { attachments: readyAttachments } : {}),
      };
    })
    .sort(
      (left, right) =>
        left.title.localeCompare(right.title) ||
        left.id.localeCompare(right.id),
    );
}
