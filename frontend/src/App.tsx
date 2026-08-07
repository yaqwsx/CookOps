import { useCallback, useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";

import {
  type CurrentIdentity,
  type DevelopmentIdentity,
  createDevelopmentSession,
  createGoogleSession,
  getCurrentIdentity,
  getDevelopmentIdentities,
  logout,
} from "./api/auth";
import {
  type AvailableOrganization,
  getAvailableOrganizations,
  OrganizationRequestError,
} from "./api/organizations";
import { loadGoogleIdentityServices } from "./google-identity-services";
import { EventOverview } from "./events-overview";
import { EventPlanner } from "./event-planner";
import { EventShopping } from "./event-shopping";
import { RecipeCatalog } from "./recipe-catalog-view";
import { IngredientCatalog } from "./ingredient-catalog-view";
import { EventReceipts } from "./event-receipts";
import type { SupportedLocale } from "./i18n";
import { runtimeAuthentication } from "./runtime-config";
import { SynchronizationStatus } from "./synchronization-status";
import { useOutboxSynchronization } from "./sync-lifecycle";
import "./app.css";

const sections = ["events", "recipes", "ingredients", "settings"] as const;

const organizationPath =
  /^\/organizations\/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})(?:\/|$)/i;
const eventSectionPath =
  /^\/organizations\/([0-9a-f-]{36})\/events\/([0-9a-f-]{36})\/(planner|shopping|receipts)(?:\/([0-9a-f-]{36}))?$/i;
const recipeCatalogPath = /^\/organizations\/[0-9a-f-]{36}\/recipes$/i;
const ingredientCatalogPath = /^\/organizations\/[0-9a-f-]{36}\/ingredients$/i;

function eventOverviewPathFor(organizationId: string) {
  return `/organizations/${organizationId}/events`;
}

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
  | { status: "ready"; organizations: AvailableOrganization[] };

