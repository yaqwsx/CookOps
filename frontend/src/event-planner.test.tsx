import { createEvent, fireEvent, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { EventPlanner } from "./event-planner";
import i18n, { defaultLocale } from "./i18n";

const {
  readEventPlanner,
  queueRecipeSchedule,
  queueScheduledRecipeContext,
  queueScheduledRecipeMove,
  queueScheduledRecipeCatalogUpdate,
  queueAddedOverride,
  queueEventDayCreate,
  queueEventDayNote,
  queueEventDayVisibility,
  queueEventMealRoleCreate,
  queueEventMealRoleName,
  pullOrganization,
  ensureArchivedEventCached,
} = vi.hoisted(() => ({
  readEventPlanner: vi.fn(),
  queueRecipeSchedule: vi.fn(),
  queueScheduledRecipeContext: vi.fn(),
  queueScheduledRecipeMove: vi.fn(),
  queueScheduledRecipeCatalogUpdate: vi.fn(),
  queueAddedOverride: vi.fn(),
  queueEventDayCreate: vi.fn(),
  queueEventDayNote: vi.fn(),
  queueEventDayVisibility: vi.fn(),
  queueEventMealRoleCreate: vi.fn(),
  queueEventMealRoleName: vi.fn(),
  pullOrganization: vi.fn(),
  ensureArchivedEventCached: vi.fn(),
}));
vi.mock("./planner-projections", () => ({ readEventPlanner }));
vi.mock("./scheduled-recipe", () => ({
  queueRecipeSchedule,
  queueScheduledRecipeContext,
  queueScheduledRecipeMove,
  queueScheduledRecipeCatalogUpdate,
}));
vi.mock("./scheduled-ingredient-override", () => ({ queueAddedOverride }));
vi.mock("./event-day", () => ({ queueEventDayCreate, queueEventDayNote, queueEventDayVisibility }));
vi.mock("./event-meal-role", () => ({ queueEventMealRoleCreate, queueEventMealRoleName }));
vi.mock("./sync-bootstrap", () => ({
  pullOrganization,
  SyncRequestError: class SyncRequestError extends Error {},
}));
vi.mock("./archive-cache", () => ({ ensureArchivedEventCached, dietaryTagSeedKeys: new Set(["vegetarian", "vegan", "gluten", "lactose"]) }));

const ids = {
  organization: "5ce17d2f-8365-4b1f-a80b-34d10425d51c",
  event: "3d8b2b21-c378-4574-9e46-9338c81305ef",
  day: "4d8b2b21-c378-4574-9e46-9338c81305ef",
  role: "5d8b2b21-c378-4574-9e46-9338c81305ef",
  recipe: "6d8b2b21-c378-4574-9e46-9338c81305ef",
};
const emptyPlannerCollections = {
  hiddenDays: [],
  retiredDays: [],
  retiredRoles: [],
  ingredients: [],
};

describe("EventPlanner", () => {
  afterEach(() => vi.clearAllMocks());

  function dataTransfer() {
    const values = new Map<string, string>();
    return {
      effectAllowed: "",
      dropEffect: "",
      setData: (type: string, value: string) => values.set(type, value),
      getData: (type: string) => values.get(type) ?? "",
    } as unknown as DataTransfer;
  }

  it("uses labelled non-drag controls to queue the existing typed command", async () => {
    await i18n.changeLanguage(defaultLocale);
    readEventPlanner.mockResolvedValue({
      name: "Letní vaření",
      startDate: "2026-08-10",
      endDate: "2026-08-10",
      attendance: 12,
      lifecycle: "active",
      days: [{ id: ids.day, date: "2026-08-10", note: "Připravit zeleninu" }],
      roles: [{ id: ids.role, name: "Večeře", position: "a", custom: false }],
      recipes: [
        {
          id: ids.recipe,
          versionId: "7d8b2b21-c378-4574-9e46-9338c81305ef",
          name: "Chili",
        },
      ],
      scheduled: [],
      ...emptyPlannerCollections,
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
    expect(screen.getByRole("combobox", { name: "Den" })).toBeVisible();
    expect(screen.getAllByLabelText("Chod")[0]).toBeVisible();
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

  it("schedules a catalog recipe dropped onto an active meal role", async () => {
    await i18n.changeLanguage(defaultLocale);
    readEventPlanner.mockResolvedValue({
      name: "Letní vaření", startDate: "2026-08-10", endDate: "2026-08-10", attendance: 12, lifecycle: "active",
      days: [{ id: ids.day, date: "2026-08-10", note: null }],
      roles: [{ id: ids.role, name: "Večeře", position: "a", retired: false, custom: false }],
      recipes: [{ id: ids.recipe, versionId: "7d8b2b21-c378-4574-9e46-9338c81305ef", name: "Chili" }],
      scheduled: [], ...emptyPlannerCollections,
    });
    pullOrganization.mockResolvedValue(false);
    render(<EventPlanner eventId={ids.event} onUnauthenticated={vi.fn()} organizationId={ids.organization} userId="a6a58bd6-214e-49af-8fae-e5f974bf8e08" />);
    const transfer = dataTransfer();
    const source = (await screen.findAllByRole("listitem")).find((item) => item.draggable);
    if (!source) throw new Error("drag source missing");
    fireEvent.dragStart(source, { dataTransfer: transfer });
    const target = screen.getByRole("region", { name: "Večeře" });
    const dragOver = createEvent.dragOver(target, { dataTransfer: transfer });
    fireEvent(target, dragOver);
    expect(dragOver.defaultPrevented).toBe(true);
    expect(screen.getByText("Pusťte recept sem")).toBeVisible();
    fireEvent.drop(target, { dataTransfer: transfer });
    expect(queueRecipeSchedule).toHaveBeenCalledWith("a6a58bd6-214e-49af-8fae-e5f974bf8e08", ids.organization, { eventId: ids.event, eventDayId: ids.day, eventMealRoleId: ids.role, recipeId: ids.recipe });
  });

  it("ignores malformed or foreign drag payloads", async () => {
    await i18n.changeLanguage(defaultLocale);
    readEventPlanner.mockResolvedValue({
      name: "Letní vaření", startDate: "2026-08-10", endDate: "2026-08-10", attendance: 12, lifecycle: "active",
      days: [{ id: ids.day, date: "2026-08-10", note: null }],
      roles: [{ id: ids.role, name: "Večeře", position: "a", retired: false, custom: false }],
      recipes: [{ id: ids.recipe, versionId: "7d8b2b21-c378-4574-9e46-9338c81305ef", name: "Chili" }],
      scheduled: [], ...emptyPlannerCollections,
    });
    pullOrganization.mockResolvedValue(false);
    render(<EventPlanner eventId={ids.event} onUnauthenticated={vi.fn()} organizationId={ids.organization} userId="a6a58bd6-214e-49af-8fae-e5f974bf8e08" />);
    const source = (await screen.findAllByRole("listitem")).find((item) => item.draggable);
    if (!source) throw new Error("drag source missing");
    const transfer = dataTransfer();
    fireEvent.dragStart(source, { dataTransfer: transfer });
    transfer.setData("application/x-cookops-planner", "{broken");
    const target = screen.getByRole("region", { name: "Večeře" });
    fireEvent.dragOver(target, { dataTransfer: transfer });
    fireEvent.drop(target, { dataTransfer: transfer });
    await Promise.resolve();
    expect(queueRecipeSchedule).not.toHaveBeenCalled();
    expect(queueScheduledRecipeMove).not.toHaveBeenCalled();
  });

  it("does not drag retired cards or drop onto an archived planner", async () => {
    await i18n.changeLanguage(defaultLocale);
    readEventPlanner.mockResolvedValue({
      name: "Archivovaná akce", startDate: "2026-08-10", endDate: "2026-08-10", attendance: 12, lifecycle: "archived",
      days: [{ id: ids.day, date: "2026-08-10", note: null }],
      roles: [{ id: ids.role, name: "Večeře", position: "a", retired: false, custom: false }], recipes: [],
      scheduled: [{ id: ids.recipe, recipeId: ids.recipe, name: "Chili", dinerCount: 12, dayId: ids.day, roleId: ids.role, retired: true, position: "a", detailLines: [], preparedWeight: null, perDinerWeight: null, hasLocalOverrides: false, dietaryWarnings: [{ exceptionName: "Alex", tagNames: ["vegan"], tagDescriptors: [{ id: "dietary-vegan", seedKey: "vegan" }], ingredientNames: ["Tofu"] }] }], ...emptyPlannerCollections,
    });
    pullOrganization.mockResolvedValue(false);
    render(<EventPlanner eventId={ids.event} onUnauthenticated={vi.fn()} organizationId={ids.organization} userId="a6a58bd6-214e-49af-8fae-e5f974bf8e08" />);
    expect(await screen.findByText("Alex: štítky Veganské se týkají surovin Tofu.")).toBeVisible();
    const card = (await screen.findAllByRole("listitem")).find((item) => item.textContent?.includes("Chili"));
    if (!card) throw new Error("retired card missing");
    const transfer = dataTransfer();
    fireEvent.dragStart(card, { dataTransfer: transfer });
    fireEvent.drop(screen.getByRole("region", { name: "Večeře" }), { dataTransfer: transfer });
    expect(queueRecipeSchedule).not.toHaveBeenCalled();
    expect(queueScheduledRecipeMove).not.toHaveBeenCalled();
  });

  it("moves a visible scheduled card dropped onto another active slot", async () => {
    await i18n.changeLanguage(defaultLocale);
    const secondDay = "8d8b2b21-c378-4574-9e46-9338c81305ef";
    const secondRole = "9d8b2b21-c378-4574-9e46-9338c81305ef";
    readEventPlanner.mockResolvedValue({
      name: "Letní vaření", startDate: "2026-08-10", endDate: "2026-08-11", attendance: 12, lifecycle: "active",
      days: [{ id: ids.day, date: "2026-08-10", note: null }, { id: secondDay, date: "2026-08-11", note: null }],
      roles: [{ id: ids.role, name: "Večeře", position: "a", retired: false, custom: false }, { id: secondRole, name: "Oběd", position: "b", retired: false, custom: false }],
      recipes: [],
      scheduled: [{ id: ids.recipe, recipeId: ids.recipe, recipeVersionId: "7d8b2b21-c378-4574-9e46-9338c81305ef", name: "Chili", dinerCount: 12, dayId: ids.day, roleId: ids.role, position: "a", retired: false, detailLines: [], preparedWeight: null, perDinerWeight: null, hasLocalOverrides: false, lines: [], localAddedIngredients: [], dietaryWarnings: [{ exceptionName: "Alex", tagNames: ["Vegan"], ingredientNames: ["Tofu"] }], catalogUpdateAvailable: false, catalogUpdateChanges: { added: 0, removed: 0, changed: 0 }, catalogScaleImpact: { reset: false } }],
      ...emptyPlannerCollections,
    });
    pullOrganization.mockResolvedValue(false);
    render(<EventPlanner eventId={ids.event} onUnauthenticated={vi.fn()} organizationId={ids.organization} userId="a6a58bd6-214e-49af-8fae-e5f974bf8e08" />);
    expect(await screen.findByText("Alex: štítky Vegan se týkají surovin Tofu.")).toBeVisible();
    const transfer = dataTransfer();
    const source = (await screen.findAllByRole("listitem")).find((item) => item.draggable);
    const targets = screen.getAllByRole("region", { name: "Oběd" });
    if (!source || !targets[1]) throw new Error("drag target missing");
    fireEvent.dragStart(source, { dataTransfer: transfer });
    fireEvent.drop(targets[1], { dataTransfer: transfer });
    expect(queueScheduledRecipeMove).toHaveBeenCalledWith("a6a58bd6-214e-49af-8fae-e5f974bf8e08", ids.organization, { scheduledRecipeId: ids.recipe, eventId: ids.event, eventDayId: secondDay, eventMealRoleId: secondRole, positionKey: "a" });
  });

  it("saves a day note through an accessible native textarea", async () => {
    await i18n.changeLanguage(defaultLocale);
    readEventPlanner.mockResolvedValue({
      name: "Letní vaření", startDate: "2026-08-10", endDate: "2026-08-10", attendance: 12,
      lifecycle: "active", days: [{ id: ids.day, date: "2026-08-10", note: "Původní" }], roles: [], recipes: [], scheduled: [], ...emptyPlannerCollections,
    });
    pullOrganization.mockResolvedValue(false);
    const user = userEvent.setup();
    render(<EventPlanner eventId={ids.event} onUnauthenticated={vi.fn()} organizationId={ids.organization} userId="a6a58bd6-214e-49af-8fae-e5f974bf8e08" />);
    const note = await screen.findByLabelText("Poznámka dne");
    await user.clear(note);
    await user.type(note, "Nová poznámka");
    await user.click(screen.getByRole("button", { name: "Uložit poznámku dne" }));
    expect(queueEventDayNote).toHaveBeenCalledWith("a6a58bd6-214e-49af-8fae-e5f974bf8e08", ids.organization, { eventDayId: ids.day, eventId: ids.event, note: "Nová poznámka" });
  });

  it("adds a custom meal role through a labelled native input", async () => {
    await i18n.changeLanguage(defaultLocale);
    readEventPlanner.mockResolvedValue({
      name: "Letní vaření", startDate: "2026-08-10", endDate: "2026-08-10", attendance: 12,
      lifecycle: "active", days: [], ...emptyPlannerCollections, roles: [], recipes: [], scheduled: [],
    });
    pullOrganization.mockResolvedValue(false);
    const user = userEvent.setup();
    render(<EventPlanner eventId={ids.event} onUnauthenticated={vi.fn()} organizationId={ids.organization} userId="a6a58bd6-214e-49af-8fae-e5f974bf8e08" />);
    await user.type(await screen.findByLabelText("Název chodu"), "Pozdní večeře");
    await user.click(screen.getByRole("button", { name: "Přidat chod" }));
    expect(queueEventMealRoleCreate).toHaveBeenCalledWith("a6a58bd6-214e-49af-8fae-e5f974bf8e08", ids.organization, { eventId: ids.event, customName: "Pozdní večeře" });
  });

  it("renames a custom meal role through an accessible native input", async () => {
    await i18n.changeLanguage(defaultLocale);
    readEventPlanner.mockResolvedValue({
      name: "Letní vaření", startDate: "2026-08-10", endDate: "2026-08-10", attendance: 12,
      lifecycle: "active", days: [], ...emptyPlannerCollections,
      roles: [{ id: ids.role, name: "Večeře", position: "a", custom: true }], recipes: [], scheduled: [],
    });
    pullOrganization.mockResolvedValue(false);
    queueEventMealRoleName.mockResolvedValue(undefined);
    const user = userEvent.setup();
    render(<EventPlanner eventId={ids.event} onUnauthenticated={vi.fn()} organizationId={ids.organization} userId="a6a58bd6-214e-49af-8fae-e5f974bf8e08" />);
    const nameInputs = await screen.findAllByLabelText("Název chodu");
    await user.clear(nameInputs[1]);
    await user.type(nameInputs[1], "Pozdní večeře");
    await user.click(screen.getByRole("button", { name: "Uložit název chodu" }));
    expect(queueEventMealRoleName).toHaveBeenCalledWith("a6a58bd6-214e-49af-8fae-e5f974bf8e08", ids.organization, { eventId: ids.event, eventMealRoleId: ids.role, customName: "Pozdní večeře" });
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
      roles: [{ id: ids.role, name: "Večeře", position: "a", custom: false }],
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
          recipeId: ids.recipe,
          recipeVersionId: "7d8b2b21-c378-4574-9e46-9338c81305ef",
          detailLines: [],
          preparedWeight: null,
          perDinerWeight: null,
          hasLocalOverrides: false,
          catalogUpdateAvailable: true,
          catalogUpdateChanges: { added: 1, removed: 0, changed: 0 },
          catalogScaleImpact: { currentUnitId: "a", targetUnitId: "b", currentUnitName: "porce", targetUnitName: "osoba", reset: false, targetBase: "1", suggestedAmount: "12" },
        },
      ],
      ...emptyPlannerCollections,
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
      roles: [{ id: ids.role, name: "Večeře", position: "a", custom: false }],
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
          detailLines: [],
          preparedWeight: null,
          perDinerWeight: null,
          hasLocalOverrides: false,
        },
      ],
      ...emptyPlannerCollections,
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

  it("shows scaling units and queues the selected catalog-update choice", async () => {
    await i18n.changeLanguage(defaultLocale);
    readEventPlanner.mockResolvedValue({
      name: "Letní vaření",
      startDate: "2026-08-10",
      endDate: "2026-08-10",
      attendance: 12,
      lifecycle: "active",
      days: [{ id: ids.day, date: "2026-08-10", note: null }],
      roles: [{ id: ids.role, name: "Večeře", position: "a", custom: false }],
      recipes: [{ id: ids.recipe, versionId: "8d8b2b21-c378-4574-9e46-9338c81305ef", name: "Chili" }],
      scheduled: [{
        id: ids.recipe,
        recipeId: ids.recipe,
        recipeVersionId: "7d8b2b21-c378-4574-9e46-9338c81305ef",
        name: "Chili",
        dinerCount: 12,
        consumptionPercentage: "100",
        selectedScaleAmount: "2",
        dayId: ids.day,
        roleId: ids.role,
        position: "a",
        lines: [],
        detailLines: [],
        preparedWeight: null,
        perDinerWeight: null,
        hasLocalOverrides: false,
        localAddedIngredients: [],
        catalogUpdateAvailable: true,
        catalogUpdateChanges: { added: 1, removed: 0, changed: 1 },
        catalogScaleImpact: { currentUnitName: "porce", targetUnitName: "osoba", reset: true, suggestedAmount: "12", currentUnitId: "a", targetUnitId: "b", targetBase: "1" },
      }],
      ...emptyPlannerCollections,
    });
    pullOrganization.mockResolvedValue(false);
    const user = userEvent.setup();
    render(<EventPlanner eventId={ids.event} onUnauthenticated={vi.fn()} organizationId={ids.organization} userId="a6a58bd6-214e-49af-8fae-e5f974bf8e08" />);
    await user.click(await screen.findByText("Náhled aktualizace katalogu"));
    expect(screen.getByText(/porce.*osoba/)).toBeVisible();
    expect(screen.getByText(/návrh podle účasti 12/)).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Zachovat úpravy" }));
    expect(queueScheduledRecipeCatalogUpdate).toHaveBeenCalledWith(
      "a6a58bd6-214e-49af-8fae-e5f974bf8e08",
      ids.organization,
      expect.objectContaining({ preserveOverrides: true }),
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
      roles: [{ id: ids.role, name: "Večeře", position: "a", custom: false }],
      recipes: [],
      scheduled: [
        {
          id: ids.recipe,
          name: "Chili",
          dinerCount: 12,
          dayId: ids.day,
          roleId: ids.role,
          position: "a",
          detailLines: [],
          preparedWeight: null,
          perDinerWeight: null,
          hasLocalOverrides: false,
        },
      ],
      ...emptyPlannerCollections,
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
    expect(screen.getByText("Tato akce je archivovaná a plán je jen pro čtení.")).toBeVisible();
    expect(screen.queryByText("Náhled aktualizace katalogu")).not.toBeInTheDocument();
    expect(queueScheduledRecipeCatalogUpdate).not.toHaveBeenCalled();
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
      roles: [{ id: ids.role, name: "Večeře", position: "a", custom: false }],
      recipes: [],
      ...emptyPlannerCollections,
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
          detailLines: [{ id: "cd8b2b21-c378-4574-9e46-9338c81305ef", name: "Místní surovina: Paprika", quantity: "1", unitName: null, note: null }],
          preparedWeight: null,
          perDinerWeight: null,
          hasLocalOverrides: false,
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
    const card = (await screen.findAllByRole("listitem")).find((item) => item.textContent?.includes("Chili"));
    if (!card) throw new Error("Scheduled recipe card is missing");
    await user.click(within(card).getByText("Podrobnosti receptu"));
    await user.click(await screen.findByText("Přidat místní surovinu"));
    expect(within(card).getAllByText("Místní surovina: Paprika: 1")).toHaveLength(1);
    expect(within(card).getByText("Místní surovina: Paprika: 1")).toBeVisible();
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

  it("expands pinned recipe details and keeps archived cards read-only", async () => {
    await i18n.changeLanguage(defaultLocale);
    readEventPlanner.mockResolvedValue({
      name: "Archivovaná akce", startDate: "2026-08-10", endDate: "2026-08-10", attendance: 2, lifecycle: "archived",
      days: [{ id: ids.day, date: "2026-08-10", note: null }], roles: [{ id: ids.role, name: "Večeře", position: "a", retired: false, custom: false }], recipes: [],
      scheduled: [{ id: ids.recipe, recipeId: ids.recipe, recipeVersionId: "old-version", recipeVersionName: "Pinned soup", recipeDescription: "# Pinned\n<script>bad()</script>", scalingUnitName: "portion", scaleMode: "manual", hasLocalOverrides: true, detailLines: [{ id: "line", name: "Paprika", quantity: "2", unitName: "ks", note: "nakrájet" }], preparedWeight: "3", perDinerWeight: "1.5", dinerCount: 2, consumptionPercentage: "100", selectedScaleAmount: "3", dayId: ids.day, roleId: ids.role, position: "a", retired: false, lines: [], localAddedIngredients: [], catalogUpdateAvailable: false, catalogUpdateChanges: { added: 0, removed: 0, changed: 0 }, catalogScaleImpact: { reset: false } }], ...emptyPlannerCollections,
    });
    pullOrganization.mockResolvedValue(false);
    render(<EventPlanner eventId={ids.event} onUnauthenticated={vi.fn()} organizationId={ids.organization} userId="a6a58bd6-214e-49af-8fae-e5f974bf8e08" />);
    const details = await screen.findByText("Podrobnosti receptu");
    expect(screen.getByText("Paprika: 2 ks · nakrájet")).not.toBeVisible();
    await userEvent.setup().click(details);
    expect(screen.getByText("Paprika: 2 ks · nakrájet")).toBeVisible();
    expect(screen.getByText("Připravená hmotnost: 3 · na strávníka 1.5")).toBeVisible();
    expect(screen.getByText(/# Pinned/)).toHaveTextContent("<script>bad()</script>");
    expect(screen.queryByRole("button", { name: "Upravit škálování" })).not.toBeInTheDocument();
  });
});
