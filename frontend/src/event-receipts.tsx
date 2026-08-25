import { liveQuery } from "dexie";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
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
  isReceiptImageReadabilityError,
  queueReceiptAttachment,
  removeReceiptUpload,
  retryReceiptUpload,
  setReceiptAttachmentLifecycle,
} from "./receipt-media";
import { loadReceiptImage } from "./receipt-image-cache";
import { localDb, type PendingUpload } from "./local-db";
import {
  readEventReceipts,
  type ReceiptProjection,
} from "./receipt-projections";
import { pullOrganization, SyncRequestError } from "./sync-bootstrap";
import {
  readEventPlanner,
  type EventPlannerProjection,
} from "./planner-projections";
import {
  readEventCosts,
  type EventCostsProjection,
} from "./event-cost-projections";
import { EventSummary, useEventPendingSync } from "./event-summary";
import { EventSectionNavigation } from "./event-section-navigation";
import { ensureArchivedEventCached } from "./archive-cache";
import {
  formatReceiptAmount,
  formatReceiptDate,
  isReceiptDate,
} from "./receipt-display";

const blank: ReceiptInput = {
  title: "",
  totalAmount: "0",
  receiptDate: "",
  note: "",
};
const emptyUploads: PendingUpload[] = [];

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
  offline,
}: {
  eventId: string;
  organizationId: string;
  onQueued: (upload: PendingUpload) => void;
  uploads: PendingUpload[];
  receipt: ReceiptProjection;
  userId: string;
  readOnly: boolean;
  offline: boolean;
}) {
  const { i18n, t } = useTranslation();
  const locale = i18n.resolvedLanguage ?? "cs";
  const [editing, setEditing] = useState(false);
  const [error, setError] = useState<"unavailable" | "retakePhoto">();
  const [attaching, setAttaching] = useState(false);
  const [previewUrls, setPreviewUrls] = useState<Record<string, string>>({});
  const [cachedImageUrls, setCachedImageUrls] = useState<
    Record<string, string>
  >({});
  const previewUrlEntries = useRef(
    new Map<string, { signature: string; url: string }>(),
  );
  const attachingRef = useRef(false);
  const busy = useRef(false);
  useEffect(() => {
    const currentIds = new Set<string>();
    for (const upload of uploads) {
      currentIds.add(upload.id);
      const signature = `${upload.id}:${upload.createdAt}:${upload.blob.size}:${upload.blob.type}:${upload.state === "synchronized"}`;
      const existing = previewUrlEntries.current.get(upload.id);
      if (upload.state === "synchronized") {
        if (existing) {
          URL.revokeObjectURL(existing.url);
          previewUrlEntries.current.delete(upload.id);
        }
      } else if (existing?.signature !== signature) {
        if (existing) URL.revokeObjectURL(existing.url);
        if (typeof URL.createObjectURL === "function")
          previewUrlEntries.current.set(upload.id, {
            signature,
            url: URL.createObjectURL(upload.blob),
          });
      }
    }
    for (const [id, entry] of previewUrlEntries.current) {
      if (!currentIds.has(id)) {
        URL.revokeObjectURL(entry.url);
        previewUrlEntries.current.delete(id);
      }
    }
    setPreviewUrls(
      Object.fromEntries(
        [...previewUrlEntries.current].map(([id, entry]) => [id, entry.url]),
      ),
    );
  }, [uploads]);
  useEffect(
    () => () => {
      for (const entry of previewUrlEntries.current.values())
        URL.revokeObjectURL(entry.url);
      previewUrlEntries.current.clear();
    },
    [],
  );
  useEffect(() => {
    let active = true;
    const entries = receipt.attachments ?? [];
    void Promise.all(
      entries
        .filter((attachment) => !attachment.retired)
        .map(
          async (attachment) =>
            [
              attachment.id,
              await loadReceiptImage(userId, organizationId, attachment),
            ] as const,
        ),
    ).then((loaded) => {
      if (!active) return;
      const urls: Record<string, string> = {};
      for (const [id, blob] of loaded)
        if (blob && typeof URL.createObjectURL === "function")
          urls[id] = URL.createObjectURL(blob);
      setCachedImageUrls(urls);
    });
    return () => {
      active = false;
    };
  }, [organizationId, receipt.attachments, userId]);
  useEffect(
    () => () => {
      Object.values(cachedImageUrls).forEach((url) => {
        URL.revokeObjectURL(url);
      });
    },
    [cachedImageUrls],
  );
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
      setError("unavailable");
    } finally {
      busy.current = false;
    }
  }
  async function attach(input: HTMLInputElement, replaceAttachmentId?: string) {
    if (attachingRef.current) {
      setError("unavailable");
      return;
    }
    const files = Array.from(input.files ?? []);
    if (!files.length) return;
    attachingRef.current = true;
    setAttaching(true);
    setError(undefined);
    try {
      for (const file of files) {
        const prepared = await prepareReceiptImage(file);
        const pending = replaceAttachmentId
          ? await queueReceiptAttachment(
              userId,
              organizationId,
              receipt.id,
              prepared,
              replaceAttachmentId,
            )
          : await queueReceiptAttachment(
              userId,
              organizationId,
              receipt.id,
              prepared,
            );
        onQueued(pending);
      }
    } catch (reason) {
      setError(
        isReceiptImageReadabilityError(reason) ? "retakePhoto" : "unavailable",
      );
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
      setError("unavailable");
    }
  }
  return (
    <li className="receipt-item" data-receipt-id={receipt.id}>
      <div aria-disabled={receipt.retired}>
        <h3>{receipt.title}</h3>
        <p>
          {formatReceiptAmount(receipt.totalAmount, receipt.currency, locale)}
        </p>
        {receipt.receiptDate ? (
          <p>
            {isReceiptDate(receipt.receiptDate) ? (
              <time dateTime={receipt.receiptDate}>
                {formatReceiptDate(receipt.receiptDate, locale)}
              </time>
            ) : (
              receipt.receiptDate
            )}
          </p>
        ) : null}
        {receipt.note ? <p>{receipt.note}</p> : null}
        {receipt.attachments?.map((attachment) => (
          <div key={attachment.id}>
            {!attachment.retired ? (
              <img
                alt={t("receipts.photo")}
                loading="lazy"
                src={
                  cachedImageUrls[attachment.id] ??
                  `/media/receipt-attachments/${attachment.id}?organization_id=${organizationId}`
                }
              />
            ) : null}
            {offline && !cachedImageUrls[attachment.id] ? (
              <p role="status">{t("receipts.photoUnavailableOffline")}</p>
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
            {!readOnly &&
            !receipt.retired &&
            !attachment.retired &&
            !uploads.some(
              (upload) =>
                upload.replaceAttachmentId === attachment.id &&
                (upload.state === "pending" || upload.state === "uploading"),
            ) ? (
              <label className="receipt-item__media">
                {t("receipts.replacePhoto")}
                <input
                  accept="image/*"
                  capture="environment"
                  disabled={attaching}
                  onChange={(event) =>
                    void attach(event.currentTarget, attachment.id)
                  }
                  type="file"
                />
              </label>
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
          <div key={upload.id}>
            {previewUrls[upload.id] ? (
              <img
                alt={t("receipts.photo")}
                loading="lazy"
                src={previewUrls[upload.id]}
              />
            ) : null}
            <p role="status">
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
          </div>
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
      {error ? <p role="alert">{t(`receipts.errors.${error}`)}</p> : null}
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
  const identity = `${userId}:${organizationId}:${eventId}`;
  const [receiptsState, setReceiptsState] = useState<{
    identity: string;
    receipts?: ReceiptProjection[];
  }>();
  const [uploads, setUploads] = useState<PendingUpload[]>([]);
  const optimisticUploadIds = useRef(new Set<string>());
  const [offlineState, setOfflineState] = useState({ identity, value: false });
  const [errorState, setErrorState] = useState({ identity, value: false });
  const [readOnlyState, setReadOnlyState] = useState({
    identity,
    value: false,
  });
  const identityRef = useRef(identity);
  identityRef.current = identity;
  const [plannerState, setPlannerState] = useState<{
    identity: string;
    planner?: EventPlannerProjection;
  }>();
  const [costsState, setCostsState] = useState<{
    identity: string;
    costs?: EventCostsProjection;
  }>();
  const pendingSync = useEventPendingSync(userId, organizationId, eventId);
  const uploadsByReceipt = useMemo(() => {
    const byReceipt = new Map<string, PendingUpload[]>();
    for (const upload of uploads) {
      if (!upload.receiptId) continue;
      const receiptUploads = byReceipt.get(upload.receiptId) ?? [];
      receiptUploads.push(upload);
      byReceipt.set(upload.receiptId, receiptUploads);
    }
    return byReceipt;
  }, [uploads]);
  const refresh = useCallback(
    async (signal?: AbortSignal) => {
      const requestIdentity = identity;
      const isCurrent = () => identityRef.current === requestIdentity;
      try {
        const readOnly = async () => {
          const event = await localDb.canonicalRecords.get([
            userId,
            organizationId,
            "event",
            eventId,
          ]);
          if (!event) return undefined;
          return (
            event.fields.lifecycle === "archived" &&
            typeof event.fields.current_archive_snapshot_id === "string"
          );
        };
        if (!isCurrent() || signal?.aborted) return;
        const initiallyReadOnly = await readOnly();
        if (initiallyReadOnly !== false) {
          await pullOrganization(userId, organizationId);
          if (!isCurrent() || signal?.aborted) return;
          if (await readOnly())
            await ensureArchivedEventCached(
              userId,
              organizationId,
              eventId,
              fetch,
              signal,
            );
        } else {
          setReadOnlyState({ identity: requestIdentity, value: false });
        }
        if (!isCurrent() || signal?.aborted) return;
        setReadOnlyState({
          identity: requestIdentity,
          value: (await readOnly()) === true,
        });
        setOfflineState({ identity: requestIdentity, value: false });
        setErrorState({ identity: requestIdentity, value: false });
      } catch (reason) {
        if (signal?.aborted || !isCurrent()) return;
        if (reason instanceof SyncRequestError && reason.status === 401)
          return onUnauthenticated();
        setOfflineState({ identity: requestIdentity, value: true });
      }
    },
    [eventId, identity, onUnauthenticated, organizationId, userId],
  );
  useEffect(() => {
    const effectIdentity = identity;
    setReceiptsState(undefined);
    setPlannerState(undefined);
    setCostsState(undefined);
    setOfflineState({ identity: effectIdentity, value: false });
    setErrorState({ identity: effectIdentity, value: false });
    setReadOnlyState({ identity: effectIdentity, value: false });
    const plannerSubscription = liveQuery(() =>
      readEventPlanner(userId, organizationId, eventId),
    ).subscribe({
      next: (next) =>
        setPlannerState({ identity: effectIdentity, planner: next }),
    });
    const costsSubscription = liveQuery(() =>
      readEventCosts(userId, organizationId, eventId),
    ).subscribe({
      next: (next) => setCostsState({ identity: effectIdentity, costs: next }),
    });
    const subscription = liveQuery(() =>
      readEventReceipts(userId, organizationId, eventId),
    ).subscribe({
      next: (next) =>
        setReceiptsState({ identity: effectIdentity, receipts: next }),
      error: () => setErrorState({ identity: effectIdentity, value: true }),
    });
    const controller = new AbortController();
    void refresh(controller.signal);
    return () => {
      controller.abort();
      subscription.unsubscribe();
      plannerSubscription.unsubscribe();
      costsSubscription.unsubscribe();
    };
  }, [eventId, organizationId, refresh, userId, identity]);
  const planner =
    plannerState?.identity === identity ? plannerState.planner : undefined;
  const costs =
    costsState?.identity === identity ? costsState.costs : undefined;
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
  const receipts =
    receiptsState?.identity === identity ? receiptsState.receipts : undefined;
  const offline = offlineState.identity === identity && offlineState.value;
  const error = errorState.identity === identity && errorState.value;
  const readOnly = readOnlyState.identity === identity && readOnlyState.value;
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
    <>
      {planner ? (
        <EventSummary
          eventId={eventId}
          organizationId={organizationId}
          userId={userId}
          planner={planner}
          costs={costs}
          pendingSync={pendingSync}
        />
      ) : null}
      <EventSectionNavigation
        current="receipts"
        eventId={eventId}
        organizationId={organizationId}
      />
      <section className="event-receipts" aria-labelledby="receipts-heading">
        <header className="event-receipts__header">
          <h2 id="receipts-heading">{t("receipts.heading")}</h2>
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
                offline={offline}
                userId={userId}
                readOnly={readOnly}
                uploads={uploadsByReceipt.get(receipt.id) ?? emptyUploads}
              />
            ))}
          </ul>
        )}
      </section>
    </>
  );
}
