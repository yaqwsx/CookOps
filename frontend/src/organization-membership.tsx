import { useCallback, useEffect, useState } from "react";
import { useTranslation } from "react-i18next";

import {
  getMemberships,
  inviteMember,
  MembershipRequestError,
  removeMember,
  type OrganizationMembership,
} from "./api/memberships";

export function OrganizationMemberships({
  organizationId,
  userId,
  onUnauthenticated,
}: {
  organizationId: string;
  userId: string;
  onUnauthenticated: () => void;
}) {
  const { t } = useTranslation();
  const [memberships, setMemberships] = useState<
    OrganizationMembership[] | null
  >(null);
  const [email, setEmail] = useState("");
  const [error, setError] = useState<"unavailable" | "forbidden" | null>(null);
  const [pending, setPending] = useState(false);

  const load = useCallback(async () => {
    setError(null);
    try {
      setMemberships(await getMemberships(organizationId));
    } catch (reason) {
      if (reason instanceof MembershipRequestError && reason.status === 401) {
        onUnauthenticated();
        return;
      }
      setError(
        reason instanceof MembershipRequestError && reason.status === 404
          ? "forbidden"
          : "unavailable",
      );
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
      <p>{t("membership.onlineOnly")}</p>
      <form onSubmit={(event) => void invite(event)}>
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
      </form>
      {error === "unavailable" ? (
        <p role="alert">{t("membership.unavailable")}</p>
      ) : null}
      <ul aria-label={t("membership.heading")}>
        {memberships?.map((membership) => (
          <li key={membership.id}>
            <span>{membership.invitedEmail}</span>{" "}
            <span>{t(`membership.role.${membership.role}`)}</span>{" "}
            <span>{t(`membership.state.${membership.state}`)}</span>
            {membership.role === "member" && membership.state === "active" ? (
              <button
                disabled={pending}
                onClick={() => void remove(membership)}
                type="button"
              >
                {t("membership.remove")}
              </button>
            ) : null}
          </li>
        ))}
      </ul>
    </section>
  );
}
