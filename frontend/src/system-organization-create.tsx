import { useState, type FormEvent } from "react";
import { useTranslation } from "react-i18next";

import { createSystemOrganization, SystemOrganizationRequestError } from "./api/system-organizations";

export function SystemOrganizationCreate({
  userId,
  onCreated,
}: {
  userId: string;
  onCreated: () => void;
}) {
  const { t } = useTranslation();
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [currency, setCurrency] = useState("CZK");
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);

  async function submit(event: FormEvent) {
    event.preventDefault();
    setError(null);
    setSaved(false);
    if (!name.trim() || name.trim().length > 200) {
      setError(t("systemOrganizations.errors.name"));
      return;
    }
    if (!/^[A-Za-z]{3}$/.test(currency)) {
      setError(t("systemOrganizations.errors.currency"));
      return;
    }
    setPending(true);
    try {
      await createSystemOrganization(userId, {
        name: name.trim(),
        description: description || null,
        defaultCurrency: currency.toUpperCase(),
      });
      setName("");
      setDescription("");
      setCurrency("CZK");
      setSaved(true);
      onCreated();
    } catch (caught) {
      setError(
        caught instanceof SystemOrganizationRequestError && caught.status === 403
          ? t("systemOrganizations.forbidden")
          : t("systemOrganizations.error"),
      );
    } finally {
      setPending(false);
    }
  }

  return (
    <section aria-labelledby="system-organization-heading">
      <h2 id="system-organization-heading">{t("systemOrganizations.heading")}</h2>
      <form onSubmit={(event) => void submit(event)}>
        <label>
          {t("systemOrganizations.name")}
          <input maxLength={200} onChange={(event) => setName(event.target.value)} value={name} />
        </label>
        <label>
          {t("systemOrganizations.description")}
          <textarea maxLength={10000} onChange={(event) => setDescription(event.target.value)} value={description} />
        </label>
        <label>
          {t("systemOrganizations.currency")}
          <input maxLength={3} onChange={(event) => setCurrency(event.target.value)} value={currency} />
        </label>
        <button disabled={pending} type="submit">{t("systemOrganizations.submit")}</button>
        {error ? <p role="alert">{error}</p> : null}
        {saved ? <p role="status">{t("systemOrganizations.saved")}</p> : null}
      </form>
    </section>
  );
}
