import { createContext, useCallback, useEffect, useRef, useState } from "react";
import {
  Link,
  Outlet,
  useBlocker,
  useNavigate,
  useParams,
  useRouterState,
} from "@tanstack/react-router";
import { useTranslation } from "react-i18next";

import {
  type CurrentIdentity,
  createDevelopmentSession,
  createGoogleSession,
  type DevelopmentIdentity,
  getCurrentIdentity,
  setCurrentIdentityLocale,
  getDevelopmentIdentities,
  logout,
} from "./api/auth";
import {
  type AvailableOrganization,
  getAvailableOrganizations,
  OrganizationRequestError,
} from "./api/organizations";
import { loadGoogleIdentityServices } from "./google-identity-services";
import type { SupportedLocale } from "./i18n";
import appI18n from "./i18n";
import { runtimeAuthentication } from "./runtime-config";
import { useOutboxSynchronization } from "./sync-lifecycle";
import { SynchronizationStatus } from "./synchronization-status";
import { getSystemAdministrationAccess } from "./api/system-organizations";
import {
  hasValidOfflineAuthorization,
  localDb,
  readCachedOrganizations,
} from "./local-db";
import "./app.css";

type AuthenticationState =
  | { status: "loading" }
  | { status: "startupError" }
  | { status: "developmentLogin"; identities: DevelopmentIdentity[] | null }
  | { status: "googleLogin"; googleClientId: string }
  | { status: "configurationError" }
  | { status: "authenticated"; identity: CurrentIdentity };

type OrganizationState =
  | { status: "loading" }
  | { status: "error" }
  | { status: "offlineBlocked" }
  | { status: "ready"; organizations: AvailableOrganization[] };

function applyIdentityLocale(
  i18n: {
    resolvedLanguage?: string;
    changeLanguage: (locale: string) => Promise<unknown>;
  },
  identity: CurrentIdentity,
) {
  const next = identity.preferred_locale === "en" ? "en" : "cs";
  return (i18n.resolvedLanguage ?? "cs") === next
    ? Promise.resolve()
    : i18n.changeLanguage(next);
}

function LocalePicker({ persist }: { persist: boolean }) {
  const { i18n, t } = useTranslation();
  const [error, setError] = useState(false);
  const mounted = useRef(true);
  useEffect(
    () => () => {
      mounted.current = false;
    },
    [],
  );
  const locale = (i18n.resolvedLanguage ?? "cs") as SupportedLocale;

  return (
    <label className="locale-picker">
      <span>{t("shell.language")}</span>
      <select
        value={locale}
        onChange={(event) => {
          const selected = event.target.value as SupportedLocale;
          setError(false);
          void i18n.changeLanguage(selected);
          if (persist)
            void setCurrentIdentityLocale(selected).catch(() => {
              if (mounted.current) setError(true);
            });
        }}
      >
        <option value="cs">Čeština</option>
        <option value="en">English</option>
      </select>
      {error ? <span role="alert">{t("shell.localeError")}</span> : null}
    </label>
  );
}

function DevelopmentLogin({
  identities,
  onSignIn,
  onRetry,
}: {
  identities: DevelopmentIdentity[] | null;
  onSignIn: (subject: string) => Promise<void>;
  onRetry: () => void;
}) {
  const { t } = useTranslation();
  const [error, setError] = useState(false);
  const [pendingSubject, setPendingSubject] = useState<string | null>(null);

  async function signIn(subject: string) {
    setError(false);
    setPendingSubject(subject);
    try {
      await onSignIn(subject);
    } catch {
      setError(true);
      setPendingSubject(null);
    }
  }

  return (
    <main className="login-main" id="main">
      <section className="login-card" aria-labelledby="login-heading">
        <p className="eyebrow">CookOps</p>
        <h1 id="login-heading">{t("developmentLogin.title")}</h1>
        {identities ? (
          <>
            <aside className="development-warning">
              {t("developmentLogin.warning")}
            </aside>
            <p>{t("developmentLogin.introduction")}</p>
            <fieldset className="identity-list">
              <legend>{t("developmentLogin.identities")}</legend>
              {identities.map((identity) => (
                <button
                  className="identity-button"
                  disabled={pendingSubject !== null}
                  key={identity.subject}
                  onClick={() => void signIn(identity.subject)}
                  type="button"
                >
                  {t("developmentLogin.signInAs", {
                    name: identity.display_name,
                  })}
                </button>
              ))}
            </fieldset>
            {error ? <p role="alert">{t("developmentLogin.error")}</p> : null}
          </>
        ) : (
          <div role="alert">
            <p>{t("developmentLogin.unavailable")}</p>
            <button onClick={onRetry} type="button">
              {t("authentication.retry")}
            </button>
          </div>
        )}
        <div className="login-locale-picker">
          <LocalePicker persist={false} />
        </div>
      </section>
    </main>
  );
}

