import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { EventOverview } from "./events-overview";
import i18n, { defaultLocale } from "./i18n";

const organizationId = "5ce17d2f-8365-4b1f-a80b-34d10425d51c";
const firstEvent = {
  id: "3d8b2b21-c378-4574-9e46-9338c81305ef",
  organization_id: organizationId,
  name: "Letní vaření",
  start_date: "2026-08-10",
  end_date: "2026-08-12",
  base_expected_attendance: 24,
  budget_amount: "1200.50",
  currency: "CZK",
  lifecycle: "active",
  archived_at: null,
};

function deferredResponse() {
  let resolve: (value: Response) => void = () => undefined;
  const promise = new Promise<Response>((next) => {
    resolve = next;
  });
  return { promise, resolve };
}

function response(body: object, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

describe("EventOverview", () => {
  beforeEach(async () => {
    await i18n.changeLanguage(defaultLocale);
  });

  afterEach(() => vi.unstubAllGlobals());

  it("renders the Czech event overview and keyboard-loads a following page", async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      return url.includes("cursor=next-page")
        ? response({
            events: [
              {
                ...firstEvent,
                id: "ff47ec98-a6c0-4873-bf73-929e55ef0035",
                name: "Archiv",
              },
            ],
            next_cursor: null,
          })
        : response({ events: [firstEvent], next_cursor: "next-page" });
    });
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup();
    render(
      <EventOverview
        onUnauthenticated={vi.fn()}
        organizationId={organizationId}
      />,
    );

    expect(
      await screen.findByRole("heading", { name: "Letní vaření" }),
    ).toBeInTheDocument();
    expect(screen.getByText("Aktivní")).toBeInTheDocument();
    expect(
      screen.getByText(
        "Tento přehled je pouze pro čtení a načítá se online; nejde o offline projekci.",
      ),
    ).toBeInTheDocument();
    expect(screen.getByText("Očekávaná účast")).toBeInTheDocument();
    await user.tab();
    await user.keyboard("{Enter}");
    expect(
      await screen.findByRole("heading", { name: "Archiv" }),
    ).toBeInTheDocument();
    expect(fetchMock.mock.calls[0]?.[0]).toContain(
      `/api/v1/organizations/${organizationId}/events?page_size=25`,
    );
  });

  it("has distinct empty and recoverable error states", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(response({ events: [], next_cursor: null }))
      .mockResolvedValueOnce(response({ detail: "temporary" }, 503))
      .mockResolvedValueOnce(response({ events: [], next_cursor: null }));
    vi.stubGlobal("fetch", fetchMock);
    const { rerender } = render(
      <EventOverview
        onUnauthenticated={vi.fn()}
        organizationId={organizationId}
      />,
    );
    expect(
      await screen.findByText("V této organizaci zatím nejsou žádné akce."),
    ).toBeInTheDocument();

    rerender(
      <EventOverview
        key="error"
        onUnauthenticated={vi.fn()}
        organizationId={organizationId}
      />,
    );
    expect(
      await screen.findByRole("alert", {
        name: "",
      }),
    ).toHaveTextContent("Akce se nepodařilo načíst. Zkuste to znovu.");
    await userEvent
      .setup()
      .click(screen.getByRole("button", { name: "Zkusit znovu" }));
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(3));
  });

  it("returns to authentication rather than showing an authorization error", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(response({}, 401)));
    const onUnauthenticated = vi.fn();
    render(
      <EventOverview
        onUnauthenticated={onUnauthenticated}
        organizationId={organizationId}
      />,
    );

    await waitFor(() => expect(onUnauthenticated).toHaveBeenCalledOnce());
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("rejects an event returned from another organization", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        response({
          events: [
            {
              ...firstEvent,
              organization_id: "8d43bc6e-5a35-4f9f-96cb-7d9fa7efbc53",
            },
          ],
          next_cursor: null,
        }),
      ),
    );
    render(
      <EventOverview
        onUnauthenticated={vi.fn()}
        organizationId={organizationId}
      />,
    );

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Akce se nepodařilo načíst. Zkuste to znovu.",
    );
    expect(
      screen.queryByRole("heading", { name: "Letní vaření" }),
    ).not.toBeInTheDocument();
  });

  it("does not let an older next page overwrite a changed organization", async () => {
    const nextPage = deferredResponse();
    const otherOrganizationId = "8d43bc6e-5a35-4f9f-96cb-7d9fa7efbc53";
    vi.stubGlobal(
      "fetch",
      vi.fn((input: RequestInfo | URL) => {
        const url = String(input);
        if (url.includes(otherOrganizationId)) {
          return Promise.resolve(
            response({
              events: [
                {
                  ...firstEvent,
                  id: "f6ec31a3-f4c5-4a2b-a0ac-b375d606fba1",
                  organization_id: otherOrganizationId,
                  name: "Jiná akce",
                },
              ],
              next_cursor: null,
            }),
          );
        }
        if (url.includes("cursor=next-page")) return nextPage.promise;
        return Promise.resolve(
          response({ events: [firstEvent], next_cursor: "next-page" }),
        );
      }),
    );
    const user = userEvent.setup();
    const { rerender } = render(
      <EventOverview
        onUnauthenticated={vi.fn()}
        organizationId={organizationId}
      />,
    );
    await screen.findByRole("heading", { name: "Letní vaření" });
    await user.click(screen.getByRole("button", { name: "Načíst další akce" }));
    rerender(
      <EventOverview
        onUnauthenticated={vi.fn()}
        organizationId={otherOrganizationId}
      />,
    );
    expect(
      await screen.findByRole("heading", { name: "Jiná akce" }),
    ).toBeInTheDocument();
    nextPage.resolve(
      response({
        events: [
          {
            ...firstEvent,
            id: "ff47ec98-a6c0-4873-bf73-929e55ef0035",
            name: "Archiv",
          },
        ],
        next_cursor: null,
      }),
    );
    await waitFor(() =>
      expect(
        screen.queryByRole("heading", { name: "Archiv" }),
      ).not.toBeInTheDocument(),
    );
  });

  it("invalidates an in-flight next page before a newer request", async () => {
    const nextPage = deferredResponse();
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(
        response({ events: [firstEvent], next_cursor: "next-page" }),
      )
      .mockReturnValueOnce(nextPage.promise)
      .mockResolvedValueOnce(
        response({ events: [firstEvent], next_cursor: "next-page" }),
      );
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup();
    const { rerender } = render(
      <EventOverview
        onUnauthenticated={vi.fn()}
        organizationId={organizationId}
      />,
    );
    await screen.findByRole("heading", { name: "Letní vaření" });
    await user.click(screen.getByRole("button", { name: "Načíst další akce" }));
    rerender(
      <EventOverview
        onUnauthenticated={vi.fn()}
        organizationId={organizationId}
      />,
    );
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(3));
    nextPage.resolve(
      response({
        events: [
          {
            ...firstEvent,
            id: "ff47ec98-a6c0-4873-bf73-929e55ef0035",
            name: "Stará stránka",
          },
        ],
        next_cursor: null,
      }),
    );
    expect(
      await screen.findByRole("heading", { name: "Letní vaření" }),
    ).toBeInTheDocument();
    await waitFor(() =>
      expect(
        screen.queryByRole("heading", { name: "Stará stránka" }),
      ).not.toBeInTheDocument(),
    );
  });

  it("preserves a precise Decimal budget amount", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        response({
          events: [
            {
              ...firstEvent,
              budget_amount: "9007199254740993.01",
              currency: "USD",
            },
          ],
          next_cursor: null,
        }),
      ),
    );
    render(
      <EventOverview
        onUnauthenticated={vi.fn()}
        organizationId={organizationId}
      />,
    );

    expect(
      await screen.findByText("9007199254740993.01 USD"),
    ).toBeInTheDocument();
  });
});
