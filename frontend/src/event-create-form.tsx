import { useRef, useState } from "react";
import { useTranslation } from "react-i18next";

import { queueEventCreate, type EventCreateInput } from "./event-create";

const errorCodes = new Set([
  "name",
  "startDate",
  "endDate",
  "dateRange",
  "attendance",
  "budget",
  "location",
  "organizationCurrency",
]);

const initialInput: EventCreateInput = {
  name: "",
  startDate: "",
  endDate: "",
  baseExpectedAttendance: "0",
  budgetAmount: "0",
  location: "",
  generalNote: "",
};

export function EventCreate({
  organizationId,
  userId,
}: {
  organizationId: string;
  userId: string;
}) {
  const { t } = useTranslation();
  const [input, setInput] = useState(initialInput);
  const [error, setError] = useState<string>();
  const [created, setCreated] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const submitInFlight = useRef(false);

  function change(field: keyof EventCreateInput, value: string) {
    setInput((current) => ({ ...current, [field]: value }));
    setError(undefined);
    setCreated(false);
  }

  async function submit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (submitInFlight.current) return;
    submitInFlight.current = true;
    setSubmitting(true);
    try {
      await queueEventCreate(userId, organizationId, input);
      setInput(initialInput);
      setCreated(true);
    } catch (reason) {
      setError(
        reason instanceof Error && errorCodes.has(reason.message)
          ? reason.message
          : "unavailable",
      );
    } finally {
      submitInFlight.current = false;
      setSubmitting(false);
    }
  }

  return (
    <form className="event-create" onSubmit={(event) => void submit(event)}>
      <h3>{t("eventsCreate.heading")}</h3>
      <div className="event-create__fields">
        <label>
          {t("eventsCreate.name")}
          <input
            autoComplete="off"
            maxLength={200}
            onChange={(event) => change("name", event.target.value)}
            required
            value={input.name}
          />
        </label>
        <label>
          {t("eventsCreate.startDate")}
          <input
            onChange={(event) => change("startDate", event.target.value)}
            required
            type="date"
            value={input.startDate}
          />
        </label>
        <label>
          {t("eventsCreate.endDate")}
          <input
            onChange={(event) => change("endDate", event.target.value)}
            required
            type="date"
            value={input.endDate}
          />
        </label>
        <label>
          {t("eventsCreate.attendance")}
          <input
            inputMode="numeric"
            min="0"
            onChange={(event) =>
              change("baseExpectedAttendance", event.target.value)
            }
            required
            step="1"
            type="number"
            value={input.baseExpectedAttendance}
          />
        </label>
        <label>
          {t("eventsCreate.budget")}
          <input
            inputMode="decimal"
            onChange={(event) => change("budgetAmount", event.target.value)}
            pattern="(?:0|[1-9][0-9]*)(?:\.[0-9]+)?"
            required
            value={input.budgetAmount}
          />
        </label>
        <label>
          {t("eventsCreate.location")}
          <input
            maxLength={300}
            onChange={(event) => change("location", event.target.value)}
            value={input.location}
          />
        </label>
        <label className="event-create__note">
          {t("eventsCreate.note")}
          <textarea
            onChange={(event) => change("generalNote", event.target.value)}
            value={input.generalNote}
          />
        </label>
      </div>
      {error ? <p role="alert">{t(`eventsCreate.errors.${error}`)}</p> : null}
      {created ? <p role="status">{t("eventsCreate.saved")}</p> : null}
      <button disabled={submitting} type="submit">
        {t("eventsCreate.submit")}
      </button>
    </form>
  );
}
