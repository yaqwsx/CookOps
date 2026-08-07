import { useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";

import { queueEventAttendanceUpdate } from "./event-attendance";

export function EventAttendance({
  attendance,
  eventId,
  organizationId,
  userId,
}: {
  attendance: number;
  eventId: string;
  organizationId: string;
  userId: string;
}) {
  const { t } = useTranslation();
  const [value, setValue] = useState(String(attendance));
  const [error, setError] = useState<string>();
  const [saved, setSaved] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const submitInFlight = useRef(false);

  useEffect(() => {
    setValue(String(attendance));
  }, [attendance]);

  async function submit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (submitInFlight.current) return;
    submitInFlight.current = true;
    setSubmitting(true);
    try {
      await queueEventAttendanceUpdate(userId, organizationId, eventId, value);
      setSaved(true);
      setError(undefined);
    } catch (reason) {
      setSaved(false);
      setError(reason instanceof Error ? reason.message : "unavailable");
    } finally {
      submitInFlight.current = false;
      setSubmitting(false);
    }
  }

  return (
    <form className="event-attendance" onSubmit={(event) => void submit(event)}>
      <label>
        {t("eventsEdit.attendance")}
        <input
          inputMode="numeric"
          min="0"
          onChange={(event) => {
            setValue(event.target.value);
            setError(undefined);
            setSaved(false);
          }}
          required
          step="1"
          type="number"
          value={value}
        />
      </label>
      <button disabled={submitting} type="submit">
        {t("eventsEdit.submit")}
      </button>
      {error ? <p role="alert">{t(`eventsEdit.errors.${error}`)}</p> : null}
      {saved ? <p role="status">{t("eventsEdit.saved")}</p> : null}
    </form>
  );
}