function GoogleLogin({
  googleClientId,
  onSignIn,
}: {
  googleClientId: string;
  onSignIn: (idToken: string) => Promise<void>;
}) {
  const { i18n, t } = useTranslation();
  const [retry, setRetry] = useState(0);

  return (
    <main className="login-main" id="main">
      <section className="login-card" aria-labelledby="google-login-heading">
        <p className="eyebrow">CookOps</p>
        <h1 id="google-login-heading">{t("googleLogin.title")}</h1>
        <p>{t("googleLogin.introduction")}</p>
        <GoogleIdentityButton
          googleClientId={googleClientId}
          key={retry}
          locale={i18n.resolvedLanguage ?? "cs"}
          onRetry={() => setRetry((attempt) => attempt + 1)}
          onSignIn={onSignIn}
        />
        <div className="login-locale-picker">
          <LocalePicker persist={false} />
        </div>
      </section>
    </main>
  );
}

function GoogleIdentityButton({
  googleClientId,
  locale,
  onRetry,
  onSignIn,
}: {
  googleClientId: string;
  locale: string;
  onRetry: () => void;
  onSignIn: (idToken: string) => Promise<void>;
}) {
  const { t } = useTranslation();
  const button = useRef<HTMLDivElement>(null);
  const [status, setStatus] = useState<
    "loading" | "ready" | "loadError" | "signInError"
  >("loading");

  useEffect(() => {
    let active = true;
    setStatus("loading");
    void loadGoogleIdentityServices()
      .then((google) => {
        if (!active || !button.current) return;
        google.accounts.id.initialize({
          client_id: googleClientId,
          callback: (response) => {
            if (!response.credential) {
              if (active) setStatus("signInError");
              return;
            }
            if (active) setStatus("loading");
            void onSignIn(response.credential).catch(() => {
              if (active) setStatus("signInError");
            });
          },
        });
        button.current.replaceChildren();
        google.accounts.id.renderButton(button.current, {
          theme: "outline",
          size: "large",
          text: "signin_with",
          locale,
        });
        setStatus("ready");
      })
      .catch(() => {
        if (active) setStatus("loadError");
      });
    return () => {
      active = false;
    };
  }, [googleClientId, locale, onSignIn]);

  return (
    <>
      {status === "loading" ? (
        <p aria-live="polite" role="status">
          {t("googleLogin.loading")}
        </p>
      ) : null}
      {status === "loadError" ? (
        <div role="alert">
          <p>{t("googleLogin.loadError")}</p>
          <button onClick={onRetry} type="button">
            {t("googleLogin.retry")}
          </button>
        </div>
      ) : null}
      {status === "signInError" ? (
        <div role="alert">
          <p>{t("googleLogin.signInError")}</p>
          <button onClick={onRetry} type="button">
            {t("googleLogin.retry")}
          </button>
        </div>
      ) : null}
      <div
        className="google-sign-in"
        hidden={status !== "ready"}
        ref={button}
      />
    </>
  );
}

