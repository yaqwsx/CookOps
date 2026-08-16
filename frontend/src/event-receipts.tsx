import { liveQuery } from "dexie";
import { useCallback, useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";

import {
  queueReceiptCreate,
  queueReceiptRetire,
  queueReceiptRestore,
  queueReceiptUpdate,
  type ReceiptInput,
} from "./receipt-metadata";
import {
  prepareReceiptImage,
  queueReceiptAttachment,
  removeReceiptUpload,
  retryReceiptUpload,
  setReceiptAttachmentLifecycle,
} from "./receipt-media";
import { localDb, type PendingUpload } from "./local-db";
import {
  readEventReceipts,
  type ReceiptProjection,
} from "./receipt-projections";
import { pullOrganization, SyncRequestError } from "./sync-bootstrap";

const blank: ReceiptInput = {
  title: "",
  totalAmount: "0",
  receiptDate: "",
  note: "",
};

function ReceiptForm({
  eventId,
  organizationId,
  receipt,
  userId,
  onDone,
}: {
  eventId: string;
  organizationId: string;
  receipt?: ReceiptProjection;
  userId: string;
  onDone?: () => void;
}) {
  const { t } = useTranslation();
  const [input, setInput] = useState<ReceiptInput>(
    receipt
      ? {
          title: receipt.title,
          totalAmount: receipt.totalAmount,
          receiptDate: receipt.receiptDate ?? "",
          note: receipt.note ?? "",
        }
      : blank,
  );
  const [error, setError] = useState<string>();
  const busy = useRef(false);
  function change(field: keyof ReceiptInput, value: string) {
    setInput((current) => ({ ...current, [field]: value }));
    setError(undefined);
  }
  async function submit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (busy.current) return;
    busy.current = true;
    try {
      if (receipt)
        await queueReceiptUpdate(
          userId,
          organizationId,
          eventId,
          receipt.id,
          input,
        );
      else await queueReceiptCreate(userId, organizationId, eventId, input);
      onDone?.();
      if (!receipt) setInput(blank);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "unavailable");
    } finally {
      busy.current = false;
    }
  }
  return (
    <form className="receipt-form" onSubmit={(event) => void submit(event)}>
      <label>
        {t("receipts.title")}
        <input
          autoComplete="off"
          maxLength={200}
          onChange={(event) => change("title", event.target.value)}
          required
          value={input.title}
        />
      </label>
      <label>
        {t("receipts.total")}
        <input
          inputMode="decimal"
          onChange={(event) => change("totalAmount", event.target.value)}
          pattern="(?:0|[1-9][0-9]*)(?:\.[0-9]+)?"
          required
          value={input.totalAmount}
        />
      </label>
      <label>
        {t("receipts.date")}
        <input
          onChange={(event) => change("receiptDate", event.target.value)}
          type="date"
          value={input.receiptDate}
        />
      </label>
      <label className="receipt-form__note">
        {t("receipts.note")}
        <textarea
          onChange={(event) => change("note", event.target.value)}
          value={input.note}
        />
      </label>
      {error ? <p role="alert">{t(`receipts.errors.${error}`)}</p> : null}
      <button type="submit">
        {t(receipt ? "receipts.save" : "receipts.create")}
      </button>
    </form>
  );
}

