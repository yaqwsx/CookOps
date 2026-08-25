import { useCallback, useEffect, useState, type FormEvent } from "react";
import { useTranslation } from "react-i18next";

import {
  changeSystemOrganizationLifecycle,
  createSystemOrganization,
  editSystemOrganization,
  getSystemOrganizations,
  SystemOrganizationRequestError,
  type SystemOrganization,
} from "./api/system-organizations";
import { OrganizationMemberships } from "./organization-membership";

export function SystemOrganizationCreate({
  userId,
  onCreated,
  onUnauthenticated = () => undefined,
}: {
  userId: string;
  onCreated: () => void;
  onUnauthenticated?: () => void;
}) {
  const { t } = useTranslation();
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [currency, setCurrency] = useState("CZK");
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);
  const [organizations, setOrganizations] = useState<SystemOrganization[]>([]);
  const [listError, setListError] = useState(false);
  const [pendingLifecycle, setPendingLifecycle] = useState<string | null>(null);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [editName, setEditName] = useState("");
  const [editDescription, setEditDescription] = useState("");
  const [editCurrency, setEditCurrency] = useState("CZK");
  const [editError, setEditError] = useState<string | null>(null);
  const [pendingEdit, setPendingEdit] = useState(false);
  const [selectedOrganizationId, setSelectedOrganizationId] = useState<
    string | null
  >(null);
  const selectedOrganization = organizations.find(
    ({ id }) => id === selectedOrganizationId,
  );

  const refresh = useCallback(async () => {
    try {
      const next = await getSystemOrganizations();
      setOrganizations(next);
      setSelectedOrganizationId((current) =>
        current &&
        next.some(
          (organization) =>
            organization.id === current && !organization.retired_at,
        )
          ? current
          : null,
      );
      setListError(false);
    } catch {
      setListError(true);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

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
      await refresh();
    } catch (caught) {
      setError(
        caught instanceof SystemOrganizationRequestError &&
          caught.status === 403
          ? t("systemOrganizations.forbidden")
          : t("systemOrganizations.error"),
      );
    } finally {
      setPending(false);
    }
  }

  async function changeLifecycle(organization: SystemOrganization) {
    const operation = organization.retired_at ? "restore" : "retire";
    if (
      operation === "retire" &&
      !window.confirm(
        t("systemOrganizations.retireConfirm", { name: organization.name }),
      )
    )
      return;
    setPendingLifecycle(organization.id);
    try {
      await changeSystemOrganizationLifecycle(
        userId,
        organization.id,
        operation,
      );
      await refresh();
      onCreated();
    } catch {
      setListError(true);
    } finally {
      setPendingLifecycle(null);
    }
  }

  function beginEdit(organization: SystemOrganization) {
    setEditingId(organization.id);
    setEditName(organization.name);
    setEditDescription(organization.description ?? "");
    setEditCurrency(organization.default_currency);
    setEditError(null);
  }

  async function saveEdit(event: FormEvent) {
    event.preventDefault();
    if (!editingId) return;
    setEditError(null);
    if (!editName.trim() || editName.trim().length > 200) {
      setEditError(t("systemOrganizations.errors.name"));
      return;
    }
    if (!/^[A-Za-z]{3}$/.test(editCurrency)) {
      setEditError(t("systemOrganizations.errors.currency"));
      return;
    }
    setPendingEdit(true);
    try {
      await editSystemOrganization(userId, editingId, {
        name: editName.trim(),
        description: editDescription || null,
        defaultCurrency: editCurrency.toUpperCase(),
      });
      setEditingId(null);
      await refresh();
      onCreated();
    } catch {
      setEditError(t("systemOrganizations.editError"));
    } finally {
      setPendingEdit(false);
    }
  }

  return (
    <section aria-labelledby="system-organization-heading">
      <h2 id="system-organization-heading">
        {t("systemOrganizations.heading")}
      </h2>
      <form onSubmit={(event) => void submit(event)}>
        <label>
          {t("systemOrganizations.name")}
          <input
            maxLength={200}
            onChange={(event) => setName(event.target.value)}
            value={name}
          />
        </label>
        <label>
          {t("systemOrganizations.description")}
          <textarea
            maxLength={10000}
            onChange={(event) => setDescription(event.target.value)}
            value={description}
          />
        </label>
        <label>
          {t("systemOrganizations.currency")}
          <input
            maxLength={3}
            onChange={(event) => setCurrency(event.target.value)}
            value={currency}
          />
        </label>
        <button disabled={pending} type="submit">
          {t("systemOrganizations.submit")}
        </button>
        {error ? <p role="alert">{error}</p> : null}
        {saved ? <p role="status">{t("systemOrganizations.saved")}</p> : null}
      </form>
      <section aria-labelledby="system-organizations-list-heading">
        <h2 id="system-organizations-list-heading">
          {t("systemOrganizations.listHeading")}
        </h2>
        {listError ? (
          <p role="alert">
            {t("systemOrganizations.error")}{" "}
            <button onClick={() => void refresh()} type="button">
              {t("authentication.retry")}
            </button>
          </p>
        ) : null}
        <ul>
          {organizations.map((organization) => (
            <li key={organization.id}>
              {editingId === organization.id ? (
                <form onSubmit={(event) => void saveEdit(event)}>
                  <label>
                    {t("systemOrganizations.name")}
                    <input
                      aria-label={t("systemOrganizations.editName", {
                        name: organization.name,
                      })}
                      maxLength={200}
                      onChange={(event) => setEditName(event.target.value)}
                      value={editName}
                    />
                  </label>
                  <label>
                    {t("systemOrganizations.description")}
                    <textarea
                      maxLength={10000}
                      onChange={(event) =>
                        setEditDescription(event.target.value)
                      }
                      value={editDescription}
                    />
                  </label>
                  <label>
                    {t("systemOrganizations.currency")}
                    <input
                      maxLength={3}
                      onChange={(event) => setEditCurrency(event.target.value)}
                      value={editCurrency}
                    />
                  </label>
                  <button disabled={pendingEdit} type="submit">
                    {t("systemOrganizations.saveEdit")}
                  </button>
                  <button
                    disabled={pendingEdit}
                    onClick={() => setEditingId(null)}
                    type="button"
                  >
                    {t("systemOrganizations.cancelEdit")}
                  </button>
                  {editError ? <p role="alert">{editError}</p> : null}
                </form>
              ) : null}
              {editingId !== organization.id ? (
                <>
                  {organization.name} —{" "}
                  {organization.retired_at
                    ? t("systemOrganizations.retired")
                    : t("systemOrganizations.active")}
                  {!organization.retired_at ? (
                    <button
                      aria-label={t(
                        "systemOrganizations.manageAdministratorsFor",
                        { name: organization.name },
                      )}
                      aria-pressed={selectedOrganizationId === organization.id}
                      disabled={pendingEdit}
                      onClick={() => setSelectedOrganizationId(organization.id)}
                      type="button"
                    >
                      {t("systemOrganizations.manageAdministrators")}
                    </button>
                  ) : null}
                  <button
                    aria-label={t("systemOrganizations.editName", {
                      name: organization.name,
                    })}
                    onClick={() => beginEdit(organization)}
                    type="button"
                  >
                    {t("systemOrganizations.edit")}
                  </button>
                </>
              ) : null}
              <button
                disabled={pendingLifecycle === organization.id || pendingEdit}
                onClick={() => void changeLifecycle(organization)}
                type="button"
              >
                <span aria-hidden="true">
                  {organization.retired_at
                    ? t("systemOrganizations.restore")
                    : t("systemOrganizations.retire")}
                </span>
                <span className="sr-only">
                  {t(
                    organization.retired_at
                      ? "systemOrganizations.restore"
                      : "systemOrganizations.retire",
                  )}{" "}
                  {organization.name}
                </span>
              </button>
            </li>
          ))}
        </ul>
      </section>
      {selectedOrganization ? (
        <OrganizationMemberships
          onUnauthenticated={onUnauthenticated}
          organizationId={selectedOrganization.id}
          organizationName={selectedOrganization.name}
          systemAdmin
          systemAdminManagement
          userId={userId}
        />
      ) : null}
    </section>
  );
}
