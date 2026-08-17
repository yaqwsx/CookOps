import { fireEvent, render, screen, waitFor } from "@testing-library/react";
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
    getOrganizations.mockResolvedValue([{
      id: "5ce17d2f-8365-4b1f-a80b-34d10425d51c",
      name: "Kitchen",
      description: "Old",
      default_currency: "CZK",
      retired_at: null,
      retired_by_user_id: null,
    }]);
    editOrganization.mockResolvedValue({});
  });

  it("submits an inline edit and refreshes the administration list", async () => {
    render(<SystemOrganizationCreate userId="user-id" onCreated={vi.fn()} />);
    expect(await screen.findByRole("button", { name: "Edit organization Kitchen" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Edit organization Kitchen" }));
    fireEvent.change(screen.getByRole("textbox", { name: "Edit organization Kitchen" }), { target: { value: "Updated kitchen" } });
    fireEvent.click(screen.getByRole("button", { name: "Save changes" }));
    await waitFor(() => expect(editOrganization).toHaveBeenCalledWith("user-id", "5ce17d2f-8365-4b1f-a80b-34d10425d51c", expect.objectContaining({ name: "Updated kitchen" })));
    expect(getOrganizations).toHaveBeenCalledTimes(2);
  });
});