function AuthenticationStatus({
  state,
  onRetry,
}: {
  state: "loading" | "startupError" | "configurationError";
  onRetry: () => void;
}) {
  const { t } = useTranslation();

  return (
    <main className="login-main" id="main">
      <section className="login-card" aria-labelledby="authentication-heading">
        <p className="eyebrow">CookOps</p>
        <h1 id="authentication-heading">
          {state === "loading"
            ? t("authentication.loading")
            : state === "startupError"
              ? t("authentication.startupError")
              : t("authentication.configurationError")}
        </h1>
        {state === "loading" ? (
          <p aria-live="polite" role="status">
            {t("authentication.loading")}
          </p>
        ) : state === "startupError" ? (
          <button onClick={onRetry} type="button">
            {t("authentication.retry")}
          </button>
        ) : null}
        <div className="login-locale-picker">
          <LocalePicker persist={false} />
        </div>
      </section>
    </main>
  );
}

export type RouteShell = {
  identity: CurrentIdentity;
  organizationId?: string;
  organizations: OrganizationState;
  routeAccess: "allowed" | "loading" | "blocked" | "denied";
  systemAdmin: boolean;
  discardToken: number;
  ingredientDiscardToken: number;
  reportRecipeDirty: (dirty: boolean) => void;
  reportIngredientDirty: (dirty: boolean) => void;
  refreshOrganizations: () => void;
  onUnauthenticated: () => void;
};
export const RouteShellContext = createContext<RouteShell>(
  null as unknown as RouteShell,
);

