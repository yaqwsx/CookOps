import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import i18n from "./i18n";
import { SystemOrganizationCreate } from "./system-organization-create";

const { getOrganizations, editOrganization } = vi.hoisted(() => ({
  getOrganizations: vi.fn(),
  editOrganization: vi.fn(),
}));

vi.mock("./api/system-organizations", () => ({
  changeSystemOrganizationLifecycle: vi.fn(),
  createSystemOrganization: vi.fn(),
  editSystemOrganization: editOrganization,
  getSystemOrganizations: getOrganizations,
}));

describe("SystemOrganizationCreate editing", () => {
  beforeEach(() => {
    void i18n.changeLanguage("en");
    getOrganizations.mockResolvedValue([
      {
        id: "5ce17d2f-8365-4b1f-a80b-34d10425d51c",
        name: "Kitchen",
        description: "Old",
        default_currency: "CZK",
        retired_at: null,
        retired_by_user_id: null,
      },
    ]);
    editOrganization.mockResolvedValue({});
  });

  it("submits an inline edit and refreshes the administration list", async () => {
    render(<SystemOrganizationCreate userId="user-id" onCreated={vi.fn()} />);
    expect(
      await screen.findByRole("button", { name: "Edit organization Kitchen" }),
    ).toBeInTheDocument();
    fireEvent.click(
      screen.getByRole("button", { name: "Edit organization Kitchen" }),
    );
    fireEvent.change(
      screen.getByRole("textbox", { name: "Edit organization Kitchen" }),
      { target: { value: "Updated kitchen" } },
    );
    fireEvent.click(screen.getByRole("button", { name: "Save changes" }));
    await waitFor(() =>
      expect(editOrganization).toHaveBeenCalledWith(
        "user-id",
        "5ce17d2f-8365-4b1f-a80b-34d10425d51c",
        expect.objectContaining({ name: "Updated kitchen" }),
      ),
    );
    expect(getOrganizations).toHaveBeenCalledTimes(2);
  });

  it("loads membership management for the selected active organization only", async () => {
    const membershipFetch = vi.fn(
      async () =>
        new Response(JSON.stringify({ memberships: [] }), { status: 200 }),
    );
    vi.stubGlobal("fetch", membershipFetch);
    getOrganizations.mockResolvedValueOnce([
      {
        id: "5ce17d2f-8365-4b1f-a80b-34d10425d51c",
        name: "Kitchen",
        description: null,
        default_currency: "CZK",
        retired_at: null,
        retired_by_user_id: null,
      },
      {
        id: "7ce17d2f-8365-4b1f-a80b-34d10425d51c",
        name: "Retired kitchen",
        description: null,
        default_currency: "CZK",
        retired_at: "2026-01-01T00:00:00Z",
        retired_by_user_id: "user-id",
      },
    ]);
    render(<SystemOrganizationCreate userId="user-id" onCreated={vi.fn()} />);

    expect(
      await screen.findByRole("button", {
        name: "Manage administrators for Kitchen",
      }),
    ).toHaveAttribute("aria-pressed", "false");
    expect(
      screen.queryByRole("button", {
        name: "Manage administrators for Retired kitchen",
      }),
    ).toBeNull();
    expect(membershipFetch).not.toHaveBeenCalled();

    await userEvent.click(
      screen.getByRole("button", { name: "Manage administrators for Kitchen" }),
    );
    expect(
      await screen.findByText("Managing organization: Kitchen"),
    ).toBeVisible();
    expect(
      screen.getByRole("button", { name: "Manage administrators for Kitchen" }),
    ).toHaveAttribute("aria-pressed", "true");
    await waitFor(() =>
      expect(membershipFetch).toHaveBeenCalledWith(
        expect.stringContaining("/members"),
        expect.anything(),
      ),
    );
  });
});
