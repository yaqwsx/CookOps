import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { App } from "./App";
import i18n, { defaultLocale } from "./i18n";

vi.mock("./event-costs-page", () => ({
  EventCostsPage: ({
    onBack,
    onOpenReceipts,
  }: {
    onBack: () => void;
    onOpenReceipts: () => void;
  }) => (
    <section aria-label="costs-route">
      <button onClick={onBack} type="button">Back to planner</button>
      <button onClick={onOpenReceipts} type="button">Receipts</button>
    </section>
  ),
}));

const alice = {
  id: "a6a58bd6-214e-49af-8fae-e5f974bf8e08",
  display_name: "Alice Member",
  verified_email: "alice@example.test",
};
const organizations = {
  organizations: [
    {
      id: "5ce17d2f-8365-4b1f-a80b-34d10425d51c",
      name: "Development organization",
    },
  ],
};
const primaryOrganization = organizations.organizations[0];
if (!primaryOrganization)
  throw new Error("Missing primary organization fixture.");

function response(body: object | null, status = 200) {
  return new Response(body === null ? null : JSON.stringify(body), {
    status,
    headers: body === null ? undefined : { "content-type": "application/json" },
  });
}

function mockAnonymousDevelopmentSession({
  logoutFails = false,
  organizationList = organizations,
  organizationUnauthorized = false,
} = {}) {
  window.COOKOPS_RUNTIME_CONFIG = { authentication: { provider: "dummy" } };
  let signedIn = false;
  let accessRevoked = false;
  const fetchMock = vi.fn(
    async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input);
      if (path === "/auth/session") {
        return signedIn && !accessRevoked
          ? response(alice)
          : response({ detail: "not authenticated" }, 401);
      }
      if (path === "/api/v1/organizations") {
        if (!organizationUnauthorized) return response(organizationList);
        accessRevoked = true;
        return response({ detail: "not authenticated" }, 401);
      }
      if (path === "/auth/dummy/identities") {
        return response({
          identities: [
            { subject: "dummy-alice", display_name: "Alice Member" },
            { subject: "dummy-zoe", display_name: "Zoe No Access" },
          ],
        });
      }
      if (path === "/auth/dummy/session" && init?.method === "POST") {
        signedIn = true;
        return response(null, 204);
      }
      if (path === "/auth/session/logout" && init?.method === "POST") {
        if (logoutFails) return response({ detail: "unavailable" }, 503);
        signedIn = false;
        return response(null, 204);
      }
      throw new Error(`Unexpected request: ${path}`);
    },
  );
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

function mockAnonymousGoogleSession() {
  let signedIn = false;
  const fetchMock = vi.fn(
    async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input);
      if (path === "/auth/session") {
        return signedIn
          ? response(alice)
          : response({ detail: "not authenticated" }, 401);
      }
      if (path === "/api/v1/organizations") return response(organizations);
      if (path === "/auth/google/session" && init?.method === "POST") {
        signedIn = true;
        return response(null, 204);
      }
      throw new Error(`Unexpected request: ${path}`);
    },
  );
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

