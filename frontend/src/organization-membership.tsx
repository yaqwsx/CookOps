import { useCallback, useEffect, useState } from "react";
import { useTranslation } from "react-i18next";

import {
  getMemberships,
  assignOrganizationAdmin,
  inviteMember,
  MembershipRequestError,
  removeMember,
  revokeOrganizationAdmin,
  type OrganizationMembership,
} from "./api/memberships";

export function OrganizationMemberships({
  organizationId,
  userId,
  onUnauthenticated,
  systemAdmin,
  systemAdminManagement = false,
  organizationName,
}: {
  organizationId: string;
  userId: string;
  onUnauthenticated: () => void;
  systemAdmin: boolean;
  systemAdminManagement?: boolean;
  organizationName?: string;
}) {
  const { t } = useTranslation();
  const [memberships, setMemberships] = useState<
    OrganizationMembership[] | null
  >(null);
  const [email, setEmail] = useState("");
  const [error, setError] = useState<"unavailable" | "forbidden" | null>(null);
  const [pending, setPending] = useState(false);
  const [changed, setChanged] = useState(false);

  const load = useCallback(async (clearStatus = true): Promise<boolean> => {
    setError(null);
    if (clearStatus) setChanged(false);
    try {
      setMemberships(await getMemberships(organizationId));
      return true;
    } catch (reason) {
      if (reason instanceof MembershipRequestError && reason.status === 401) {
        onUnauthenticated();
        return false;
      }
      setError(
        reason instanceof MembershipRequestError && reason.status === 404
          ? "forbidden"
          : "unavailable",
      );
      return false;
    }
  }, [onUnauthenticated, organizationId]);

  useEffect(() => {
    setMemberships(null);
    void load();
  }, [load]);

  async function invite(event: React.FormEvent) {
    event.preventDefault();
    if (!email.trim() || pending) return;
    setPending(true);
    setError(null);
    try {
      await inviteMember(organizationId, userId, email);
      setEmail("");
      await load();
    } catch (reason) {
      if (reason instanceof MembershipRequestError && reason.status === 401)
        onUnauthenticated();
      else setError("unavailable");
    } finally {
      setPending(false);
    }
  }

  async function remove(membership: OrganizationMembership) {
    if (
      pending ||
      !window.confirm(
        t("membership.removeConfirm", { email: membership.invitedEmail }),
      )
    )
      return;
    setPending(true);
    setError(null);
    try {
      await removeMember(organizationId, userId, membership.id);
      await load();
    } catch (reason) {
      if (reason instanceof MembershipRequestError && reason.status === 401)
        onUnauthenticated();
      else setError("unavailable");
    } finally {
      setPending(false);
    }
  }

  async function changeRole(membership: OrganizationMembership) {
    if (pending) return;
    if (systemAdminManagement && membership.role === "organization_admin" && !window.confirm(t("membership.revokeConfirm", { email: membership.invitedEmail }))) return;
    setPending(true);
    setError(null);
    setChanged(false);
    try {
      const change = membership.role === "member" ? assignOrganizationAdmin : revokeOrganizationAdmin;
      await change(organizationId, userId, membership.id);
      if (await load(false)) setChanged(true);
    } catch (reason) {
      if (reason instanceof MembershipRequestError && reason.status === 401) onUnauthenticated();
      else setError("unavailable");
    } finally {
      setPending(false);
    }
  }

  if (error === "forbidden")
    return <p role="status">{t("membership.forbidden")}</p>;
  if (!memberships && !error)
    return <p role="status">{t("membership.loading")}</p>;
  return (
    <section
      aria-labelledby="memberships-heading"
      className="membership-settings"
    >
      <h3 id="memberships-heading">{t("membership.heading")}</h3>
      {organizationName ? <p>{t("membership.managingOrganization", { name: organizationName })}</p> : null}
      {!systemAdminManagement ? <p>{t("membership.onlineOnly")}</p> : null}
      {!systemAdminManagement ? <form onSubmit={(event) => void invite(event)}>
        <label>
          <span>{t("membership.email")}</span>
          <input
            autoComplete="email"
            disabled={pending}
            onChange={(event) => setEmail(event.target.value)}
            required
            type="email"
            value={email}
          />
        </label>
        <button disabled={pending} type="submit">
          {t("membership.invite")}
        </button>
      </form> : null}
      {error === "unavailable" ? (
        <p role="alert">{t("membership.unavailable")}</p>
      ) : null}
      {pending ? <p role="status">{t("membership.pending")}</p> : null}
      {changed ? <p role="status">{t("membership.changed")}</p> : null}
      {memberships?.length === 0 ? <p role="status">{t("membership.noMembers")}</p> : null}
      {memberships && (!memberships.length && systemAdminManagement || memberships.length > 0 && !memberships.some((membership) => membership.role === "organization_admin" && membership.state === "active")) ? <p role="status">{t("membership.noAdmins")}</p> : null}
      <ul aria-label={t("membership.heading")}>
        {memberships?.map((membership) => (
          <li key={membership.id}>
            <span>{membership.invitedEmail}</span>{" "}
            <span>{t(`membership.role.${membership.role}`)}</span>{" "}
            <span>{t(`membership.state.${membership.state}`)}</span>
            {!systemAdminManagement && membership.role === "member" && membership.state === "active" ? (
              <button
                disabled={pending}
                onClick={() => void remove(membership)}
                type="button"
              >
                {t("membership.remove")}
              </button>
            ) : null}
            {systemAdmin && membership.state === "active" ? (
              <button disabled={pending} onClick={() => void changeRole(membership)} type="button">
                {membership.role === "member" ? t("membership.assignAdmin") : t("membership.revokeAdmin")}
              </button>
            ) : null}
          </li>
        ))}
      </ul>
    </section>
  );
}
