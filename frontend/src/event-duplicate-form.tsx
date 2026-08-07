import { useRef, useState } from "react";
import { useTranslation } from "react-i18next";

import { queueEventDuplicate } from "./event-duplicate";

export function EventDuplicate({
  eventId,
  name,
  snapshotId,
  organizationId,
  userId,
}: {
  eventId: string;
  name: string;
  snapshotId: string | null;
  organizationId: string;
  userId: string;
}) {
  const { t } = useTranslation();
  const [copyName, setCopyName] = useState(
    `${name} (${t("eventDuplicate.copy")})`,
  );
  const [error, setError] = useState<string>();
  const busy = useRef(false);
  if (!snapshotId) return null;
  async function submit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!snapshotId) return;
    if (busy.current) return;
    busy.current = true;
    try {
      await queueEventDuplicate(
        userId,
        organizationId,
        eventId,
        snapshotId,
        copyName,
      );
      setError(undefined);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "unavailable");
    } finally {
      busy.current = false;
    }
  }
  return (
    <form className="event-duplicate" onSubmit={(event) => void submit(event)}>
      <label>
        {t("eventDuplicate.name")}
        <input
          onChange={(event) => setCopyName(event.target.value)}
          required
          value={copyName}
        />
      </label>
      <button type="submit">{t("eventDuplicate.action")}</button>
      {error ? <p role="alert">{t(`eventLifecycle.errors.${error}`)}</p> : null}
    </form>
  );
}
