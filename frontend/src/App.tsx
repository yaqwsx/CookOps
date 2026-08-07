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
import { loadGoogleIdentityServices } from "./google-identity-services";
import { EventOverview } from "./events-overview";
import type { SupportedLocale } from "./i18n";
import { runtimeAuthentication } from "./runtime-config";
import { SynchronizationStatus } from "./synchronization-status";
import { useOutboxSynchronization } from "./sync-lifecycle";
import "./app.css";

const sections = ["events", "recipes", "ingredients", "settings"] as const;

const eventOverviewPath =
  /^\/organizations\/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\/events\/?$/i;

function organizationIdFromEventOverviewPath() {
  return window.location.pathname.match(eventOverviewPath)?.[1];
}

type AuthenticationState =
  | { status: "loading" }
  | { status: "startupError" }
  | { status: "developmentLogin"; identities: DevelopmentIdentity[] | null }
  | { status: "googleLogin"; googleClientId: string }
  | { status: "configurationError" }
  | { status: "authenticated"; identity: CurrentIdentity };

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
  const organizationId = organizationIdFromEventOverviewPath();
  useOutboxSynchronization(identity.id);

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
                <a href={`#${section}`}>{t(`shell.${section}`)}</a>
              </li>
            ))}
          </ul>
        </nav>

        <div className="header-actions">
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
              {section === "events" && organizationId ? (
                <EventOverview
                  onUnauthenticated={onUnauthenticated}
                  organizationId={organizationId}
                  userId={identity.id}
                />
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