describe("development authentication", () => {
  beforeEach(async () => {
    await i18n.changeLanguage(defaultLocale);
    delete window.COOKOPS_RUNTIME_CONFIG;
    delete window.google;
    window.history.replaceState(null, "", "/");
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    delete window.COOKOPS_RUNTIME_CONFIG;
    delete window.google;
  });

  it("starts in Czech and presents only named development identities", async () => {
    const user = userEvent.setup();
    mockAnonymousDevelopmentSession();
    render(<App />);

    expect(
      await screen.findByRole("heading", { name: "Vývojové přihlášení" }),
    ).toBeInTheDocument();
    expect(
      screen.getByText(
        "Vývojová autentizace je aktivní. Používejte ji pouze pro místní vývoj a automatické testy.",
      ),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Přihlásit se jako Alice Member" }),
    ).toBeInTheDocument();
    expect(document.documentElement).toHaveAttribute("lang", "cs");

    await user.selectOptions(
      screen.getByRole("combobox", { name: "Jazyk" }),
      "en",
    );
    expect(
      screen.getByRole("heading", { name: "Development sign-in" }),
    ).toBeInTheDocument();
    expect(screen.getByRole("combobox", { name: "Language" })).toHaveValue(
      "en",
    );
  });

  it("renders a localized recoverable error when the session check cannot start", async () => {
    window.COOKOPS_RUNTIME_CONFIG = { authentication: { provider: "dummy" } };
    let failed = false;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        if (String(input) === "/auth/session" && !failed) {
          failed = true;
          throw new Error("network unavailable");
        }
        if (String(input) === "/auth/session") {
          return response({ detail: "not authenticated" }, 401);
        }
        if (String(input) === "/auth/dummy/identities") {
          return response({ identities: [] });
        }
        throw new Error(`Unexpected request: ${String(input)}`);
      }),
    );
    const user = userEvent.setup();
    render(<App />);

    expect(
      await screen.findByRole("heading", {
        name: "Přihlášení se nepodařilo načíst.",
      }),
    ).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Zkusit znovu" }));
    expect(
      await screen.findByRole("heading", { name: "Vývojové přihlášení" }),
    ).toBeInTheDocument();
  });

  it("requires an explicit runtime authentication provider", async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      if (String(input) === "/auth/session") {
        return response({ detail: "not authenticated" }, 401);
      }
      throw new Error(`Unexpected request: ${String(input)}`);
    });
    vi.stubGlobal("fetch", fetchMock);
    render(<App />);

    expect(
      await screen.findByRole("heading", {
        name: "Přihlášení není správně nakonfigurováno.",
      }),
    ).toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("retries an unavailable dummy identity list", async () => {
    window.COOKOPS_RUNTIME_CONFIG = { authentication: { provider: "dummy" } };
    let listAttempts = 0;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        if (String(input) === "/auth/session") {
          return response({ detail: "not authenticated" }, 401);
        }
        if (String(input) === "/auth/dummy/identities") {
          listAttempts += 1;
          return listAttempts === 1
            ? response({ detail: "unavailable" }, 503)
            : response({
                identities: [
                  { subject: "dummy-alice", display_name: "Alice Member" },
                ],
              });
        }
        throw new Error(`Unexpected request: ${String(input)}`);
      }),
    );
    const user = userEvent.setup();
    render(<App />);

    expect(
      await screen.findByText("Vývojová autentizace není k dispozici."),
    ).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Zkusit znovu" }));
    expect(
      await screen.findByRole("button", {
        name: "Přihlásit se jako Alice Member",
      }),
    ).toBeInTheDocument();
    expect(listAttempts).toBe(2);
  });

  it("establishes a cookie-backed session and renders the authenticated shell", async () => {
    const user = userEvent.setup();
    const fetchMock = mockAnonymousDevelopmentSession();
    render(<App />);

    await user.click(
      await screen.findByRole("button", {
        name: "Přihlásit se jako Alice Member",
      }),
    );

    expect(await screen.findByText("Alice Member")).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Odhlásit se" }),
    ).toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledWith(
      "/auth/dummy/session",
      expect.objectContaining({
        method: "POST",
        credentials: "same-origin",
        body: JSON.stringify({ subject: "dummy-alice" }),
        headers: { "content-type": "application/json" },
      }),
    );
    const sessionRequests = fetchMock.mock.calls.filter(
      ([path]) => path === "/auth/session",
    );
    expect(sessionRequests).toHaveLength(2);
    expect(sessionRequests[1]?.[1]).toEqual({ credentials: "same-origin" });
    expect(
      await screen.findByRole("combobox", { name: "Organizace" }),
    ).toHaveValue(primaryOrganization.id);
    await waitFor(() => {
      expect(window.location.pathname).toBe(
        `/organizations/${primaryOrganization.id}/events`,
      );
    });
  });

  it("switches organizations through the existing events route", async () => {
    const user = userEvent.setup();
    const secondOrganization = {
      id: "a6a58bd6-214e-49af-8fae-e5f974bf8e08",
      name: "Second organization",
    };
    mockAnonymousDevelopmentSession({
      organizationList: {
        organizations: [...organizations.organizations, secondOrganization],
      },
    });
    render(<App />);

    await user.click(
      await screen.findByRole("button", {
        name: "Přihlásit se jako Alice Member",
      }),
    );
    await user.selectOptions(
      await screen.findByRole("combobox", { name: "Organizace" }),
      secondOrganization.id,
    );
    expect(window.location.pathname).toBe(
      `/organizations/${secondOrganization.id}/events`,
    );
  });

  it("keeps a case-insensitive bookmarked organization route", async () => {
    const user = userEvent.setup();
    window.history.replaceState(
      null,
      "",
      `/organizations/${primaryOrganization.id.toUpperCase()}/recipes`,
    );
    mockAnonymousDevelopmentSession();
    render(<App />);

    await user.click(
      await screen.findByRole("button", {
        name: "Přihlásit se jako Alice Member",
      }),
    );
    await screen.findByRole("combobox", { name: "Organizace" });
    expect(window.location.pathname).toBe(
      `/organizations/${primaryOrganization.id.toUpperCase()}/recipes`,
    );
  });

  it("renders a bookmarked costs route and navigates to receipts or planner", async () => {
    const user = userEvent.setup();
    const eventId = "6ce17d2f-8365-4b1f-a80b-34d10425d51c";
    window.history.replaceState(
      null,
      "",
      `/organizations/${primaryOrganization.id}/events/${eventId}/costs`,
    );
    mockAnonymousDevelopmentSession();
    render(<App />);

    await user.click(
      await screen.findByRole("button", {
        name: "Přihlásit se jako Alice Member",
      }),
    );
    await screen.findByRole("region", { name: "costs-route" });
    await user.click(screen.getByRole("button", { name: "Back to planner" }));
    expect(window.location.pathname).toBe(
      `/organizations/${primaryOrganization.id}/events/${eventId}/planner`,
    );
    window.history.pushState(
      null,
      "",
      `/organizations/${primaryOrganization.id}/events/${eventId}/costs`,
    );
    fireEvent(window, new PopStateEvent("popstate"));
    await screen.findByRole("region", { name: "costs-route" });
    await user.click(screen.getByRole("button", { name: "Receipts" }));
    expect(window.location.pathname).toBe(
      `/organizations/${primaryOrganization.id}/events/${eventId}/receipts`,
    );
  });

  it("returns to authentication when current organization access is revoked", async () => {
    const user = userEvent.setup();
    mockAnonymousDevelopmentSession({ organizationUnauthorized: true });
    render(<App />);

    await user.click(
      await screen.findByRole("button", {
        name: "Přihlásit se jako Alice Member",
      }),
    );
    expect(
      await screen.findByRole("heading", { name: "Vývojové přihlášení" }),
    ).toBeInTheDocument();
  });

  it("posts a Google ID token only to the Google session endpoint", async () => {
    let completeGoogleSignIn: ((credential: string) => void) | undefined;
    const renderButton = vi.fn((element: HTMLElement) => {
      const button = document.createElement("button");
      button.textContent = "Continue with Google";
      button.type = "button";
      button.addEventListener("click", () =>
        completeGoogleSignIn?.("google-id-token"),
      );
      element.append(button);
    });
    window.COOKOPS_RUNTIME_CONFIG = {
      authentication: {
        provider: "google",
        googleClientId: "cookops-test.apps.googleusercontent.com",
      },
    };
    window.google = {
      accounts: {
        id: {
          initialize: ({ callback }) => {
            completeGoogleSignIn = (credential) => callback({ credential });
          },
          renderButton,
        },
      },
    };
    const user = userEvent.setup();
    const fetchMock = mockAnonymousGoogleSession();
    render(<App />);

    await user.click(
      await screen.findByRole("button", { name: "Continue with Google" }),
    );

    expect(await screen.findByText("Alice Member")).toBeInTheDocument();
    expect(renderButton).toHaveBeenCalledOnce();
    expect(fetchMock).toHaveBeenCalledWith(
      "/auth/google/session",
      expect.objectContaining({
        method: "POST",
        credentials: "same-origin",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ id_token: "google-id-token" }),
      }),
    );
    expect(fetchMock.mock.calls.map(([path]) => path)).not.toContain(
      "/auth/dummy/identities",
    );
  });

  it("remounts Google Identity Services after a failed token presentation", async () => {
    let callback: ((response: { credential: string }) => void) | undefined;
    const renderButton = vi.fn((element: HTMLElement) => {
      const button = document.createElement("button");
      button.textContent = "Continue with Google";
      button.type = "button";
      button.addEventListener("click", () =>
        callback?.({ credential: "google-id-token" }),
      );
      element.append(button);
    });
    window.COOKOPS_RUNTIME_CONFIG = {
      authentication: {
        provider: "google",
        googleClientId: "cookops-test.apps.googleusercontent.com",
      },
    };
    window.google = {
      accounts: {
        id: {
          initialize: ({ callback: next }) => (callback = next),
          renderButton,
        },
      },
    };
    let tokenAttempts = 0;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const path = String(input);
        if (path === "/auth/session") {
          return tokenAttempts > 1
            ? response(alice)
            : response({ detail: "not authenticated" }, 401);
        }
        if (path === "/auth/google/session" && init?.method === "POST") {
          tokenAttempts += 1;
          return tokenAttempts === 1
            ? response({ detail: "denied" }, 403)
            : response(null, 204);
        }
        throw new Error(`Unexpected request: ${path}`);
      }),
    );
    const user = userEvent.setup();
    render(<App />);

    await user.click(
      await screen.findByRole("button", { name: "Continue with Google" }),
    );
    expect(
      await screen.findByText("Přihlášení se nepodařilo. Zkuste to znovu."),
    ).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Zkusit znovu" }));
    await user.click(
      await screen.findByRole("button", { name: "Continue with Google" }),
    );

    expect(await screen.findByText("Alice Member")).toBeInTheDocument();
    expect(renderButton).toHaveBeenCalledTimes(2);
  });

  it("keeps the authenticated shell and reports a distinct logout failure", async () => {
    const user = userEvent.setup();
    mockAnonymousDevelopmentSession({ logoutFails: true });
    render(<App />);
    await user.click(
      await screen.findByRole("button", {
        name: "Přihlásit se jako Alice Member",
      }),
    );

    await user.click(screen.getByRole("button", { name: "Odhlásit se" }));
    expect(
      await screen.findByText("Odhlášení se nepodařilo. Zkuste to znovu."),
    ).toBeInTheDocument();
    expect(screen.getByText("Alice Member")).toBeInTheDocument();
  });

  it("switches authenticated controls to English and logs out", async () => {
    const user = userEvent.setup();
    mockAnonymousDevelopmentSession();
    render(<App />);
    await user.click(
      await screen.findByRole("button", {
        name: "Přihlásit se jako Alice Member",
      }),
    );
    await user.selectOptions(
      screen.getByRole("combobox", { name: "Jazyk" }),
      "en",
    );

    expect(screen.getByRole("button", { name: "Log out" })).toBeInTheDocument();
    expect(
      screen.getByRole("navigation", { name: "Organization navigation" }),
    ).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Log out" }));

    await waitFor(() => {
      expect(
        screen.getByRole("heading", { name: "Development sign-in" }),
      ).toBeInTheDocument();
    });
  });
});
