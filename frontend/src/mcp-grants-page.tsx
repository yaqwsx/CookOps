import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { AuthenticationRequestError, getAuthorizedGrants, revokeAuthorizedGrant, type AuthorizedGrant } from "./api/auth";

export function McpGrantsPage({ onUnauthenticated }: { onUnauthenticated: () => void }) {
  const { t } = useTranslation();
  const [grants, setGrants] = useState<AuthorizedGrant[]>([]);
  const [error, setError] = useState(false);
  useEffect(() => { void getAuthorizedGrants().then(setGrants).catch((e: unknown) => { if (e instanceof AuthenticationRequestError && e.status === 401) onUnauthenticated(); else setError(true); }); }, [onUnauthenticated]);
  return <section aria-labelledby="mcp-grants-heading"><h1 id="mcp-grants-heading">{t("mcpGrants.heading")}</h1>{error ? <p role="alert">{t("mcpGrants.error")}</p> : null}{grants.length === 0 && !error ? <p>{t("mcpGrants.empty")}</p> : <ul>{grants.map((grant) => <li key={grant.handle}><span>{grant.clientId}</span>{grant.expiresAt ? <time dateTime={new Date(grant.expiresAt * 1000).toISOString()}>{new Date(grant.expiresAt * 1000).toLocaleDateString()}</time> : null}<button type="button" onClick={() => { if (!window.confirm(t("mcpGrants.confirm"))) return; void revokeAuthorizedGrant(grant.handle).then(() => setGrants((current) => current.filter((item) => item.handle !== grant.handle))).catch(() => setError(true)); }}>{t("mcpGrants.revoke")}</button></li>)}</ul>}</section>;
}