function LocalePicker() {
  const { i18n, t } = useTranslation();
  const locale = (i18n.resolvedLanguage ?? "cs") as SupportedLocale;

  return (
    <label className="locale-picker">
      <span>{t("shell.language")}</span>
      <select
        value={locale}
        onChange={(event) => void i18n.changeLanguage(event.target.value)}
      >
        <option value="cs">Čeština</option>
        <option value="en">English</option>
      </select>
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
          <LocalePicker />
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
          <LocalePicker />
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
          <LocalePicker />
        </div>
      </section>
    </main>
  );
}

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
  const [logoutError, setLogoutError] = useState(false);
  const [organizations, setOrganizations] = useState<OrganizationState>({
    status: "loading",
  });
  const [pathname, setPathname] = useState(window.location.pathname);
  useOutboxSynchronization(identity.id);

  const loadOrganizations = useCallback(async () => {
    setOrganizations({ status: "loading" });
    try {
      const available = await getAvailableOrganizations();
      setOrganizations({ status: "ready", organizations: available });
    } catch (error) {
      if (error instanceof OrganizationRequestError && error.status === 401) {
        onUnauthenticated();
        return;
      }
      setOrganizations({ status: "error" });
    }
  }, [onUnauthenticated]);

  useEffect(() => {
    function updatePathname() {
      setPathname(window.location.pathname);
    }
    window.addEventListener("popstate", updatePathname);
    return () => window.removeEventListener("popstate", updatePathname);
  }, []);

  useEffect(() => {
    void loadOrganizations();
  }, [loadOrganizations]);

  const pathOrganizationId = pathname
    .match(organizationPath)?.[1]
    ?.toLowerCase();
  const organizationId =
    pathOrganizationId &&
    (organizations.status !== "ready" ||
      organizations.organizations.some(({ id }) => id === pathOrganizationId))
      ? pathOrganizationId
      : undefined;

  useEffect(() => {
    if (
      organizations.status !== "ready" ||
      organizations.organizations.length === 0
    )
      return;
    if (organizationId) return;
    const firstOrganization = organizations.organizations[0];
    if (!firstOrganization) return;
    const nextPath = eventOverviewPathFor(firstOrganization.id);
    window.history.replaceState(null, "", nextPath);
    setPathname(nextPath);
  }, [organizationId, organizations]);

  function selectOrganization(nextOrganizationId: string) {
    const nextPath = eventOverviewPathFor(nextOrganizationId);
    window.history.pushState(null, "", nextPath);
    setPathname(nextPath);
  }

  function openEvent(eventId: string) {
    const nextPath = `/organizations/${organizationId}/events/${eventId}/planner`;
    window.history.pushState(null, "", nextPath);
    setPathname(nextPath);
  }

  function openRecipes(event: React.MouseEvent<HTMLAnchorElement>) {
    if (!organizationId) return;
    event.preventDefault();
    const nextPath = `/organizations/${organizationId}/recipes`;
    window.history.pushState(null, "", nextPath);
    setPathname(nextPath);
  }

  function openIngredients(event: React.MouseEvent<HTMLAnchorElement>) {
    if (!organizationId) return;
    event.preventDefault();
    const nextPath = `/organizations/${organizationId}/ingredients`;
    window.history.pushState(null, "", nextPath);
    setPathname(nextPath);
  }

  const eventSection = pathname.match(eventSectionPath);
  const eventId = eventSection?.[2]?.toLowerCase();
  const eventSectionName = eventSection?.[3]?.toLowerCase();
  const shoppingListId = eventSection?.[4]?.toLowerCase();
  const recipeCatalogOpen = recipeCatalogPath.test(pathname);
  const ingredientCatalogOpen = ingredientCatalogPath.test(pathname);

  function openShopping(eventId: string, listId?: string) {
    const nextPath = `/organizations/${organizationId}/events/${eventId}/shopping${listId ? `/${listId}` : ""}`;
    window.history.pushState(null, "", nextPath);
    setPathname(nextPath);
  }

  function openReceipts(eventId: string) {
    const nextPath = `/organizations/${organizationId}/events/${eventId}/receipts`;
    window.history.pushState(null, "", nextPath);
    setPathname(nextPath);
  }

  async function signOut() {
    setLogoutError(false);
    try {
      await onLogout();
    } catch {
      setLogoutError(true);
    }
  }

  return (
    <div className="app-shell">
      <header className="app-header">
        <a className="brand" href="#main">
          CookOps
        </a>

        <nav aria-label={t("shell.navigation")}>
          <ul className="primary-navigation">
            {sections.map((section) => (
              <li key={section}>
                <a
                  href={
                    (section === "recipes" || section === "ingredients") &&
                    organizationId
                      ? `/organizations/${organizationId}/${section}`
                      : `#${section}`
                  }
                  onClick={
                    section === "recipes"
                      ? openRecipes
                      : section === "ingredients"
                        ? openIngredients
                        : undefined
                  }
                >
                  {t(`shell.${section}`)}
                </a>
              </li>
            ))}
          </ul>
        </nav>

        <div className="header-actions">
          {organizations.status === "ready" ? (
            <label className="organization-picker">
              <span>{t("shell.organization")}</span>
              <select
                onChange={(event) => selectOrganization(event.target.value)}
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
          <SynchronizationStatus userId={identity.id} />
          <LocalePicker />
          <div className="user-menu">
            <span className="identity-name">{identity.display_name}</span>
            <button onClick={() => void signOut()} type="button">
              {t("shell.logout")}
            </button>
            {logoutError ? (
              <span role="alert">{t("shell.logoutError")}</span>
            ) : null}
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
        {organizations.status === "ready" &&
        organizations.organizations.length === 0 ? (
          <p role="status">{t("shell.noOrganizations")}</p>
        ) : null}
        <section className="introduction" aria-labelledby="app-heading">
          <p className="eyebrow">CookOps</p>
          <h1 id="app-heading">{t("shell.heading")}</h1>
          <p>{t("shell.introduction")}</p>
        </section>

        <div className="section-grid">
          {sections.map((section) => (
            <section id={section} className="section-card" key={section}>
              <h2 id={section === "events" ? "events-heading" : undefined}>
                {t(`shell.${section}`)}
              </h2>
              {section === "recipes" && organizationId && recipeCatalogOpen ? (
                <RecipeCatalog
                  onUnauthenticated={onUnauthenticated}
                  organizationId={organizationId}
                  userId={identity.id}
                />
              ) : section === "ingredients" &&
                organizationId &&
                ingredientCatalogOpen ? (
                <IngredientCatalog
                  onUnauthenticated={onUnauthenticated}
                  organizationId={organizationId}
                  userId={identity.id}
                />
              ) : section === "events" && organizationId ? (
                eventId && eventSectionName === "planner" ? (
                  <EventPlanner
                    eventId={eventId}
                    onOpenReceipts={() => openReceipts(eventId)}
                    onOpenShopping={() => openShopping(eventId)}
                    onUnauthenticated={onUnauthenticated}
                    organizationId={organizationId}
                    userId={identity.id}
                  />
                ) : eventId && eventSectionName === "shopping" ? (
                  <EventShopping
                    eventId={eventId}
                    onBack={() => openShopping(eventId)}
                    onOpenList={(listId) => openShopping(eventId, listId)}
                    onOpenPlanner={() => openEvent(eventId)}
                    onUnauthenticated={onUnauthenticated}
                    organizationId={organizationId}
                    shoppingListId={shoppingListId}
                    userId={identity.id}
                  />
                ) : eventId && eventSectionName === "receipts" ? (
                  <EventReceipts
                    eventId={eventId}
                    onBack={() => openEvent(eventId)}
                    onUnauthenticated={onUnauthenticated}
                    organizationId={organizationId}
                    userId={identity.id}
                  />
                ) : (
                  <EventOverview
                    onOpen={openEvent}
                    onUnauthenticated={onUnauthenticated}
                    organizationId={organizationId}
                    userId={identity.id}
                  />
                )
              ) : (
                <p>{t("shell.sectionPlaceholder")}</p>
              )}
            </section>
          ))}
        </div>
      </main>
    </div>
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
          if (active) setAuthentication({ status: "authenticated", identity });
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