function ReceiptItem({
  eventId,
  organizationId,
  onQueued,
  uploads,
  receipt,
  userId,
  readOnly,
}: {
  eventId: string;
  organizationId: string;
  onQueued: (upload: PendingUpload) => void;
  uploads: PendingUpload[];
  receipt: ReceiptProjection;
  userId: string;
  readOnly: boolean;
}) {
  const { t } = useTranslation();
  const [editing, setEditing] = useState(false);
  const [error, setError] = useState(false);
  const [attaching, setAttaching] = useState(false);
  const attachingRef = useRef(false);
  const busy = useRef(false);
  async function lifecycle() {
    if (busy.current) return;
    busy.current = true;
    try {
      await (receipt.retired ? queueReceiptRestore : queueReceiptRetire)(
        userId,
        organizationId,
        eventId,
        receipt.id,
      );
    } catch {
      setError(true);
    } finally {
      busy.current = false;
    }
  }
  async function attach(input: HTMLInputElement) {
    if (attachingRef.current) {
      setError(true);
      return;
    }
    const files = Array.from(input.files ?? []);
    if (!files.length) return;
    attachingRef.current = true;
    setAttaching(true);
    setError(false);
    try {
      for (const file of files) {
        const pending = await queueReceiptAttachment(
          userId,
          organizationId,
          receipt.id,
          await prepareReceiptImage(file),
        );
        onQueued(pending);
      }
    } catch {
      setError(true);
    } finally {
      input.value = "";
      attachingRef.current = false;
      setAttaching(false);
    }
  }
  async function attachmentLifecycle(
    attachmentId: string,
    operation: "retire" | "restore",
  ) {
    try {
      await setReceiptAttachmentLifecycle(
        userId,
        organizationId,
        receipt.id,
        attachmentId,
        operation,
      );
    } catch {
      setError(true);
    }
  }
  return (
    <li className="receipt-item" data-receipt-id={receipt.id}>
      <div aria-disabled={receipt.retired}>
        <h3>{receipt.title}</h3>
        <p>
          {t("receipts.amount", {
            amount: receipt.totalAmount,
            currency: receipt.currency,
          })}
        </p>
        {receipt.receiptDate ? <p>{receipt.receiptDate}</p> : null}
        {receipt.note ? <p>{receipt.note}</p> : null}
        {receipt.attachments?.map((attachment) => (
          <div key={attachment.id}>
            {!attachment.retired ? (
              <img
                alt={t("receipts.photo")}
                loading="lazy"
                src={`/media/receipt-attachments/${attachment.id}?organization_id=${organizationId}`}
              />
            ) : null}
            {!readOnly ? (
              <button
                onClick={() =>
                  void attachmentLifecycle(
                    attachment.id,
                    attachment.retired ? "restore" : "retire",
                  )
                }
                type="button"
              >
                {t(
                  attachment.retired
                    ? "receipts.restorePhoto"
                    : "receipts.removePhoto",
                )}
              </button>
            ) : null}
          </div>
        ))}
        {!readOnly && !receipt.retired ? (
          <label className="receipt-item__media">
            {t("receipts.addPhoto")}
            <input
              accept="image/*"
              capture="environment"
              disabled={attaching}
              multiple
              onChange={(event) => void attach(event.currentTarget)}
              type="file"
            />
          </label>
        ) : null}
        {uploads.map((upload) => (
          <p key={upload.id} role="status">
            {t(
              `receipts.photo${upload.state[0].toUpperCase()}${upload.state.slice(1)}`,
            )}
            {!readOnly && upload.state === "failed" ? (
              <button
                onClick={() => void retryReceiptUpload(upload.id)}
                type="button"
              >
                {t("receipts.retryPhoto")}
              </button>
            ) : null}
            {!readOnly ? (
              <button
                onClick={() => void removeReceiptUpload(userId, upload.id)}
                type="button"
              >
                {t("receipts.removePhoto")}
              </button>
            ) : null}
          </p>
        ))}
      </div>
      <div className="receipt-item__actions">
        {!readOnly && !receipt.retired ? (
          <button onClick={() => setEditing((value) => !value)} type="button">
            {t("receipts.edit")}
          </button>
        ) : null}
        {!readOnly ? (
          <button onClick={() => void lifecycle()} type="button">
            {t(receipt.retired ? "receipts.restore" : "receipts.retire")}
          </button>
        ) : null}
      </div>
      {error ? <p role="alert">{t("receipts.errors.unavailable")}</p> : null}
      {!readOnly && editing && !receipt.retired ? (
        <ReceiptForm
          eventId={eventId}
          onDone={() => setEditing(false)}
          organizationId={organizationId}
          receipt={receipt}
          userId={userId}
        />
      ) : null}
    </li>
  );
}

