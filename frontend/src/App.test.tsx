import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { RouterProvider } from "@tanstack/react-router";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import i18n, { defaultLocale } from "./i18n";
import { createAppRouter } from "./router";

vi.mock("./event-costs-page", () => ({
  EventCostsPage: ({
    onBack,
    onOpenReceipts,
  }: {
    onBack: () => void;
    onOpenReceipts: () => void;
  }) => (
    <section aria-label="costs-route">
      <button onClick={onBack} type="button">
        Back to planner
      </button>
      <button onClick={onOpenReceipts} type="button">
        Receipts
      </button>
    </section>
  ),
}));

vi.mock("./event-settings-page", () => ({
  EventSettingsPage: ({
    onOpenCosts,
    onOpenPlanner,
  }: {
    onOpenCosts: () => void;
    onOpenPlanner: () => void;
  }) => (
    <section aria-label="event-settings-route">
      <button onClick={onOpenPlanner} type="button">
        Back to planner
      </button>
      <button onClick={onOpenCosts} type="button">
        Costs
      </button>
    </section>
  ),
}));

vi.mock("./event-shopping", () => ({
  EventShopping: () => <section aria-label="shopping-route" />,
}));

vi.mock("./recipe-catalog-view", () => ({
  RecipeCatalog: ({
    editRecipeId,
    onBackToCatalog,
    onDirtyChange,
    selectedRecipeId,
  }: {
    editRecipeId?: string;
    onBackToCatalog?: () => void;
    onDirtyChange?: (dirty: boolean) => void;
    selectedRecipeId?: string;
  }) => (
    <section aria-label="recipe-route">
      <span>{selectedRecipeId ?? "catalog"}</span>
      {editRecipeId ? (
        <button onClick={() => onDirtyChange?.(true)} type="button">
          Make dirty
        </button>
      ) : null}
      {onBackToCatalog ? (
        <button onClick={onBackToCatalog} type="button">
          Back to catalog
        </button>
      ) : null}
    </section>
  ),
}));

vi.mock("./ingredient-catalog-view", () => ({
  IngredientCatalog: ({
    onBackToCatalog,
    onDirtyChange,
    selectedIngredientId,
  }: {
    onBackToCatalog?: () => void;
    onDirtyChange?: (dirty: boolean) => void;
    selectedIngredientId?: string;
  }) => (
    <section aria-label="ingredient-route">
      <span>{selectedIngredientId ?? "catalog"}</span>
      {onBackToCatalog ? (
        <button onClick={onBackToCatalog} type="button">
          Back to ingredient catalog
        </button>
      ) : null}
      {selectedIngredientId && selectedIngredientId !== "__invalid__" ? (
        <button onClick={() => onDirtyChange?.(true)} type="button">
          Make ingredient dirty
        </button>
      ) : null}
    </section>
  ),
}));

