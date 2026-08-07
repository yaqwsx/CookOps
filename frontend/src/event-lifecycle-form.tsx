import { useRef, useState } from "react";
import { useTranslation } from "react-i18next";

import {
  queueEventLifecycle,
  type EventLifecycleOperation,
} from "./event-lifecycle";

export function EventLifecycle({
  eventId,
  lifecycle,
  organizationId,
  userId,
}: {
  eventId: string;
  lifecycle: "active" | "archived";
  organizationId: string;
  userId: string;
}) {
  const { t } = useTranslation();
  const [confirming, setConfirming] = useState(false);
  const [error, setError] = useState<string>();
  const busy = useRef(false);
  const operation: EventLifecycleOperation =
    lifecycle === "active" ? "archive" : "reactivate";

  async function submit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (busy.current) return;
    busy.current = true;
    try {
      await queueEventLifecycle(userId, organizationId, eventId, operation);
      setConfirming(false);
      setError(undefined);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "unavailable");
    } finally {
      busy.current = false;
    }
  }

  if (!confirming)
    return (
      <button onClick={() => setConfirming(true)} type="button">
        {t(`eventLifecycle.${operation}`)}
      </button>
    );
  return (
    <form className="event-lifecycle" onSubmit={(event) => void submit(event)}>
      <p id={`event-lifecycle-${eventId}`}>
        {t(`eventLifecycle.confirm.${operation}`)}
      </p>
      <button aria-describedby={`event-lifecycle-${eventId}`} type="submit">
        {t(`eventLifecycle.confirmAction.${operation}`)}
      </button>
      <button onClick={() => setConfirming(false)} type="button">
        {t("eventLifecycle.cancel")}
      </button>
      {error ? <p role="alert">{t(`eventLifecycle.errors.${error}`)}</p> : null}
    </form>
  );
}
