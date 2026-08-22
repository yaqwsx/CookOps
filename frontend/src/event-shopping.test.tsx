import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { EventShopping } from "./event-shopping";
import i18n, { defaultLocale } from "./i18n";
import { SyncRequestError } from "./sync-bootstrap";

const {
  readEventPlanner,
  readEventCosts,
  readShoppingList,
  readShoppingLists,
  queueShoppingList,
  queueShoppingListRefresh,
  queueShoppingAvailableSupply,
  queueShoppingContributionFulfilment,
  queueShoppingManualPurchaseTarget,
  queueShoppingRowFulfilment,
  queueAdHocShoppingItem,
  queueAdHocShoppingItemFulfilment,
  queueAdHocShoppingItemLifecycle,
  queueAdHocShoppingItemUpdate,
  ensureArchivedEventCached,
  pullOrganization,
} = vi.hoisted(() => ({
  readEventPlanner: vi.fn(),
  readEventCosts: vi.fn().mockResolvedValue(undefined),
  readShoppingList: vi.fn(),
  readShoppingLists: vi.fn(),
  queueShoppingList: vi.fn(),
  queueShoppingListRefresh: vi.fn(),
  queueShoppingAvailableSupply: vi.fn(),
  queueShoppingContributionFulfilment: vi.fn(),
  queueShoppingManualPurchaseTarget: vi.fn(),
  queueShoppingRowFulfilment: vi.fn(),
  queueAdHocShoppingItem: vi.fn(),
  queueAdHocShoppingItemFulfilment: vi.fn(),
  queueAdHocShoppingItemLifecycle: vi.fn(),
  queueAdHocShoppingItemUpdate: vi.fn(),
  ensureArchivedEventCached: vi.fn(),
  pullOrganization: vi.fn(),
}));
vi.mock("./planner-projections", () => ({ readEventPlanner }));
vi.mock("./event-cost-projections", () => ({ readEventCosts }));
vi.mock("./shopping-projections", () => ({
  readShoppingLists,
  readShoppingList,
}));
vi.mock("./shopping-list", () => ({
  hasQueuedShoppingListRefresh: vi.fn().mockResolvedValue(false),
  queueShoppingList,
  queueShoppingListRefresh,
}));
vi.mock("./shopping-operations", () => ({
  queueShoppingAvailableSupply,
  queueShoppingContributionFulfilment,
  queueShoppingManualPurchaseTarget,
  queueShoppingRowFulfilment,
}));
vi.mock("./ad-hoc-shopping-item", () => ({
  queueAdHocShoppingItem,
  queueAdHocShoppingItemFulfilment,
  queueAdHocShoppingItemLifecycle,
  queueAdHocShoppingItemUpdate,
}));
vi.mock("./sync-bootstrap", () => ({
  pullOrganization,
  SyncRequestError: class SyncRequestError extends Error {
    constructor(readonly status: number) { super("Sync request failed."); }
  },
}));
vi.mock("./archive-cache", () => ({ ensureArchivedEventCached }));

const ids = {
  user: "a6a58bd6-214e-49af-8fae-e5f974bf8e08",
  organization: "5ce17d2f-8365-4b1f-a80b-34d10425d51c",
  event: "3d8b2b21-c378-4574-9e46-9338c81305ef",
  scheduled: "8d8b2b21-c378-4574-9e46-9338c81305ef",
};