const alice = {
  id: "a6a58bd6-214e-49af-8fae-e5f974bf8e08",
  display_name: "Alice Member",
  verified_email: "alice@example.test",
  preferred_locale: "cs" as const,
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

function requestPath(input: RequestInfo | URL): string {
  const url = new URL(
    input instanceof Request ? input.url : input,
    window.location.origin,
  );
  return url.pathname + url.search;
}

function mockAnonymousDevelopmentSession({
  logoutFails = false,
  localePersistFails = false,
  preferredLocale = "cs" as "cs" | "en",
  initiallySignedIn = false,
  organizationList = organizations,
  organizationUnauthorized = false,
} = {}) {
  window.COOKOPS_RUNTIME_CONFIG = { authentication: { provider: "dummy" } };
  let signedIn = initiallySignedIn;
  let accessRevoked = false;
  const fetchMock = vi.fn(
    async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = requestPath(input);
      if (path === "/auth/session") {
        return signedIn && !accessRevoked
          ? response({ ...alice, preferred_locale: preferredLocale })
          : response({ detail: "not authenticated" }, 401);
      }
      if (path === "/api/v1/organizations") {
        if (!organizationUnauthorized) return response(organizationList);
        accessRevoked = true;
        return response({ detail: "not authenticated" }, 401);
      }
      if (path === "/api/v1/system/organizations/access")
        return response(null, 403);
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
      if (path === "/auth/session/locale" && init?.method === "PATCH") {
        if (localePersistFails) return response({ detail: "unavailable" }, 503);
        const body = JSON.parse(init.body as string) as {
          preferred_locale: "cs" | "en";
        };
        return response({ ...alice, preferred_locale: body.preferred_locale });
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
      const path = requestPath(input);
      if (path === "/auth/session") {
        return signedIn
          ? response(alice)
          : response({ detail: "not authenticated" }, 401);
      }
      if (path === "/api/v1/organizations") return response(organizations);
      if (path === "/api/v1/system/organizations/access")
        return response(null, 403);
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
    const fetchMock = mockAnonymousDevelopmentSession();
    render(<RouterProvider router={createAppRouter()} />);

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
    expect(
      fetchMock.mock.calls.some(([path]) => path === "/auth/session/locale"),
    ).toBe(false);
  });

  it("renders a localized recoverable error when the session check cannot start", async () => {
    window.COOKOPS_RUNTIME_CONFIG = { authentication: { provider: "dummy" } };
    let failed = false;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        if (requestPath(input) === "/auth/session" && !failed) {
          failed = true;
          throw new Error("network unavailable");
        }
        if (requestPath(input) === "/auth/session") {
          return response({ detail: "not authenticated" }, 401);
        }
        if (requestPath(input) === "/auth/dummy/identities") {
          return response({ identities: [] });
        }
        throw new Error(`Unexpected request: ${requestPath(input)}`);
      }),
    );
    const user = userEvent.setup();
    render(<RouterProvider router={createAppRouter()} />);

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
      if (requestPath(input) === "/auth/session") {
        return response({ detail: "not authenticated" }, 401);
      }
      throw new Error(`Unexpected request: ${requestPath(input)}`);
    });
    vi.stubGlobal("fetch", fetchMock);
    render(<RouterProvider router={createAppRouter()} />);

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
        if (requestPath(input) === "/auth/session") {
          return response({ detail: "not authenticated" }, 401);
        }
        if (requestPath(input) === "/auth/dummy/identities") {
          listAttempts += 1;
          return listAttempts === 1
            ? response({ detail: "unavailable" }, 503)
            : response({
                identities: [
                  { subject: "dummy-alice", display_name: "Alice Member" },
                ],
              });
        }
        throw new Error(`Unexpected request: ${requestPath(input)}`);
      }),
    );
    const user = userEvent.setup();
    render(<RouterProvider router={createAppRouter()} />);

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
    render(<RouterProvider router={createAppRouter()} />);

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

  it("applies the server locale after signing in from the Czech login UI", async () => {
    const user = userEvent.setup();
    mockAnonymousDevelopmentSession({ preferredLocale: "en" });
    render(<RouterProvider router={createAppRouter()} />);

    expect(
      await screen.findByRole("heading", { name: "Vývojové přihlášení" }),
    ).toBeInTheDocument();
    await user.click(
      screen.getByRole("button", { name: "Přihlásit se jako Alice Member" }),
    );

    expect(await screen.findByText("Alice Member")).toBeInTheDocument();
    expect(
      await screen.findByRole("button", { name: "Log out" }),
    ).toBeInTheDocument();
    expect(screen.getByRole("combobox", { name: "Language" })).toHaveValue(
      "en",
    );
    await waitFor(() => {
      expect(window.location.pathname).toBe(
        `/organizations/${primaryOrganization.id}/events`,
      );
    });
  });

  it("persists authenticated locale changes and shows failures", async () => {
    const user = userEvent.setup();
    const fetchMock = mockAnonymousDevelopmentSession({
      localePersistFails: true,
    });
    render(<RouterProvider router={createAppRouter()} />);
    await user.click(
      await screen.findByRole("button", {
        name: "Přihlásit se jako Alice Member",
      }),
    );
    const picker = await screen.findByRole("combobox", { name: "Jazyk" });
    await user.selectOptions(picker, "en");
    expect(picker).toHaveValue("en");
    const localeRequest = fetchMock.mock.calls.find(
      ([path]) => path === "/auth/session/locale",
    );
    expect(fetchMock).toHaveBeenCalledWith(
      "/auth/session/locale",
      expect.objectContaining({
        method: "PATCH",
        body: JSON.stringify({ preferred_locale: "en" }),
        headers: expect.objectContaining({
          "content-type": "application/json",
        }),
      }),
    );
    expect(
      Object.keys(
        (localeRequest?.[1]?.headers ?? {}) as Record<string, unknown>,
      ).some((key) => key.toLowerCase() === "origin"),
    ).toBe(false);
    expect(
      await screen.findByText(
        "The language could not be saved. Please try again.",
      ),
    ).toBeInTheDocument();
  });

  it("applies the persisted locale on session boot and does not persist login selection", async () => {
    const fetchMock = mockAnonymousDevelopmentSession({
      preferredLocale: "en",
      initiallySignedIn: true,
    });
    render(<RouterProvider router={createAppRouter()} />);
    expect(
      await screen.findByRole("combobox", { name: "Language" }),
    ).toHaveValue("en");
    expect(
      fetchMock.mock.calls.some(([path]) => path === "/auth/session/locale"),
    ).toBe(false);
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
    render(<RouterProvider router={createAppRouter()} />);

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
    render(<RouterProvider router={createAppRouter()} />);

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

  it("routes direct recipe detail and edit URLs without opening events", async () => {
    const user = userEvent.setup();
    const recipeId = "6ce17d2f-8365-4b1f-a80b-34d10425d51c";
    window.history.replaceState(
      null,
      "",
      `/organizations/${primaryOrganization.id}/recipes/${recipeId}/edit`,
    );
    mockAnonymousDevelopmentSession();
    render(<RouterProvider router={createAppRouter()} />);
    await user.click(
      await screen.findByRole("button", {
        name: "Přihlásit se jako Alice Member",
      }),
    );
    expect(
      await screen.findByRole("region", { name: "recipe-route" }),
    ).toHaveTextContent(recipeId);
    expect(screen.getByRole("button", { name: "Make dirty" })).toBeVisible();
    expect(
      screen.queryByRole("heading", { name: "Přehled akcí" }),
    ).not.toBeInTheDocument();
  });

  it("rejects malformed recipe and organization ids at the route boundary", async () => {
    const user = userEvent.setup();
    window.history.replaceState(
      null,
      "",
      `/organizations/${primaryOrganization.id}/recipes/not-a-uuid`,
    );
    mockAnonymousDevelopmentSession();
    render(<RouterProvider router={createAppRouter()} />);
    await user.click(
      await screen.findByRole("button", {
        name: "Přihlásit se jako Alice Member",
      }),
    );
    expect(
      await screen.findByText("Požadovaná stránka nebyla nalezena."),
    ).toBeVisible();
    window.history.replaceState(null, "", "/organizations/not-an-id/recipes");
    fireEvent(window, new PopStateEvent("popstate"));
    expect(
      await screen.findByText("Požadovaná stránka nebyla nalezena."),
    ).toBeVisible();
  });

  it("routes direct ingredient detail and rejects malformed ids", async () => {
    const user = userEvent.setup();
    const ingredientId = "7ce17d2f-8365-4b1f-a80b-34d10425d51c";
    window.history.replaceState(
      null,
      "",
      `/organizations/${primaryOrganization.id}/ingredients/${ingredientId}`,
    );
    mockAnonymousDevelopmentSession();
    render(<RouterProvider router={createAppRouter()} />);
    await user.click(
      await screen.findByRole("button", {
        name: "Přihlásit se jako Alice Member",
      }),
    );
    expect(
      await screen.findByRole("region", { name: "ingredient-route" }),
    ).toHaveTextContent(ingredientId);
    await user.click(
      screen.getByRole("button", { name: "Back to ingredient catalog" }),
    );
    expect(window.location.pathname).toBe(
      `/organizations/${primaryOrganization.id}/ingredients`,
    );

    window.history.replaceState(
      null,
      "",
      `/organizations/${primaryOrganization.id}/ingredients/not-a-uuid`,
    );
    fireEvent(window, new PopStateEvent("popstate"));
    expect(
      await screen.findByText("Požadovaná stránka nebyla nalezena."),
    ).toBeVisible();
  });

  it("shows a generic access state for a valid but unavailable organization", async () => {
    const user = userEvent.setup();
    window.history.replaceState(
      null,
      "",
      "/organizations/9ce17d2f-8365-4b1f-a80b-34d10425d51c/events",
    );
    mockAnonymousDevelopmentSession();
    render(<RouterProvider router={createAppRouter()} />);
    await user.click(
      await screen.findByRole("button", {
        name: "Přihlásit se jako Alice Member",
      }),
    );
    expect(
      await screen.findByText("Tato organizace není v tomto účtu dostupná."),
    ).toBeVisible();
    expect(
      screen.queryByRole("region", { name: "events-route" }),
    ).not.toBeInTheDocument();
  });

  it("guards ingredient detail navigation when an editor is dirty", async () => {
    const user = userEvent.setup();
    const ingredientId = "7ce17d2f-8365-4b1f-a80b-34d10425d51c";
    window.history.replaceState(
      null,
      "",
      `/organizations/${primaryOrganization.id}/ingredients/${ingredientId}`,
    );
    mockAnonymousDevelopmentSession();
    const confirmMock = vi.spyOn(window, "confirm");
    render(<RouterProvider router={createAppRouter()} />);
    await user.click(
      await screen.findByRole("button", {
        name: "Přihlásit se jako Alice Member",
      }),
    );
    await user.click(
      await screen.findByRole("button", { name: "Make ingredient dirty" }),
    );
    confirmMock.mockReturnValue(false);
    await user.click(
      screen.getByRole("button", { name: "Back to ingredient catalog" }),
    );
    expect(window.location.pathname).toContain(`/ingredients/${ingredientId}`);
    expect(confirmMock).toHaveBeenCalledWith("Zahodit neuložené změny?");
    confirmMock.mockReturnValue(true);
    await user.click(
      screen.getByRole("button", { name: "Back to ingredient catalog" }),
    );
    expect(window.location.pathname).toBe(
      `/organizations/${primaryOrganization.id}/ingredients`,
    );
    confirmMock.mockRestore();
  });

  it("requires discarding an edit before browser back leaves the route", async () => {
    const user = userEvent.setup();
    const recipeId = "6ce17d2f-8365-4b1f-a80b-34d10425d51c";
    window.history.replaceState(
      null,
      "",
      `/organizations/${primaryOrganization.id}/recipes/${recipeId}/edit`,
    );
    mockAnonymousDevelopmentSession();
    const confirmMock = vi.spyOn(window, "confirm");
    const appRouter = createAppRouter();
    render(<RouterProvider router={appRouter} />);
    await user.click(
      await screen.findByRole("button", {
        name: "Přihlásit se jako Alice Member",
      }),
    );
    await user.click(await screen.findByRole("button", { name: "Make dirty" }));
    confirmMock.mockReturnValue(false);
    void appRouter.navigate({
      to: "/organizations/$organizationId/recipes",
      params: { organizationId: primaryOrganization.id },
    });
    await waitFor(() =>
      expect(window.location.pathname).toContain(`/recipes/${recipeId}/edit`),
    );
    confirmMock.mockReturnValue(true);
    await appRouter.navigate({
      to: "/organizations/$organizationId/recipes",
      params: { organizationId: primaryOrganization.id },
    });
    await waitFor(() =>
      expect(window.location.pathname).toBe(
        `/organizations/${primaryOrganization.id}/recipes`,
      ),
    );
    confirmMock.mockRestore();
  });

  it("blocks dirty navigation between editor params on the same route", async () => {
    const user = userEvent.setup();
    const recipeA = "6ce17d2f-8365-4b1f-a80b-34d10425d51c";
    const recipeB = "7ce17d2f-8365-4b1f-a80b-34d10425d51c";
    window.history.replaceState(
      null,
      "",
      `/organizations/${primaryOrganization.id}/recipes/${recipeA}/edit`,
    );
    mockAnonymousDevelopmentSession();
    const confirmMock = vi.spyOn(window, "confirm").mockReturnValue(false);
    const appRouter = createAppRouter();
    render(<RouterProvider router={appRouter} />);
    await user.click(
      await screen.findByRole("button", {
        name: "Přihlásit se jako Alice Member",
      }),
    );
    await user.click(await screen.findByRole("button", { name: "Make dirty" }));
    void appRouter.navigate({
      to: "/organizations/$organizationId/recipes/$recipeId/edit",
      params: { organizationId: primaryOrganization.id, recipeId: recipeB },
    });
    await waitFor(() =>
      expect(window.location.pathname).toContain(`/recipes/${recipeA}/edit`),
    );
    await waitFor(() =>
      expect(confirmMock).toHaveBeenCalledWith(
        "Zahodit neuložené změny receptu?",
      ),
    );
    confirmMock.mockRestore();
  });

  it("synchronizes a confirmed real browser back from edit to catalog", async () => {
    const user = userEvent.setup();
    const recipeId = "6ce17d2f-8365-4b1f-a80b-34d10425d51c";
    window.history.replaceState(
      null,
      "",
      `/organizations/${primaryOrganization.id}/recipes`,
    );
    mockAnonymousDevelopmentSession();
    const confirmMock = vi.spyOn(window, "confirm").mockReturnValue(true);
    render(<RouterProvider router={createAppRouter()} />);
    await user.click(
      await screen.findByRole("button", {
        name: "Přihlásit se jako Alice Member",
      }),
    );
    await screen.findByText("catalog");
    window.history.pushState(
      null,
      "",
      `/organizations/${primaryOrganization.id}/recipes/${recipeId}/edit`,
    );
    fireEvent(window, new PopStateEvent("popstate"));
    await user.click(await screen.findByRole("button", { name: "Make dirty" }));
    window.history.back();
    await waitFor(() => {
      expect(window.location.pathname).toBe(
        `/organizations/${primaryOrganization.id}/recipes`,
      );
      expect(screen.getByText("catalog")).toBeVisible();
    });
    expect(confirmMock).toHaveBeenCalled();
    confirmMock.mockRestore();
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
    render(<RouterProvider router={createAppRouter()} />);

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

  it("renders a bookmarked event settings route and navigates to planner or costs", async () => {
    const user = userEvent.setup();
    const eventId = "6ce17d2f-8365-4b1f-a80b-34d10425d51c";
    window.history.replaceState(
      null,
      "",
      `/organizations/${primaryOrganization.id}/events/${eventId}/settings`,
    );
    mockAnonymousDevelopmentSession();
    render(<RouterProvider router={createAppRouter()} />);
    await user.click(
      await screen.findByRole("button", {
        name: "Přihlásit se jako Alice Member",
      }),
    );
    await screen.findByRole("region", { name: "event-settings-route" });
    await user.click(screen.getByRole("button", { name: "Back to planner" }));
    expect(window.location.pathname).toBe(
      `/organizations/${primaryOrganization.id}/events/${eventId}/planner`,
    );
    window.history.pushState(
      null,
      "",
      `/organizations/${primaryOrganization.id}/events/${eventId}/settings`,
    );
    fireEvent(window, new PopStateEvent("popstate"));
    await screen.findByRole("region", { name: "event-settings-route" });
    await user.click(screen.getByRole("button", { name: "Costs" }));
    expect(window.location.pathname).toBe(
      `/organizations/${primaryOrganization.id}/events/${eventId}/costs`,
    );
  });

  it("does not open settings for an extra route segment", async () => {
    const user = userEvent.setup();
    const eventId = "6ce17d2f-8365-4b1f-a80b-34d10425d51c";
    window.history.replaceState(
      null,
      "",
      `/organizations/${primaryOrganization.id}/events/${eventId}/settings/${eventId}`,
    );
    mockAnonymousDevelopmentSession();
    render(<RouterProvider router={createAppRouter()} />);
    await user.click(
      await screen.findByRole("button", {
        name: "Přihlásit se jako Alice Member",
      }),
    );
    await waitFor(() =>
      expect(
        screen.queryByRole("region", { name: "event-settings-route" }),
      ).not.toBeInTheDocument(),
    );
  });

  it("keeps a valid shopping-list suffix route", async () => {
    const user = userEvent.setup();
    const eventId = "6ce17d2f-8365-4b1f-a80b-34d10425d51c";
    const shoppingListId = "7ce17d2f-8365-4b1f-a80b-34d10425d51c";
    window.history.replaceState(
      null,
      "",
      `/organizations/${primaryOrganization.id}/events/${eventId}/shopping/${shoppingListId}`,
    );
    mockAnonymousDevelopmentSession();
    render(<RouterProvider router={createAppRouter()} />);
    await user.click(
      await screen.findByRole("button", {
        name: "Přihlásit se jako Alice Member",
      }),
    );
    expect(
      await screen.findByRole("region", { name: "shopping-route" }),
    ).toBeInTheDocument();
  });

  it("returns to authentication when current organization access is revoked", async () => {
    const user = userEvent.setup();
    mockAnonymousDevelopmentSession({ organizationUnauthorized: true });
    render(<RouterProvider router={createAppRouter()} />);

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
    render(<RouterProvider router={createAppRouter()} />);

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
        const path = requestPath(input);
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
    render(<RouterProvider router={createAppRouter()} />);

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
    render(<RouterProvider router={createAppRouter()} />);
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
    render(<RouterProvider router={createAppRouter()} />);
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

  it("shows the system organization route only to a system administrator and refreshes the switcher after creation", async () => {
    const user = userEvent.setup();
    window.history.replaceState(null, "", "/system/organizations");
    window.COOKOPS_RUNTIME_CONFIG = { authentication: { provider: "dummy" } };
    let signedIn = false;
    let organizationReads = 0;
    let lifecycleRetired = false;
    let lifecycleReads = 0;
    const createdId = "8c4c9065-0fb3-490f-8c31-bef5103c1b1b";
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const path = requestPath(input);
        if (path === "/auth/session") {
          return signedIn ? response({ ...alice }) : response(null, 401);
        }
        if (path === "/auth/dummy/identities") {
          return response({
            identities: [{ subject: "dummy-admin", display_name: "Admin" }],
          });
        }
        if (path === "/auth/dummy/session" && init?.method === "POST") {
          signedIn = true;
          return response(null, 204);
        }
        if (path === "/api/v1/system/organizations/access")
          return response(null, 204);
        if (path === "/api/v1/system/organizations" && !init?.method) {
          lifecycleReads += 1;
          return response([
            {
              id: createdId,
              name: "New kitchen",
              description: null,
              default_currency: "CZK",
              retired_at: lifecycleRetired ? "2026-08-16T12:00:00Z" : null,
              retired_by_user_id: lifecycleRetired ? alice.id : null,
            },
          ]);
        }
        if (path === "/api/v1/organizations") {
          organizationReads += 1;
          return response({
            organizations:
              organizationReads > 1
                ? [
                    ...organizations.organizations,
                    { id: createdId, name: "New kitchen" },
                  ]
                : organizations.organizations,
          });
        }
        if (
          path === "/api/v1/system/organizations" &&
          init?.method === "POST"
        ) {
          return response({ id: createdId, name: "New kitchen" }, 201);
        }
        if (
          path === `/api/v1/system/organizations/${createdId}/lifecycle` &&
          init?.method === "POST"
        ) {
          const lifecycleBody = JSON.parse(init.body as string);
          expect(lifecycleBody.operation).toBe(
            lifecycleRetired ? "restore" : "retire",
          );
          expect(lifecycleBody.mutation_id).toEqual(expect.any(String));
          expect(lifecycleBody.client_installation_id).toEqual(
            expect.any(String),
          );
          expect(lifecycleBody.client_wall_time).toEqual(expect.any(String));
          lifecycleRetired = !lifecycleRetired;
          return response({
            id: createdId,
            name: "New kitchen",
            description: null,
            default_currency: "CZK",
            retired_at: lifecycleRetired ? "2026-08-16T12:00:00Z" : null,
            retired_by_user_id: lifecycleRetired ? alice.id : null,
          });
        }
        throw new Error(`Unexpected request: ${path}`);
      }),
    );

    render(<RouterProvider router={createAppRouter()} />);
    await user.click(
      await screen.findByRole("button", { name: "Přihlásit se jako Admin" }),
    );
    expect(
      await screen.findByRole("heading", { name: "Nová organizace" }),
    ).toBeInTheDocument();
    await user.type(screen.getByLabelText("Název"), "New kitchen");
    await user.click(
      screen.getByRole("button", { name: "Vytvořit organizaci" }),
    );
    expect(
      await screen.findByText("Organizace byla vytvořena."),
    ).toBeInTheDocument();
    await waitFor(() =>
      expect(
        screen.getByRole("option", { name: "New kitchen" }),
      ).toBeInTheDocument(),
    );
    expect(lifecycleReads).toBeGreaterThan(0);
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(true);
    await user.click(
      screen.getByRole("button", { name: "Ukončit organizaci New kitchen" }),
    );
    expect(confirm).toHaveBeenCalledWith(
      "Opravdu chcete ukončit organizaci New kitchen?",
    );
    expect(
      await screen.findByText("New kitchen — Ukončená"),
    ).toBeInTheDocument();
    await user.click(
      screen.getByRole("button", { name: "Obnovit organizaci New kitchen" }),
    );
    expect(
      await screen.findByText("New kitchen — Aktivní"),
    ).toBeInTheDocument();
    confirm.mockRestore();
  });

  it("does not expose the system organization surface to a non-admin", async () => {
    const user = userEvent.setup();
    window.history.replaceState(null, "", "/system/organizations");
    mockAnonymousDevelopmentSession();
    render(<RouterProvider router={createAppRouter()} />);
    await user.click(
      await screen.findByRole("button", {
        name: "Přihlásit se jako Alice Member",
      }),
    );
    expect(
      await screen.findByText(
        "Tato stránka je dostupná jen systémovým administrátorům.",
      ),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("heading", { name: "Nová organizace" }),
    ).toBeNull();
  });
});
