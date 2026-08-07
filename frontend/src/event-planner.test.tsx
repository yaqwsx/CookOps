import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { EventPlanner } from "./event-planner";
import i18n, { defaultLocale } from "./i18n";

const { readEventPlanner, queueRecipeSchedule, pullOrganization } = vi.hoisted(
  () => ({
    readEventPlanner: vi.fn(),
    queueRecipeSchedule: vi.fn(),
    pullOrganization: vi.fn(),
  }),
);
vi.mock("./planner-projections", () => ({ readEventPlanner }));
vi.mock("./scheduled-recipe", () => ({ queueRecipeSchedule }));
vi.mock("./sync-bootstrap", () => ({
  pullOrganization,
  SyncRequestError: class SyncRequestError extends Error {},
}));

const ids = {
  organization: "5ce17d2f-8365-4b1f-a80b-34d10425d51c",
  event: "3d8b2b21-c378-4574-9e46-9338c81305ef",
  day: "4d8b2b21-c378-4574-9e46-9338c81305ef",
  role: "5d8b2b21-c378-4574-9e46-9338c81305ef",
  recipe: "6d8b2b21-c378-4574-9e46-9338c81305ef",
};

describe("EventPlanner", () => {
  afterEach(() => vi.clearAllMocks());

  it("uses labelled non-drag controls to queue the existing typed command", async () => {
    await i18n.changeLanguage(defaultLocale);
    readEventPlanner.mockResolvedValue({
      name: "Letní vaření",
      startDate: "2026-08-10",
      endDate: "2026-08-10",
      attendance: 12,
      lifecycle: "active",
      days: [{ id: ids.day, date: "2026-08-10", note: "Připravit zeleninu" }],
      roles: [{ id: ids.role, name: "Večeře", position: "a" }],
      recipes: [
        {
          id: ids.recipe,
          versionId: "7d8b2b21-c378-4574-9e46-9338c81305ef",
          name: "Chili",
        },
      ],
      scheduled: [],
    });
    pullOrganization.mockResolvedValue(false);
    const user = userEvent.setup();
    render(
      <EventPlanner
        eventId={ids.event}
        onUnauthenticated={vi.fn()}
        organizationId={ids.organization}
        userId="a6a58bd6-214e-49af-8fae-e5f974bf8e08"
      />,
    );
    await screen.findByRole("heading", { name: "Plán akce" });
    expect(screen.getByLabelText("Den")).toBeVisible();
    expect(screen.getByLabelText("Chod")).toBeVisible();
    expect(screen.getByLabelText("Recept")).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Přidat do plánu" }));
    expect(queueRecipeSchedule).toHaveBeenCalledWith(
      "a6a58bd6-214e-49af-8fae-e5f974bf8e08",
      ids.organization,
      {
        eventId: ids.event,
        eventDayId: ids.day,
        eventMealRoleId: ids.role,
        recipeId: ids.recipe,
      },
    );
  });
});
