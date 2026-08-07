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
  receipt,
  userId,
}: {
  eventId: string;
  organizationId: string;
  receipt: ReceiptProjection;
  userId: string;
}) {
  const { t } = useTranslation();
  const [editing, setEditing] = useState(false);
  const [error, setError] = useState(false);
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
  return (
    <li className="receipt-item">
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
        <p className="receipt-item__media">{t("receipts.metadataOnly")}</p>
      </div>
      <div className="receipt-item__actions">
        {!receipt.retired ? (
          <button onClick={() => setEditing((value) => !value)} type="button">
            {t("receipts.edit")}
          </button>
        ) : null}
        <button onClick={() => void lifecycle()} type="button">
          {t(receipt.retired ? "receipts.restore" : "receipts.retire")}
        </button>
      </div>
      {error ? <p role="alert">{t("receipts.errors.unavailable")}</p> : null}
      {editing && !receipt.retired ? (
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
  const [offline, setOffline] = useState(false);
  const [error, setError] = useState(false);
  const refresh = useCallback(async () => {
    try {
      await pullOrganization(userId, organizationId);
      setOffline(false);
      setError(false);
    } catch (reason) {
      if (reason instanceof SyncRequestError && reason.status === 401)
        return onUnauthenticated();
      setOffline(true);
    }
  }, [onUnauthenticated, organizationId, userId]);
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
      <ReceiptForm
        eventId={eventId}
        organizationId={organizationId}
        userId={userId}
      />
      {!receipts?.length ? (
        <p role="status">{t("receipts.empty")}</p>
      ) : (
        <ul className="receipt-list">
          {receipts.map((receipt) => (
            <ReceiptItem
              eventId={eventId}
              key={receipt.id}
              organizationId={organizationId}
              receipt={receipt}
              userId={userId}
            />
          ))}
        </ul>
      )}
    </section>
  );
}