function AuthenticatedShell({
  identity,
  onLogout,
  onUnauthenticated,
}: {
  identity: CurrentIdentity;
  onLogout: () => Promise<void>;
  onUnauthenticated: () => void;
}) {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const params = useParams({ strict: false }) as { organizationId?: string };
  const routeIds = useRouterState({
    select: (state) => state.matches.map((match) => match.routeId),
  });
  const pathname = useRouterState({
    select: (state) => state.location.pathname,
  });
  const [logoutError, setLogoutError] = useState(false);
  const [systemAdmin, setSystemAdmin] = useState(false);
  const [organizations, setOrganizations] = useState<OrganizationState>({
    status: "loading",
  });
  const [recipeDirty, setRecipeDirty] = useState(false);
  const [ingredientDirty, setIngredientDirty] = useState(false);
  const [discardToken, setDiscardToken] = useState(0);
  const [ingredientDiscardToken, setIngredientDiscardToken] = useState(0);
  const redirecting = useRef(false);
  const organizationId = params.organizationId?.toLowerCase();
  const routeAccess = organizationId
    ? organizations.status === "ready"
      ? organizations.organizations.some(({ id }) => id === organizationId)
        ? "allowed"
        : "denied"
      : organizations.status === "offlineBlocked"
        ? "blocked"
        : "loading"
    : "allowed";
  useOutboxSynchronization(identity.id, onUnauthenticated);
  const loadOrganizations = useCallback(async () => {
    setOrganizations({ status: "loading" });
    try {
      setOrganizations({
        status: "ready",
        organizations: await getAvailableOrganizations(),
      });
    } catch (error) {
      if (error instanceof OrganizationRequestError && error.status === 401) {
        onUnauthenticated();
        return;
      }
      if (!navigator.onLine) {
        const [cached, metadata] = await Promise.all([
          readCachedOrganizations(identity.id),
          localDb.syncMetadata.where("userId").equals(identity.id).toArray(),
        ]);
        const valid = cached.filter((organization) =>
          metadata.some(
            (entry) =>
              entry.organizationId === organization.id &&
              hasValidOfflineAuthorization(entry.lastAuthorizedAt),
          ),
        );
        setOrganizations(
          valid.length
            ? { status: "ready", organizations: valid }
            : { status: "offlineBlocked" },
        );
        return;
      }
      setOrganizations({ status: "error" });
    }
  }, [identity.id, onUnauthenticated]);
  useEffect(() => {
    void loadOrganizations();
  }, [loadOrganizations]);
  useEffect(() => {
    void getSystemAdministrationAccess()
      .then(setSystemAdmin)
      .catch(() => setSystemAdmin(false));
  }, []);
  useEffect(() => {
    if (
      organizations.status !== "ready" ||
      organizationId ||
      pathname !== "/" ||
      redirecting.current ||
      routeIds.includes("/system/organizations") ||
      routeIds.includes("/auth/mcp-grants")
    )
      return;
    const first = organizations.organizations[0];
    if (first) {
      redirecting.current = true;
      void navigate({
        to: "/organizations/$organizationId/events",
        params: { organizationId: first.id },
        replace: true,
      });
    }
  }, [navigate, organizationId, organizations, pathname, routeIds]);
  const blocker = useBlocker({
    enableBeforeUnload: () => recipeDirty || ingredientDirty,
    shouldBlockFn: ({ current, next }) => {
      if (
        current.routeId === next.routeId &&
        JSON.stringify(current.params) === JSON.stringify(next.params) &&
        JSON.stringify(current.search) === JSON.stringify(next.search)
      )
        return false;
      return recipeDirty || ingredientDirty;
    },
    withResolver: true,
  });
  useEffect(() => {
    if (blocker.status !== "blocked") return;
    if (
      window.confirm(
        t(
          ingredientDirty && !recipeDirty
            ? "ingredientsCatalog.discardChanges"
            : "recipesCatalog.discardChanges",
        ),
      )
    ) {
      setRecipeDirty(false);
      setIngredientDirty(false);
      setDiscardToken((token) => token + 1);
      setIngredientDiscardToken((token) => token + 1);
      blocker.proceed();
    } else blocker.reset();
  }, [blocker, ingredientDirty, recipeDirty, t]);
  const shell: RouteShell = {
    identity,
    organizationId,
    organizations,
    routeAccess,
    systemAdmin,
    discardToken,
    ingredientDiscardToken,
    reportRecipeDirty: setRecipeDirty,
    reportIngredientDirty: setIngredientDirty,
    refreshOrganizations: () => void loadOrganizations(),
    onUnauthenticated,
  };
  return (
    <RouteShellContext.Provider value={shell}>
      <div className="app-shell">
        <header className="app-header">
          <Link className="brand" to="/">
            CookOps
          </Link>
          <nav aria-label={t("shell.navigation")}>
            {organizationId ? (
              <ul className="primary-navigation">
                <li>
                  <Link
                    to="/organizations/$organizationId/events"
                    params={{ organizationId }}
                  >
                    {t("shell.events")}
                  </Link>
                </li>
                <li>
                  <Link
                    to="/organizations/$organizationId/recipes"
                    params={{ organizationId }}
                  >
                    {t("shell.recipes")}
                  </Link>
                </li>
                <li>
                  <Link
                    to="/organizations/$organizationId/ingredients"
                    params={{ organizationId }}
                  >
                    {t("shell.ingredients")}
                  </Link>
                </li>
                <li>
                  <Link
                    to="/organizations/$organizationId/settings"
                    params={{ organizationId }}
                  >
                    {t("shell.settings")}
                  </Link>
                </li>
              </ul>
            ) : null}
          </nav>
          <div className="header-actions">
            {organizations.status === "ready" ? (
              <label className="organization-picker">
                <span>{t("shell.organization")}</span>
                <select
                  onChange={(event) =>
                    void navigate({
                      to: "/organizations/$organizationId/events",
                      params: { organizationId: event.target.value },
                    })
                  }
                  value={organizationId ?? ""}
                >
                  {organizations.organizations.map((organization) => (
                    <option key={organization.id} value={organization.id}>
                      {organization.name}
                    </option>
                  ))}
                </select>
              </label>
            ) : null}
            <SynchronizationStatus
              organizationId={organizationId}
              userId={identity.id}
            />
            <LocalePicker persist />
            <div className="user-menu">
              <span className="identity-name">{identity.display_name}</span>
              <button
                onClick={() =>
                  void onLogout().catch(() => setLogoutError(true))
                }
                type="button"
              >
                {t("shell.logout")}
              </button>
              {logoutError ? (
                <span role="alert">{t("shell.logoutError")}</span>
              ) : null}
              {systemAdmin ? (
                <Link to="/system/organizations">
                  {t("systemOrganizations.navigation")}
                </Link>
              ) : null}
              <Link to="/auth/mcp-grants">{t("mcpGrants.navigation")}</Link>
            </div>
          </div>
        </header>
        <main id="main" className="app-main">
          {organizations.status === "loading" ? (
            <p aria-live="polite" role="status">
              {t("shell.organizationsLoading")}
            </p>
          ) : null}
          {organizations.status === "error" ? (
            <div role="alert">
              <p>{t("shell.organizationsError")}</p>
              <button onClick={() => void loadOrganizations()} type="button">
                {t("authentication.retry")}
              </button>
            </div>
          ) : null}
          {routeAccess === "blocked" ? (
            <div
              role="alert"
              aria-live="assertive"
              className="connectivity-gate"
            >
              <p>{t("shell.authorizationRequiredOffline")}</p>
              <button onClick={() => void loadOrganizations()} type="button">
                {t("authentication.retry")}
              </button>
            </div>
          ) : null}
          {organizations.status === "ready" &&
          organizations.organizations.length === 0 ? (
            <p role="status">{t("shell.noOrganizations")}</p>
          ) : null}
          <section
            className="introduction"
            aria-labelledby="app-heading"
            hidden={organizations.status === "offlineBlocked"}
          >
            <p className="eyebrow">CookOps</p>
            <h1 id="app-heading">{t("shell.heading")}</h1>
            <p>{t("shell.introduction")}</p>
          </section>
          <Outlet />
        </main>
      </div>
    </RouteShellContext.Provider>
  );
}

