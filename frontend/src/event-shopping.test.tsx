import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { EventShopping } from "./event-shopping";
import i18n, { defaultLocale } from "./i18n";

const {
  readEventPlanner,
  readShoppingLists,
  queueShoppingList,
  pullOrganization,
} = vi.hoisted(() => ({
  readEventPlanner: vi.fn(),
  readShoppingLists: vi.fn(),
  queueShoppingList: vi.fn(),
  pullOrganization: vi.fn(),
}));
vi.mock("./planner-projections", () => ({ readEventPlanner }));
vi.mock("./shopping-projections", () => ({
  readShoppingLists,
  readShoppingList: vi.fn(),
}));
vi.mock("./shopping-list", () => ({ queueShoppingList }));
vi.mock("./sync-bootstrap", () => ({
  pullOrganization,
  SyncRequestError: class SyncRequestError extends Error {},
}));

const ids = {
  user: "a6a58bd6-214e-49af-8fae-e5f974bf8e08",
  organization: "5ce17d2f-8365-4b1f-a80b-34d10425d51c",
  event: "3d8b2b21-c378-4574-9e46-9338c81305ef",
  scheduled: "8d8b2b21-c378-4574-9e46-9338c81305ef",
};

describe("EventShopping", () => {
  afterEach(() => vi.clearAllMocks());

  it("uses labelled source checkboxes and the existing typed outbox command", async () => {
    await i18n.changeLanguage(defaultLocale);
    readEventPlanner.mockResolvedValue({
      name: "Letní vaření",
      lifecycle: "active",
      scheduled: [
        {
          id: ids.scheduled,
          name: "Chili",
          dinerCount: 12,
          dayId: "day",
          roleId: "role",
          position: "a",
        },
      ],
    });
    readShoppingLists.mockResolvedValue([]);
    pullOrganization.mockResolvedValue(false);
    const user = userEvent.setup();
    render(
      <EventShopping
        eventId={ids.event}
        onBack={vi.fn()}
        onOpenList={vi.fn()}
        onOpenPlanner={vi.fn()}
        onUnauthenticated={vi.fn()}
        organizationId={ids.organization}
        userId={ids.user}
      />,
    );

    await screen.findByRole("heading", { name: "Nákupy" });
    await user.type(screen.getByLabelText("Název"), "Sobota");
    await user.click(screen.getByLabelText("Chili · Strávníci: 12"));
    await user.click(
      screen.getByRole("button", { name: "Vytvořit nákupní seznam" }),
    );
    expect(queueShoppingList).toHaveBeenCalledWith(ids.user, ids.organization, {
      eventId: ids.event,
      name: "Sobota",
      scheduledRecipeIds: [ids.scheduled],
    });
  });
});