describe("EventShopping", () => {
  afterEach(() => vi.clearAllMocks());

  it("renders summary costs and clears stale costs and read-only list on route switch", async () => {
    await i18n.changeLanguage(defaultLocale);
    const costs = { budget: "30", total: "20", actual: "10", remaining: "20", currency: "CZK", expectedShopping: "20", missingIngredients: [], scheduled: new Map() };
    readEventPlanner.mockResolvedValue({ name: "Letní vaření", lifecycle: "active", attendance: 4, scheduled: [] });
    readEventCosts.mockResolvedValue(costs);
    readShoppingLists.mockResolvedValue([]);
    readShoppingList.mockResolvedValue({ id: "list", name: "Starý seznam", sourceCount: 0, sourceRecipeIds: [], adHocItems: [], rows: [], quantityUnits: [], storeSections: [] });
    pullOrganization.mockResolvedValue(false);
    const view = render(<EventShopping eventId={ids.event} shoppingListId="list" onBack={vi.fn()} onOpenList={vi.fn()} onUnauthenticated={vi.fn()} organizationId={ids.organization} userId={ids.user} />);
    expect(await screen.findByText(/30/)).toBeInTheDocument();
    expect(await screen.findByText("Starý seznam")).toBeInTheDocument();

    readEventCosts.mockResolvedValue(undefined);
    readEventPlanner.mockResolvedValue({ name: "Nové vaření", lifecycle: "planned", attendance: 2, scheduled: [] });
    view.rerender(<EventShopping eventId={`${ids.event}-next`} onBack={vi.fn()} onOpenList={vi.fn()} onUnauthenticated={vi.fn()} organizationId={ids.organization} userId={ids.user} />);
    await waitFor(() => expect(screen.getByRole("heading", { name: "Nové vaření" })).toBeInTheDocument());
    expect(screen.queryByText(/30/)).not.toBeInTheDocument();
    expect(screen.queryByText("Starý seznam")).not.toBeInTheDocument();
    expect(screen.getByText("2")).toBeInTheDocument();
  });

  it("caches the archived event after organization pull completes", async () => {
    await i18n.changeLanguage(defaultLocale);
    readEventPlanner.mockResolvedValue({ name: "Letní vaření", lifecycle: "archived", currentArchiveSnapshotId: ids.event, scheduled: [] });
    readShoppingLists.mockResolvedValue([]);
    let resolvePull!: (value: boolean) => void;
    pullOrganization.mockReturnValue(new Promise<boolean>((resolve) => { resolvePull = resolve; }));

    render(
      <EventShopping
        eventId={ids.event}
        onBack={vi.fn()}
        onOpenList={vi.fn()}
        onUnauthenticated={vi.fn()}
        organizationId={ids.organization}
        userId={ids.user}
      />,
    );

    await waitFor(() => expect(pullOrganization).toHaveBeenCalled());
    expect(ensureArchivedEventCached).not.toHaveBeenCalled();
    resolvePull(false);
    await waitFor(() =>
      expect(ensureArchivedEventCached).toHaveBeenCalledWith(ids.user, ids.organization, ids.event),
    );
  });

  it("routes archive authentication failures to the unauthenticated handler", async () => {
    await i18n.changeLanguage(defaultLocale);
    readEventPlanner.mockResolvedValue({ name: "Letní vaření", lifecycle: "active", scheduled: [] });
    readShoppingLists.mockResolvedValue([]);
    pullOrganization.mockResolvedValue(false);
    ensureArchivedEventCached.mockRejectedValue(new SyncRequestError(401));
    const onUnauthenticated = vi.fn();
    render(<EventShopping eventId={ids.event} onBack={vi.fn()} onOpenList={vi.fn()} onUnauthenticated={onUnauthenticated} organizationId={ids.organization} userId={ids.user} />);
    await waitFor(() => expect(onUnauthenticated).toHaveBeenCalledOnce());
    expect(screen.queryByText("Nákupy se nepodařilo načíst.")).not.toBeInTheDocument();
  });

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
      days: [{ id: "day", date: "2026-08-10" }],
      roles: [{ id: "role", name: "Dinner", position: "a", custom: false }],
    });
    readShoppingLists.mockResolvedValue([]);
    pullOrganization.mockResolvedValue(false);
    const user = userEvent.setup();
    render(
      <EventShopping
        eventId={ids.event}
        onBack={vi.fn()}
        onOpenList={vi.fn()}
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

  it("fails closed for a partial planner projection", async () => {
    await i18n.changeLanguage(defaultLocale);
    readEventPlanner.mockResolvedValue({
      name: "Letní vaření",
      lifecycle: "active",
    });
    readShoppingLists.mockResolvedValue([]);
    pullOrganization.mockResolvedValue(false);
    render(<EventShopping eventId={ids.event} onBack={vi.fn()} onOpenList={vi.fn()} onUnauthenticated={vi.fn()} organizationId={ids.organization} userId={ids.user} />);
    await screen.findByRole("heading", { name: "Nákupy" });
    expect(screen.queryByText("V plánu zatím nejsou žádné recepty.")).toBeInTheDocument();
  });

  it("keeps a mobile-sized shopping row editable through the typed outbox only", async () => {
    await i18n.changeLanguage(defaultLocale);
    readEventPlanner.mockResolvedValue({
      name: "Letní vaření",
      lifecycle: "active",
      scheduled: [],
    });
    readShoppingLists.mockResolvedValue([]);
    readShoppingList.mockResolvedValue({
      id: "9d8b2b21-c378-4574-9e46-9338c81305ef",
      name: "Sobota",
      sourceCount: 1,
      createdAt: "2026-08-07T12:00:00Z",
      currentGenerationRevisionId: "0e8b2b21-c378-4574-9e46-9338c81305ef",
      sourceRecipeIds: [ids.scheduled],
      rows: [
        {
          id: "1e8b2b21-c378-4574-9e46-9338c81305ef",
          ingredientName: "Rajčata",
          sectionName: null,
          availableSupply: "0",
          manualPurchaseTarget: null,
          generatedRequirement: "4",
          target: "3",
          remaining: "2",
          unit: "kg",
          fulfilled: false,
          partial: true,
          notRequired: false,
          fulfilmentAttribution: {
            updatedAt: "2026-08-22T12:34:56Z",
            updatedByUserId: ids.user,
          },
          contributions: [
              {
                id: "2e8b2b21-c378-4574-9e46-9338c81305ef",
                generated: "2",
                requiredQuantity: "2",
                fulfilled: false,
                partial: true,
                retired: false,
                source: "Chili",
                recipeDescription: "Smoky tomato stew",
                day: "2026-08-10",
                mealRole: "Dinner",
                lineNotes: ["diced"],
                recipeNotes: [],
                ingredientNotes: [],
                estimatedUnitPrice: "3 / 1 kg (EUR)",
                expectedCost: "6.00 EUR",
                fulfilmentAttribution: {
                  updatedAt: "2026-08-22T12:34:56Z",
                  updatedByUserId: "00000000-0000-4000-8000-000000000004",
                },
              },
          ],
        },
      ],
      adHocItems: [],
      quantityUnits: [],
      storeSections: [],
    });
    pullOrganization.mockResolvedValue(false);
    const user = userEvent.setup();
    const view = render(
      <EventShopping
        eventId={ids.event}
        onBack={vi.fn()}
        onOpenList={vi.fn()}
        onUnauthenticated={vi.fn()}
        organizationId={ids.organization}
        shoppingListId="9d8b2b21-c378-4574-9e46-9338c81305ef"
        userId={ids.user}
      />,
    );
    const fulfilmentCheckbox = await screen.findByLabelText("Nakoupeno");
    await waitFor(() => expect(fulfilmentCheckbox).toHaveProperty("indeterminate", true));
    expect(fulfilmentCheckbox).toHaveAttribute("aria-checked", "mixed");
    expect(screen.getByText(/Nakoupil\(a\) vy/)).toBeVisible();
    await user.click(fulfilmentCheckbox);
    expect(queueShoppingRowFulfilment).toHaveBeenCalledWith(
      ids.user,
      ids.organization,
      expect.objectContaining({ fulfilled: true }),
    );
    await user.click(screen.getByText("Příspěvky receptů"));
    expect(screen.getAllByText("Vygenerovaná potřeba")).toHaveLength(2);
    expect(screen.getAllByText("4 kg")).toHaveLength(1);
    expect(screen.getAllByText("3 kg")).toHaveLength(3);
    const contributionCheckbox = screen.getByLabelText("Chili · 2 kg");
    expect(screen.getByText(/Nakoupil\(a\) 00000000/)).toBeVisible();
    await waitFor(() => expect(contributionCheckbox).toHaveProperty("indeterminate", true));
    expect(contributionCheckbox).toHaveAttribute("aria-checked", "mixed");
    await user.click(contributionCheckbox);
    expect(queueShoppingContributionFulfilment).toHaveBeenCalledWith(
      ids.user,
      ids.organization,
      expect.objectContaining({
        fulfilled: true,
        shoppingContributionId: "2e8b2b21-c378-4574-9e46-9338c81305ef",
      }),
    );
    expect(screen.getAllByText("Vygenerovaná potřeba")).toHaveLength(2);
    expect(screen.getAllByText("Plánovaný nákup")).toHaveLength(3);
    expect(screen.getByText("Smoky tomato stew")).toBeVisible();
    expect(screen.getByText("2026-08-10")).toBeVisible();
    expect(screen.getByText("6.00 EUR")).toBeVisible();
    readShoppingList.mockResolvedValue({
      id: "9d8b2b21-c378-4574-9e46-9338c81305ef",
      name: "Sobota",
      sourceCount: 1,
      createdAt: "2026-08-07T12:00:00Z",
      currentGenerationRevisionId: "0e8b2b21-c378-4574-9e46-9338c81305ef",
      sourceRecipeIds: [ids.scheduled],
      rows: [
        {
          id: "1e8b2b21-c378-4574-9e46-9338c81305ef",
          ingredientName: "Rajčata",
          sectionName: null,
          availableSupply: "3",
          manualPurchaseTarget: null,
          target: "0",
          remaining: "0",
          unit: "kg",
          fulfilled: false,
          notRequired: true,
          contributions: [],
        },
      ],
      adHocItems: [],
      quantityUnits: [],
      storeSections: [],
    });
    view.rerender(
      <EventShopping
        eventId="4d8b2b21-c378-4574-9e46-9338c81305ef"
        onBack={vi.fn()}
        onOpenList={vi.fn()}
        onUnauthenticated={vi.fn()}
        organizationId={ids.organization}
        shoppingListId="9d8b2b21-c378-4574-9e46-9338c81305ef"
        userId={ids.user}
      />,
    );
    await waitFor(() =>
      expect(screen.getByLabelText("K dispozici (kg)")).toHaveValue(3),
    );
  });

  it("filters completed and unnecessary aggregate rows without empty sections", async () => {
    await i18n.changeLanguage(defaultLocale);
    readEventPlanner.mockResolvedValue({ name: "Letní vaření", lifecycle: "active", scheduled: [] });
    readShoppingLists.mockResolvedValue([]);
    readShoppingList.mockResolvedValue({
      id: "9d8b2b21-c378-4574-9e46-9338c81305ef",
      name: "Sobota",
      sourceCount: 1,
      createdAt: "2026-08-07T12:00:00Z",
      currentGenerationRevisionId: "0e8b2b21-c378-4574-9e46-9338c81305ef",
      sourceRecipeIds: [ids.scheduled],
      rows: [
        { id: "completed", ingredientName: "Nakoupená rajčata", sectionName: "Zelenina", availableSupply: "0", manualPurchaseTarget: null, target: "1", remaining: "0", unit: "kg", fulfilled: true, notRequired: false, contributions: [] },
        { id: "unneeded", ingredientName: "Voda", sectionName: "Nápoje", availableSupply: "1", manualPurchaseTarget: null, target: "0", remaining: "0", unit: "l", fulfilled: false, notRequired: true, contributions: [] },
        { id: "open", ingredientName: "Cibule", sectionName: "Zelenina", availableSupply: "0", manualPurchaseTarget: null, target: "2", remaining: "2", unit: "kg", fulfilled: false, notRequired: false, contributions: [] },
      ],
      adHocItems: [],
      quantityUnits: [],
      storeSections: [],
    });
    pullOrganization.mockResolvedValue(false);
    const user = userEvent.setup();
    render(
      <EventShopping
        eventId={ids.event}
        onBack={vi.fn()}
        onOpenList={vi.fn()}
        onUnauthenticated={vi.fn()}
        organizationId={ids.organization}
        shoppingListId="9d8b2b21-c378-4574-9e46-9338c81305ef"
        userId={ids.user}
      />,
    );

    await screen.findByText("Nakoupená rajčata");
    await user.click(screen.getByLabelText("Skrýt nakoupené a nepotřebné položky"));
    expect(screen.queryByText("Nakoupená rajčata")).not.toBeInTheDocument();
    expect(screen.queryByText("Voda")).not.toBeInTheDocument();
    expect(screen.getByText("Cibule")).toBeVisible();
    expect(screen.getAllByRole("heading", { level: 4 })).toHaveLength(1);
    expect(screen.getByRole("heading", { level: 4, name: "Zelenina" })).toBeVisible();
    await user.click(screen.getByLabelText("Skrýt nakoupené a nepotřebné položky"));
    expect(screen.getByText("Nakoupená rajčata")).toBeVisible();
    expect(screen.getByText("Voda")).toBeVisible();
  });

  it("shows a neutral message when the filter hides every aggregate row", async () => {
    await i18n.changeLanguage(defaultLocale);
    readEventPlanner.mockResolvedValue({ name: "Letní vaření", lifecycle: "active", scheduled: [] });
    readShoppingLists.mockResolvedValue([]);
    readShoppingList.mockResolvedValue({
      id: "9d8b2b21-c378-4574-9e46-9338c81305ef",
      name: "Sobota",
      sourceCount: 1,
      createdAt: "2026-08-07T12:00:00Z",
      currentGenerationRevisionId: "0e8b2b21-c378-4574-9e46-9338c81305ef",
      sourceRecipeIds: [ids.scheduled],
      rows: [
        { id: "completed-only", ingredientName: "Rajčata", sectionName: "Zelenina", availableSupply: "0", manualPurchaseTarget: null, target: "1", remaining: "0", unit: "kg", fulfilled: true, notRequired: false, contributions: [] },
        { id: "unneeded-only", ingredientName: "Voda", sectionName: "Nápoje", availableSupply: "1", manualPurchaseTarget: null, target: "0", remaining: "0", unit: "l", fulfilled: false, notRequired: true, contributions: [] },
      ],
      adHocItems: [],
      quantityUnits: [],
      storeSections: [],
    });
    pullOrganization.mockResolvedValue(false);
    const user = userEvent.setup();
    render(
      <EventShopping
        eventId={ids.event}
        onBack={vi.fn()}
        onOpenList={vi.fn()}
        onUnauthenticated={vi.fn()}
        organizationId={ids.organization}
        shoppingListId="9d8b2b21-c378-4574-9e46-9338c81305ef"
        userId={ids.user}
      />,
    );

    await user.click(await screen.findByLabelText("Skrýt nakoupené a nepotřebné položky"));
    expect(screen.getByText("Všechny položky odpovídají aktivnímu filtru.")).toBeVisible();
    expect(screen.queryAllByRole("heading", { level: 4 })).toHaveLength(0);
  });

  it("creates a distinct accessible ad-hoc item through the typed outbox", async () => {
    await i18n.changeLanguage(defaultLocale);
    readEventPlanner.mockResolvedValue({
      name: "Letní vaření",
      lifecycle: "active",
      scheduled: [],
    });
    readShoppingLists.mockResolvedValue([]);
    readShoppingList.mockResolvedValue({
      id: "9d8b2b21-c378-4574-9e46-9338c81305ef",
      name: "Sobota",
      sourceCount: 0,
      createdAt: "2026-08-07T12:00:00Z",
      currentGenerationRevisionId: "0e8b2b21-c378-4574-9e46-9338c81305ef",
      sourceRecipeIds: [],
      rows: [],
      adHocItems: [],
      quantityUnits: [
        { id: "4e8b2b21-c378-4574-9e46-9338c81305ef", name: "kg" },
      ],
      storeSections: [
        { id: "5e8b2b21-c378-4574-9e46-9338c81305ef", name: "Zelenina" },
      ],
    });
    pullOrganization.mockResolvedValue(false);
    const user = userEvent.setup();
    render(
      <EventShopping
        eventId={ids.event}
        onBack={vi.fn()}
        onOpenList={vi.fn()}
        onUnauthenticated={vi.fn()}
        organizationId={ids.organization}
        shoppingListId="9d8b2b21-c378-4574-9e46-9338c81305ef"
        userId={ids.user}
      />,
    );
    await user.type(
      await screen.findByLabelText("Název položky"),
      "  Citrony ",
    );
    await user.type(screen.getByLabelText("Množství"), "3");
    await user.click(screen.getByRole("button", { name: "Přidat položku" }));
    expect(queueAdHocShoppingItem).toHaveBeenCalledWith(
      ids.user,
      ids.organization,
      {
        shoppingListId: "9d8b2b21-c378-4574-9e46-9338c81305ef",
        name: "  Citrony ",
        targetAmount: "3",
        unitId: "4e8b2b21-c378-4574-9e46-9338c81305ef",
        storeSectionId: "5e8b2b21-c378-4574-9e46-9338c81305ef",
        note: "",
      },
    );
  });

  it("marks an ad-hoc item fulfilled through its typed outbox command", async () => {
    await i18n.changeLanguage(defaultLocale);
    readEventPlanner.mockResolvedValue({ name: "Letní vaření", lifecycle: "active", scheduled: [] });
    readShoppingLists.mockResolvedValue([]);
    readShoppingList.mockResolvedValue({
      id: "9d8b2b21-c378-4574-9e46-9338c81305ef",
      name: "Sobota",
      sourceCount: 0,
      createdAt: "2026-08-07T12:00:00Z",
      currentGenerationRevisionId: "0e8b2b21-c378-4574-9e46-9338c81305ef",
      sourceRecipeIds: [], rows: [],
      adHocItems: [{ id: "2e8b2b21-c378-4574-9e46-9338c81305ef", name: "Citrony", target: "3", unit: "kg", sectionName: null, note: null, fulfilled: false, partial: true }],
      quantityUnits: [], storeSections: [],
    });
    pullOrganization.mockResolvedValue(false);
    const user = userEvent.setup();
    render(<EventShopping eventId={ids.event} onBack={vi.fn()} onOpenList={vi.fn()} onUnauthenticated={vi.fn()} organizationId={ids.organization} shoppingListId="9d8b2b21-c378-4574-9e46-9338c81305ef" userId={ids.user} />);
    const checkbox = await screen.findByLabelText("Nakoupeno");
    await waitFor(() => expect(checkbox).toHaveProperty("indeterminate", true));
    expect(checkbox).toHaveAttribute("aria-checked", "mixed");
    await user.click(checkbox);
    expect(queueAdHocShoppingItemFulfilment).toHaveBeenCalledWith(ids.user, ids.organization, {
      shoppingListId: "9d8b2b21-c378-4574-9e46-9338c81305ef",
      adHocShoppingItemId: "2e8b2b21-c378-4574-9e46-9338c81305ef",
      fulfilled: true,
    });
  });

  it("offers an explicit restore action for a retired ad-hoc item", async () => {
    await i18n.changeLanguage(defaultLocale);
    readEventPlanner.mockResolvedValue({ name: "Letní vaření", lifecycle: "active", scheduled: [] });
    readShoppingLists.mockResolvedValue([]);
    readShoppingList.mockResolvedValue({
      id: "9d8b2b21-c378-4574-9e46-9338c81305ef", name: "Sobota", sourceCount: 0,
      createdAt: "2026-08-07T12:00:00Z", currentGenerationRevisionId: "0e8b2b21-c378-4574-9e46-9338c81305ef",
      sourceRecipeIds: [], rows: [],
      adHocItems: [{ id: "2e8b2b21-c378-4574-9e46-9338c81305ef", name: "Citrony", target: "3", unit: "kg", sectionName: null, note: null, fulfilled: false, retired: true }],
      quantityUnits: [], storeSections: [],
    });
    pullOrganization.mockResolvedValue(false);
    const user = userEvent.setup();
    render(<EventShopping eventId={ids.event} onBack={vi.fn()} onOpenList={vi.fn()} onUnauthenticated={vi.fn()} organizationId={ids.organization} shoppingListId="9d8b2b21-c378-4574-9e46-9338c81305ef" userId={ids.user} />);
    await user.click(await screen.findByRole("button", { name: "Obnovit položku" }));
    expect(queueAdHocShoppingItemLifecycle).toHaveBeenCalledWith(ids.user, ids.organization, {
      shoppingListId: "9d8b2b21-c378-4574-9e46-9338c81305ef",
      adHocShoppingItemId: "2e8b2b21-c378-4574-9e46-9338c81305ef",
      operation: "restore",
    });
  });

  it("edits an active ad-hoc item through the typed outbox", async () => {
    await i18n.changeLanguage(defaultLocale);
    readEventPlanner.mockResolvedValue({ name: "Letní vaření", lifecycle: "active", scheduled: [] });
    readShoppingLists.mockResolvedValue([]);
    readShoppingList.mockResolvedValue({
      id: "9d8b2b21-c378-4574-9e46-9338c81305ef", name: "Sobota", sourceCount: 0,
      createdAt: "2026-08-07T12:00:00Z", currentGenerationRevisionId: "0e8b2b21-c378-4574-9e46-9338c81305ef",
      sourceRecipeIds: [], rows: [],
      adHocItems: [{ id: "2e8b2b21-c378-4574-9e46-9338c81305ef", name: "Citrony", target: "3", unitId: "4e8b2b21-c378-4574-9e46-9338c81305ef", unit: "kg", sectionId: "5e8b2b21-c378-4574-9e46-9338c81305ef", sectionName: null, note: null, fulfilled: false, retired: false }],
      quantityUnits: [{ id: "4e8b2b21-c378-4574-9e46-9338c81305ef", name: "kg" }], storeSections: [{ id: "5e8b2b21-c378-4574-9e46-9338c81305ef", name: "Zelenina" }],
    });
    pullOrganization.mockResolvedValue(false);
    const user = userEvent.setup();
    render(<EventShopping eventId={ids.event} onBack={vi.fn()} onOpenList={vi.fn()} onUnauthenticated={vi.fn()} organizationId={ids.organization} shoppingListId="9d8b2b21-c378-4574-9e46-9338c81305ef" userId={ids.user} />);
    await user.click(await screen.findByRole("button", { name: "Upravit položku" }));
    const name = screen.getAllByLabelText("Název položky").at(-1);
    if (!name) throw new Error("missing edit name input");
    await user.clear(name);
    await user.type(name, "Limety");
    await user.click(screen.getByRole("button", { name: "Uložit změny" }));
    expect(queueAdHocShoppingItemUpdate).toHaveBeenCalledWith(ids.user, ids.organization, expect.objectContaining({ adHocShoppingItemId: "2e8b2b21-c378-4574-9e46-9338c81305ef", name: "Limety" }));
  });

  it("queues a selected-source refresh without locally changing the list revision", async () => {
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
    readShoppingList.mockResolvedValue({
      id: "9d8b2b21-c378-4574-9e46-9338c81305ef",
      name: "Sobota",
      sourceCount: 1,
      createdAt: "2026-08-07T12:00:00Z",
      currentGenerationRevisionId: "0e8b2b21-c378-4574-9e46-9338c81305ef",
      sourceRecipeIds: [ids.scheduled],
      rows: [],
      adHocItems: [],
      quantityUnits: [],
      storeSections: [],
    });
    pullOrganization.mockResolvedValue(false);
    const user = userEvent.setup();
    render(
      <EventShopping
        eventId={ids.event}
        onBack={vi.fn()}
        onOpenList={vi.fn()}
        onUnauthenticated={vi.fn()}
        organizationId={ids.organization}
        shoppingListId="9d8b2b21-c378-4574-9e46-9338c81305ef"
        userId={ids.user}
      />,
    );
    await user.click(
      await screen.findByRole("button", { name: "Obnovit vypočtené položky" }),
    );
    await user.click(
      screen.getByRole("button", { name: "Obnovit vypočtené položky" }),
    );
    expect(queueShoppingListRefresh).toHaveBeenCalledWith(
      ids.user,
      ids.organization,
      {
        shoppingListId: "9d8b2b21-c378-4574-9e46-9338c81305ef",
        parentGenerationRevisionId: "0e8b2b21-c378-4574-9e46-9338c81305ef",
        scheduledRecipeIds: [ids.scheduled],
      },
    );
  });
});