export function EventReceipts({
  eventId,
  onBack,
  onUnauthenticated,
  organizationId,
  userId,
}: {
  eventId: string;
  onBack: () => void;
  onUnauthenticated: () => void;
  organizationId: string;
  userId: string;
}) {
  const { t } = useTranslation();
  const [receipts, setReceipts] = useState<ReceiptProjection[]>();
  const [uploads, setUploads] = useState<PendingUpload[]>([]);
  const optimisticUploadIds = useRef(new Set<string>());
  const [offline, setOffline] = useState(false);
  const [error, setError] = useState(false);
  const [readOnly, setReadOnly] = useState(false);
  const refresh = useCallback(async () => {
    try {
      const readOnly = async () => {
        const event = await localDb.canonicalRecords.get([
          userId,
          organizationId,
          "event",
          eventId,
        ]);
        return (
          event?.fields.lifecycle === "archived" &&
          typeof event.fields.current_archive_snapshot_id === "string"
        );
      };
      setReadOnly(await readOnly());
      await pullOrganization(userId, organizationId);
      setReadOnly(await readOnly());
      setOffline(false);
      setError(false);
    } catch (reason) {
      if (reason instanceof SyncRequestError && reason.status === 401)
        return onUnauthenticated();
      setOffline(true);
    }
  }, [eventId, onUnauthenticated, organizationId, userId]);
  useEffect(() => {
    const subscription = liveQuery(() =>
      readEventReceipts(userId, organizationId, eventId),
    ).subscribe({
      next: setReceipts,
      error: () => setError(true),
    });
    void refresh();
    return () => subscription.unsubscribe();
  }, [eventId, organizationId, refresh, userId]);
  useEffect(() => {
    const subscription = liveQuery(() =>
      localDb.pendingUploads.toArray(),
    ).subscribe({
      next: (items) => {
        const scoped = items.filter(
          (item) =>
            item.userId === userId && item.organizationId === organizationId,
        );
        setUploads((current) => {
          const next = new Map(scoped.map((item) => [item.id, item]));
          for (const item of scoped)
            optimisticUploadIds.current.delete(item.id);
          for (const item of current)
            if (optimisticUploadIds.current.has(item.id) && !next.has(item.id))
              next.set(item.id, item);
          return [...next.values()];
        });
      },
    });
    return () => subscription.unsubscribe();
  }, [organizationId, userId]);
  if (!receipts && !error) return <p role="status">{t("receipts.loading")}</p>;
  if (error)
    return (
      <div role="alert">
        <p>{t("receipts.error")}</p>
        <button onClick={() => void refresh()} type="button">
          {t("receipts.retry")}
        </button>
      </div>
    );
  return (
    <section className="event-receipts" aria-labelledby="receipts-heading">
      <header className="event-receipts__header">
        <h2 id="receipts-heading">{t("receipts.heading")}</h2>
        <button onClick={onBack} type="button">
          {t("receipts.planner")}
        </button>
      </header>
      <p>{t("receipts.scope")}</p>
      {offline ? <p role="status">{t("receipts.offline")}</p> : null}
      {!readOnly ? (
        <ReceiptForm
          eventId={eventId}
          organizationId={organizationId}
          userId={userId}
        />
      ) : (
        <p className="planner-archived" role="status">
          {t("planner.archived")}
        </p>
      )}
      {!receipts?.length ? (
        <p role="status">{t("receipts.empty")}</p>
      ) : (
        <ul className="receipt-list">
          {receipts.map((receipt) => (
            <ReceiptItem
              eventId={eventId}
              key={receipt.id}
              organizationId={organizationId}
              onQueued={(upload) => {
                optimisticUploadIds.current.add(upload.id);
                setUploads((current) =>
                  current.some((item) => item.id === upload.id)
                    ? current
                    : [...current, upload],
                );
              }}
              receipt={receipt}
              userId={userId}
              readOnly={readOnly}
              uploads={uploads.filter(
                (upload) => upload.receiptId === receipt.id,
              )}
            />
          ))}
        </ul>
      )}
    </section>
  );
}
