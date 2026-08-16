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
    const fetchMock = vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
      if (init?.method === "POST") {
        role = "organization_admin";
        return new Response(JSON.stringify({}), { status: 200 });
      }
      return new Response(JSON.stringify(memberships(role)), { status: 200 });
    });
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
      await screen.findByRole("button", { name: "Odebrat administrátorskou roli" }),
    ).toBeVisible();
    expect(fetchMock).toHaveBeenCalledTimes(3);
  });

  it("does not render role controls for an ordinary context", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => new Response(JSON.stringify(memberships("member")), { status: 200 })),
    );
    render(
      <OrganizationMemberships
        onUnauthenticated={vi.fn()}
        organizationId={organizationId}
        systemAdmin={false}
        userId="a6a58bd6-214e-49af-8fae-e5f974bf8e08"
      />,
    );
    await waitFor(() => expect(screen.getByText("cook@example.test")).toBeVisible());
    expect(screen.queryByRole("button", { name: "Povýšit na administrátora organizace" })).toBeNull();
  });
});
