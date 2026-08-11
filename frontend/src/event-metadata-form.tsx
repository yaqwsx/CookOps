import { useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";

import { queueEventMetadataUpdate } from "./event-metadata";

type Props = {
  eventId: string;
  organizationId: string;
  userId: string;
  name: string;
  location: string | null;
  budgetAmount: string;
  generalNote: string | null;
};

export function EventMetadata({ eventId, organizationId, userId, name, location, budgetAmount, generalNote }: Props) {
  const { t } = useTranslation();
  const [input, setInput] = useState({ name, location: location ?? "", budgetAmount, generalNote: generalNote ?? "" });
  const [error, setError] = useState(false);
  const [saved, setSaved] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const inFlight = useRef(false);
  useEffect(() => setInput({ name, location: location ?? "", budgetAmount, generalNote: generalNote ?? "" }), [budgetAmount, generalNote, location, name]);
  async function submit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (inFlight.current) return;
    inFlight.current = true;
    setSubmitting(true);
    try {
      await queueEventMetadataUpdate(userId, organizationId, { eventId, ...input });
      setSaved(true);
      setError(false);
    } catch {
      setSaved(false);
      setError(true);
    } finally {
      inFlight.current = false;
      setSubmitting(false);
    }
  }
  return <form className="event-metadata" onSubmit={(event) => void submit(event)}>
    <label>{t("eventsEdit.name")}<input maxLength={200} onChange={(event) => { setInput((current) => ({ ...current, name: event.target.value })); setSaved(false); }} required value={input.name} /></label>
    <label>{t("eventsEdit.location")}<input maxLength={300} onChange={(event) => { setInput((current) => ({ ...current, location: event.target.value })); setSaved(false); }} value={input.location} /></label>
    <label>{t("eventsEdit.budget")}<input inputMode="decimal" pattern="(?:0|[1-9][0-9]*)(?:\.[0-9]+)?" onChange={(event) => { setInput((current) => ({ ...current, budgetAmount: event.target.value })); setSaved(false); }} required value={input.budgetAmount} /></label>
    <label>{t("eventsEdit.note")}<textarea maxLength={4000} onChange={(event) => { setInput((current) => ({ ...current, generalNote: event.target.value })); setSaved(false); }} value={input.generalNote} /></label>
    <button disabled={submitting} type="submit">{t("eventsEdit.saveMetadata")}</button>
    {error ? <p role="alert">{t("eventsEdit.errors.metadata")}</p> : null}
    {saved ? <p role="status">{t("eventsEdit.metadataSaved")}</p> : null}
  </form>;
}
