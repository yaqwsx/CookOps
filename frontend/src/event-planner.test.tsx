import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { EventPlanner } from "./event-planner";
import i18n, { defaultLocale } from "./i18n";

const {
  readEventPlanner,
  queueRecipeSchedule,
  queueScheduledRecipeContext,
  queueScheduledRecipeMove,
  queueAddedOverride,
  pullOrganization,
} = vi.hoisted(() => ({
  readEventPlanner: vi.fn(),
  queueRecipeSchedule: vi.fn(),
  queueScheduledRecipeContext: vi.fn(),
  queueScheduledRecipeMove: vi.fn(),
  queueAddedOverride: vi.fn(),
  pullOrganization: vi.fn(),
}));
vi.mock("./planner-projections", () => ({ readEventPlanner }));
vi.mock("./scheduled-recipe", () => ({
  queueRecipeSchedule,
  queueScheduledRecipeContext,
  queueScheduledRecipeMove,
}));
vi.mock("./scheduled-ingredient-override", () => ({ queueAddedOverride }));
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

  it("moves a card through labelled controls without drag precision", async () => {
    await i18n.changeLanguage(defaultLocale);
    readEventPlanner.mockResolvedValue({
      name: "Letní vaření",
      startDate: "2026-08-10",
      endDate: "2026-08-10",
      attendance: 12,
      lifecycle: "active",
      days: [{ id: ids.day, date: "2026-08-10", note: null }],
      roles: [{ id: ids.role, name: "Večeře", position: "a" }],
      recipes: [],
      scheduled: [
        {
          id: ids.recipe,
          name: "Chili",
          dinerCount: 12,
          consumptionPercentage: "100",
          selectedScaleAmount: "2",
          dayId: ids.day,
          roleId: ids.role,
          position: "a",
        },
      ],
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
    await user.click(await screen.findByText("Přesunout"));
    await user.clear(screen.getByLabelText("Pořadí"));
    await user.type(screen.getByLabelText("Pořadí"), "z9");
    await user.click(screen.getByRole("button", { name: "Přesunout sem" }));
    expect(queueScheduledRecipeMove).toHaveBeenCalledWith(
      "a6a58bd6-214e-49af-8fae-e5f974bf8e08",
      ids.organization,
      {
        scheduledRecipeId: ids.recipe,
        eventId: ids.event,
        eventDayId: ids.day,
        eventMealRoleId: ids.role,
        positionKey: "z9",
      },
    );
  });

  it("edits scaling through accessible controls", async () => {
    await i18n.changeLanguage(defaultLocale);
    readEventPlanner.mockResolvedValue({
      name: "Letní vaření",
      startDate: "2026-08-10",
      endDate: "2026-08-10",
      attendance: 12,
      lifecycle: "active",
      days: [{ id: ids.day, date: "2026-08-10", note: null }],
      roles: [{ id: ids.role, name: "Večeře", position: "a" }],
      recipes: [],
      scheduled: [
        {
          id: ids.recipe,
          name: "Chili",
          dinerCount: 12,
          consumptionPercentage: "100",
          selectedScaleAmount: "2",
          dayId: ids.day,
          roleId: ids.role,
          position: "a",
          lines: [],
        },
      ],
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
    await user.click(await screen.findByText("Upravit škálování"));
    await user.click(screen.getByRole("button", { name: "Použít doporučení" }));
    expect(queueScheduledRecipeContext).toHaveBeenCalledWith(
      "a6a58bd6-214e-49af-8fae-e5f974bf8e08",
      ids.organization,
      {
        scheduledRecipeId: ids.recipe,
        eventId: ids.event,
        consumptionPercentage: "100",
        selectedScaleAmount: null,
      },
    );
  });

  it("does not render move controls for an archived planner", async () => {
    readEventPlanner.mockResolvedValue({
      name: "Archived",
      startDate: "2026-08-10",
      endDate: "2026-08-10",
      attendance: 12,
      lifecycle: "archived",
      days: [{ id: ids.day, date: "2026-08-10", note: null }],
      roles: [{ id: ids.role, name: "Večeře", position: "a" }],
      recipes: [],
      scheduled: [
        {
          id: ids.recipe,
          name: "Chili",
          dinerCount: 12,
          dayId: ids.day,
          roleId: ids.role,
          position: "a",
        },
      ],
    });
    pullOrganization.mockResolvedValue(false);
    render(
      <EventPlanner
        eventId={ids.event}
        onUnauthenticated={vi.fn()}
        organizationId={ids.organization}
        userId="a6a58bd6-214e-49af-8fae-e5f974bf8e08"
      />,
    );
    await screen.findByRole("heading", { name: "Plán akce" });
    expect(
      screen.queryByText("Přesunout", { exact: true }),
    ).not.toBeInTheDocument();
  });

  it("adds an active catalog ingredient through the typed local override", async () => {
    await i18n.changeLanguage(defaultLocale);
    const ingredientId = "7d8b2b21-c378-4574-9e46-9338c81305ef";
    const ingredientVersionId = "8d8b2b21-c378-4574-9e46-9338c81305ef";
    const pinnedIngredientId = "9d8b2b21-c378-4574-9e46-9338c81305ef";
    readEventPlanner.mockResolvedValue({
      name: "Letní vaření",
      startDate: "2026-08-10",
      endDate: "2026-08-10",
      attendance: 12,
      lifecycle: "active",
      days: [{ id: ids.day, date: "2026-08-10", note: null }],
      roles: [{ id: ids.role, name: "Večeře", position: "a" }],
      recipes: [],
      ingredients: [
        { id: ingredientId, versionId: ingredientVersionId, name: "Paprika" },
        {
          id: pinnedIngredientId,
          versionId: "ad8b2b21-c378-4574-9e46-9338c81305ef",
          name: "Pinto",
        },
      ],
      scheduled: [
        {
          id: ids.recipe,
          name: "Chili",
          dinerCount: 12,
          consumptionPercentage: "100",
          selectedScaleAmount: "2",
          dayId: ids.day,
          roleId: ids.role,
          position: "a",
          lines: [{ id: "bd8b2b21-c378-4574-9e46-9338c81305ef", quantity: "1", ingredientId: pinnedIngredientId }],
          localAddedIngredients: [
            {
              id: "cd8b2b21-c378-4574-9e46-9338c81305ef",
              name: "Paprika",
              quantity: "1",
            },
          ],
        },
      ],
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
    await user.click(await screen.findByText("Přidat místní surovinu"));
    expect(screen.getByText("Místní surovina: Paprika · 1")).toBeVisible();
    expect(screen.queryByRole("option", { name: "Pinto" })).not.toBeInTheDocument();
    const addQuantity = screen.getAllByLabelText("Množství").at(-1);
    if (!addQuantity) throw new Error("Added ingredient quantity is missing");
    await user.clear(addQuantity);
    await user.type(addQuantity, "2.5");
    await user.click(screen.getByRole("button", { name: "Přidat surovinu" }));
    expect(queueAddedOverride).toHaveBeenCalledWith(
      "a6a58bd6-214e-49af-8fae-e5f974bf8e08",
      ids.organization,
      {
        eventId: ids.event,
        scheduledRecipeId: ids.recipe,
        ingredientId,
        ingredientVersionId,
        quantity: "2.5",
        includeInPortionWeight: true,
      },
    );
  });
});
