import { liveQuery } from "dexie";
import { useEffect, useState, type FormEvent } from "react";
import { useTranslation } from "react-i18next";

import { localDb, readOfflineAuthorization } from "./local-db";
import { queueOrganizationMetadata } from "./organization-metadata";

export function OrganizationMetadataSettings({
  userId,
  organizationId,
}: {
  userId: string;
  organizationId: string;
}) {
  const { t } = useTranslation();
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [currency, setCurrency] = useState("CZK");
  const [error, setError] = useState(false);
  const [saved, setSaved] = useState(false);
  const [offlineAllowed, setOfflineAllowed] = useState(false);
  useEffect(() => {
    const subscription = liveQuery(
      async () =>
        (await localDb.optimisticOverlays.get([
          userId,
          organizationId,
          "organization",
          organizationId,
        ])) ??
        localDb.canonicalRecords.get([
          userId,
          organizationId,
          "organization",
          organizationId,
        ]),
    ).subscribe({
      next: (record) => {
        if (record) {
          setName(String(record.fields.name ?? ""));
          setDescription(
            record.fields.description == null
              ? ""
              : String(record.fields.description),
          );
          setCurrency(String(record.fields.default_currency ?? "CZK"));
        }
      },
    });
    return () => subscription.unsubscribe();
  }, [organizationId, userId]);
  useEffect(() => {
    void readOfflineAuthorization(userId, organizationId).then(
      setOfflineAllowed,
    );
  }, [organizationId, userId]);
  async function submit(event: FormEvent) {
    event.preventDefault();
    setError(false);
    setSaved(false);
    const id = await queueOrganizationMetadata(userId, organizationId, {
      name: name.trim(),
      description: description.trim() || null,
      default_currency: currency.trim().toUpperCase(),
    });
    if (id) setSaved(true);
    else setError(true);
  }
  const offline = !navigator.onLine,
    disabled = offline && !offlineAllowed;
  return (
    <section aria-labelledby="organization-metadata-heading">
      <h3 id="organization-metadata-heading">
        {t("organizationMetadata.heading")}
      </h3>
      {offline ? (
        <p role="status">
          {disabled
            ? t("organizationMetadata.offlineBlocked")
            : t("organizationMetadata.offline")}
        </p>
      ) : null}
      <form onSubmit={(event) => void submit(event)}>
        <label>
          {t("organizationMetadata.name")}
          <input
            value={name}
            onChange={(event) => setName(event.target.value)}
            disabled={disabled}
            required
            maxLength={200}
          />
        </label>
        <label>
          {t("organizationMetadata.description")}
          <textarea
            value={description}
            onChange={(event) => setDescription(event.target.value)}
            disabled={disabled}
            maxLength={10000}
          />
        </label>
        <label>
          {t("organizationMetadata.currency")}
          <input
            value={currency}
            onChange={(event) => setCurrency(event.target.value)}
            disabled={disabled}
            required
            maxLength={3}
          />
        </label>
        <button type="submit" disabled={disabled}>
          {t("organizationMetadata.save")}
        </button>
      </form>
      {error ? <p role="alert">{t("organizationMetadata.error")}</p> : null}
      {saved ? <p role="status">{t("organizationMetadata.saved")}</p> : null}
    </section>
  );
}
