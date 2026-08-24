import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import i18n, { defaultLocale } from "./i18n";
import { OrganizationMemberships } from "./organization-membership";

const organizationId = "8c4c9065-0fb3-490f-8c31-bef5103c1b1b";
const membershipId = "5ce17d2f-8365-4b1f-a80b-34d10425d51c";

function memberships(role: "member" | "organization_admin") {
  return {
    memberships: [
      {
        id: membershipId,
        invited_email: "cook@example.test",
        role,
        state: "active",
      },
    ],
  };
}

describe("OrganizationMemberships role controls", () => {
  beforeEach(async () => {
    await i18n.changeLanguage(defaultLocale);
  });

  afterEach(() => vi.restoreAllMocks());

  it("lets a system administrator promote and then shows the reloaded role", async () => {
    let role: "member" | "organization_admin" = "member";
    const fetchMock = vi.fn(
      async (_input: RequestInfo | URL, init?: RequestInit) => {
        if (init?.method === "POST") {
          role = "organization_admin";
          return new Response(JSON.stringify({}), { status: 200 });
        }
        return new Response(JSON.stringify(memberships(role)), { status: 200 });
      },
    );
    vi.stubGlobal("fetch", fetchMock);
    render(
      <OrganizationMemberships
        onUnauthenticated={vi.fn()}
        organizationId={organizationId}
        systemAdmin
        userId="a6a58bd6-214e-49af-8fae-e5f974bf8e08"
      />,
    );

    const promote = await screen.findByRole("button", {
      name: "Povýšit na administrátora organizace",
    });
    await userEvent.click(promote);
    expect(
      await screen.findByRole("button", {
        name: "Odebrat administrátorskou roli",
      }),
    ).toBeVisible();
    expect(fetchMock).toHaveBeenCalledTimes(3);
  });

  it("does not show success when the membership reload fails", async () => {
    const fetchMock = vi.fn(
      async (_input: RequestInfo | URL, init?: RequestInit) => {
        if (init?.method === "POST")
          return new Response(JSON.stringify({}), { status: 200 });
        if (fetchMock.mock.calls.length === 3)
          return new Response("", { status: 500 });
        return new Response(JSON.stringify(memberships("member")), {
          status: 200,
        });
      },
    );
    vi.stubGlobal("fetch", fetchMock);
    render(
      <OrganizationMemberships
        onUnauthenticated={vi.fn()}
        organizationId={organizationId}
        systemAdmin
        userId="a6a58bd6-214e-49af-8fae-e5f974bf8e08"
      />,
    );

    await userEvent.click(
      await screen.findByRole("button", {
        name: "Povýšit na administrátora organizace",
      }),
    );
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Změnu členství se nepodařilo provést.",
    );
    expect(
      screen.queryByText("Role administrátora byla aktualizována."),
    ).toBeNull();
  });

  it("does not render role controls for an ordinary context", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(
        async () =>
          new Response(JSON.stringify(memberships("member")), { status: 200 }),
      ),
    );
    render(
      <OrganizationMemberships
        onUnauthenticated={vi.fn()}
        organizationId={organizationId}
        systemAdmin={false}
        userId="a6a58bd6-214e-49af-8fae-e5f974bf8e08"
      />,
    );
    await waitFor(() =>
      expect(screen.getByText("cook@example.test")).toBeVisible(),
    );
    expect(
      screen.queryByRole("button", {
        name: "Povýšit na administrátora organizace",
      }),
    ).toBeNull();
  });

  it("keeps invitation and removal controls out of system administrator management", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(
        async () =>
          new Response(JSON.stringify(memberships("member")), { status: 200 }),
      ),
    );
    render(
      <OrganizationMemberships
        onUnauthenticated={vi.fn()}
        organizationId={organizationId}
        systemAdmin
        systemAdminManagement
        userId="a6a58bd6-214e-49af-8fae-e5f974bf8e08"
      />,
    );
    expect(
      await screen.findByRole("button", {
        name: "Povýšit na administrátora organizace",
      }),
    ).toBeVisible();
    expect(screen.queryByRole("button", { name: "Pozvat člena" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Odebrat člena" })).toBeNull();
    expect(
      screen.queryByText("Tato organizace zatím nemá administrátora."),
    ).toBeVisible();
  });

  it("shows both empty membership statuses in system administrator management", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(
        async () =>
          new Response(JSON.stringify({ memberships: [] }), { status: 200 }),
      ),
    );
    render(
      <OrganizationMemberships
        onUnauthenticated={vi.fn()}
        organizationId={organizationId}
        systemAdmin
        systemAdminManagement
        userId="a6a58bd6-214e-49af-8fae-e5f974bf8e08"
      />,
    );

    expect(
      await screen.findByText("Tato organizace zatím nemá žádné členy."),
    ).toBeVisible();
    expect(
      screen.getByText("Tato organizace zatím nemá administrátora."),
    ).toBeVisible();
  });

  it("confirms administrator revocation", async () => {
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);
    const fetchMock = vi.fn(
      async () =>
        new Response(JSON.stringify(memberships("organization_admin")), {
          status: 200,
        }),
    );
    vi.stubGlobal("fetch", fetchMock);
    render(
      <OrganizationMemberships
        onUnauthenticated={vi.fn()}
        organizationId={organizationId}
        systemAdmin
        systemAdminManagement
        userId="a6a58bd6-214e-49af-8fae-e5f974bf8e08"
      />,
    );
    await userEvent.click(
      await screen.findByRole("button", {
        name: "Odebrat administrátorskou roli",
      }),
    );
    expect(confirm).toHaveBeenCalled();
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });
});