export function App() {
  const { i18n } = useTranslation();
  const locale = (i18n.resolvedLanguage ?? "cs") as SupportedLocale;
  const [authentication, setAuthentication] = useState<AuthenticationState>({
    status: "loading",
  });
  const retryAuthentication = useCallback(
    () => setAuthentication({ status: "loading" }),
    [],
  );
  useEffect(() => {
    document.documentElement.lang = locale;
  }, [locale]);

  useEffect(() => {
    if (authentication.status !== "loading") return;

    let active = true;
    void getCurrentIdentity()
      .then(async (identity) => {
        if (identity) {
          if (active) {
            await applyIdentityLocale(appI18n, identity);
            if (!active) return;
            setAuthentication({ status: "authenticated", identity });
          }
          return;
        }
        const provider = runtimeAuthentication();
        if (provider?.provider === "google") {
          if (active) {
            setAuthentication({
              status: "googleLogin",
              googleClientId: provider.googleClientId,
            });
          }
          return;
        }
        if (provider === null) {
          if (active) setAuthentication({ status: "configurationError" });
          return;
        }
        try {
          const identities = await getDevelopmentIdentities();
          if (active)
            setAuthentication({ status: "developmentLogin", identities });
        } catch {
          if (active)
            setAuthentication({ status: "developmentLogin", identities: null });
        }
      })
      .catch(() => {
        if (active) setAuthentication({ status: "startupError" });
      });
    return () => {
      active = false;
    };
  }, [authentication.status]);

  if (authentication.status === "loading") {
    return (
      <AuthenticationStatus
        onRetry={() => setAuthentication({ status: "loading" })}
        state="loading"
      />
    );
  }
  if (
    authentication.status === "startupError" ||
    authentication.status === "configurationError"
  ) {
    return (
      <AuthenticationStatus
        onRetry={() => {
          setAuthentication({ status: "loading" });
        }}
        state={authentication.status}
      />
    );
  }
  if (authentication.status === "developmentLogin") {
    return (
      <DevelopmentLogin
        identities={authentication.identities}
        onRetry={() => setAuthentication({ status: "loading" })}
        onSignIn={async (subject) => {
          await createDevelopmentSession(subject);
          const identity = await getCurrentIdentity();
          if (!identity) throw new Error("session was not established");
          await applyIdentityLocale(i18n, identity);
          setAuthentication({ status: "authenticated", identity });
        }}
      />
    );
  }
  if (authentication.status === "googleLogin") {
    return (
      <GoogleLogin
        googleClientId={authentication.googleClientId}
        onSignIn={async (idToken) => {
          await createGoogleSession(idToken);
          const identity = await getCurrentIdentity();
          if (!identity) throw new Error("session was not established");
          await applyIdentityLocale(i18n, identity);
          setAuthentication({ status: "authenticated", identity });
        }}
      />
    );
  }
  return (
    <AuthenticatedShell
      identity={authentication.identity}
      onUnauthenticated={retryAuthentication}
      onLogout={async () => {
        await logout();
        setAuthentication({ status: "loading" });
      }}
    />
  );
}
