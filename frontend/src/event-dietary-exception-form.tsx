import { liveQuery } from "dexie";
import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { localDb } from "./local-db";
import {
  queueEventDietaryExceptionCreate,
  readVisibleEventDietaryExceptions,
} from "./event-dietary-exception";

export function EventDietaryExceptions({
  eventId,
  organizationId,
  userId,
}: {
  eventId: string;
  organizationId: string;
  userId: string;
}) {
  const { t } = useTranslation();
  const [name, setName] = useState("");
  const [note, setNote] = useState("");
  const [tagIds, setTagIds] = useState<string[]>([]);
  const [error, setError] = useState(false);
  const [tags, setTags] = useState<{ id: string; name: string }[]>([]);
  const [exceptions, setExceptions] = useState<
    { entityId: string; fields: Record<string, unknown> }[]
  >([]);
  useEffect(() => {
    const subscription = liveQuery(async () => {
      const [available, listed] = await Promise.all([
        localDb.canonicalRecords
          .where("[userId+organizationId+entityType]")
          .equals([userId, organizationId, "dietary_tag"])
          .toArray(),
        readVisibleEventDietaryExceptions(userId, organizationId, eventId),
      ]);
      return {
        available: available
          .filter(
            (x) => x.lifecycle === "active" && x.fields.retired_at == null,
          )
          .map((x) => ({
            id: x.entityId,
            name: String(x.fields.name ?? x.fields.seed_key ?? ""),
          })),
        listed,
      };
    }).subscribe({
      next: (set) => {
        setTags(set.available);
        setExceptions(set.listed);
      },
    });
    return () => subscription.unsubscribe();
  }, [eventId, organizationId, userId]);
  async function submit(event: React.FormEvent) {
    event.preventDefault();
    try {
      await queueEventDietaryExceptionCreate(userId, organizationId, eventId, {
        name,
        note,
        tagIds,
      });
      setName("");
      setNote("");
      setTagIds([]);
      setError(false);
    } catch {
      setError(true);
    }
  }
  return (
    <section aria-labelledby="dietary-exceptions-heading">
      <h3 id="dietary-exceptions-heading">
        {t("eventDietaryExceptions.heading")}
      </h3>
      <form onSubmit={(event) => void submit(event)}>
        <label>
          {t("eventDietaryExceptions.name")}
          <input
            required
            value={name}
            onChange={(event) => setName(event.target.value)}
          />
        </label>
        <label>
          {t("eventDietaryExceptions.note")}
          <textarea
            value={note}
            onChange={(event) => setNote(event.target.value)}
          />
        </label>
        <fieldset>
          <legend>{t("eventDietaryExceptions.tags")}</legend>
          {tags.map((tag) => (
            <label key={tag.id}>
              <input
                type="checkbox"
                checked={tagIds.includes(tag.id)}
                onChange={() =>
                  setTagIds((current) =>
                    current.includes(tag.id)
                      ? current.filter((id) => id !== tag.id)
                      : [...current, tag.id],
                  )
                }
              />
              {tag.name}
            </label>
          ))}
        </fieldset>
        <button type="submit">{t("eventDietaryExceptions.create")}</button>
        {error ? <p role="alert">{t("eventDietaryExceptions.error")}</p> : null}
      </form>
      <ul aria-label={t("eventDietaryExceptions.list")}>
        {exceptions.map((item) => (
          <li key={item.entityId}>
            {String(item.fields.name)}
            {Array.isArray(item.fields.selected_tag_names) &&
            item.fields.selected_tag_names.length > 0
              ? ` — ${item.fields.selected_tag_names.join(", ")}`
              : ""}
            {item.fields.note ? ` — ${String(item.fields.note)}` : ""}
          </li>
        ))}
      </ul>
    </section>
  );
}
