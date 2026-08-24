import {
  createRootRoute,
  createRoute,
  createRouter,
  Link,
  notFound,
  Outlet,
  useNavigate,
  useParams,
  useRouter,
  useSearch,
} from "@tanstack/react-router";
import { useContext } from "react";
import { useTranslation } from "react-i18next";

import { CatalogAdministration } from "./catalog-administration";
import { EventCostsPage } from "./event-costs-page";
import { EventPlanner } from "./event-planner";
import { EventReceipts } from "./event-receipts";
import { EventSettingsPage } from "./event-settings-page";
import { EventShopping } from "./event-shopping";
import { EventOverview } from "./events-overview";
import { IngredientCatalog } from "./ingredient-catalog-view";
import { McpGrantsPage } from "./mcp-grants-page";
import { OrganizationMemberships } from "./organization-membership";
import { OrganizationMetadataSettings } from "./organization-metadata-settings";
import { RecipeCatalog } from "./recipe-catalog-view";
import { App, RouteShellContext } from "./App";
import { SystemOrganizationCreate } from "./system-organization-create";

const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

function validateUuid({ params }: { params: Record<string, string> }) {
  if (
    Object.entries(params).some(
      ([key, value]) => key.endsWith("Id") && !uuid.test(value),
    )
  )
    throw notFound();
}

function NotFound() {
  const { t } = useTranslation();
  return (
    <main id="main">
      <p role="alert">{t("routing.notFound")}</p>
      <Link to="/">{t("routing.home")}</Link>
    </main>
  );
}

function GlobalAccessGate({ children }: { children: React.ReactNode }) {
  const { t } = useTranslation();
  const shell = useContext(RouteShellContext);
  return shell.organizations.status === "offlineBlocked" ? (
    <p role="alert">{t("shell.authorizationRequiredOffline")}</p>
  ) : (
    children
  );
}

function OrganizationGate({ children }: { children: React.ReactNode }) {
  const { t } = useTranslation();
  const shell = useContext(RouteShellContext);
  if (shell.routeAccess === "loading")
    return <p role="status">{t("shell.organizationsLoading")}</p>;
  if (shell.routeAccess === "blocked")
    return <p role="alert">{t("shell.authorizationRequiredOffline")}</p>;
  if (shell.routeAccess === "denied")
    return <p role="alert">{t("shell.organizationUnavailable")}</p>;
  return <>{children}</>;
}

function useOrganization() {
  const { organizationId } = useParams({ strict: false }) as {
    organizationId: string;
  };
  const shell = useContext(RouteShellContext);
  return { ...shell, organizationId };
}

function EventsRoute() {
  const { organizationId, identity, onUnauthenticated } = useOrganization();
  const navigate = useNavigate();
  return (
    <OrganizationGate>
      <EventOverview
        onOpen={(eventId) =>
          navigate({
            to: "/organizations/$organizationId/events/$eventId/planner",
            params: { organizationId, eventId },
          })
        }
        onOpenIngredients={() =>
          navigate({
            to: "/organizations/$organizationId/ingredients",
            params: { organizationId },
          })
        }
        onOpenRecipes={() =>
          navigate({
            to: "/organizations/$organizationId/recipes",
            params: { organizationId },
          })
        }
        onUnauthenticated={onUnauthenticated}
        organizationId={organizationId}
        userId={identity.id}
      />
    </OrganizationGate>
  );
}

function EventPlannerRoute() {
  const { organizationId, identity, onUnauthenticated } = useOrganization();
  const { eventId } = useParams({ strict: false }) as { eventId: string };
  const navigate = useNavigate();
  return (
    <OrganizationGate>
      <EventPlanner
        eventId={eventId}
        onOpenRecipe={(recipeId, recipeVersionId) =>
          navigate({
            to: "/organizations/$organizationId/recipes/$recipeId/edit",
            params: { organizationId, recipeId },
            search: { version: recipeVersionId, from: "planner" },
          })
        }
        onUnauthenticated={onUnauthenticated}
        organizationId={organizationId}
        onShoppingListCreated={(shoppingListId) =>
          navigate({
            to: "/organizations/$organizationId/events/$eventId/shopping/$shoppingListId",
            params: { organizationId, eventId, shoppingListId },
          })
        }
        userId={identity.id}
      />
    </OrganizationGate>
  );
}
function EventShoppingRoute() {
  const { organizationId, identity, onUnauthenticated } = useOrganization();
  const { eventId, shoppingListId } = useParams({ strict: false }) as {
    eventId: string;
    shoppingListId?: string;
  };
  const navigate = useNavigate();
  const base = { organizationId, eventId };
  return (
    <OrganizationGate>
      <EventShopping
        eventId={eventId}
        onBack={() =>
          navigate({
            to: "/organizations/$organizationId/events/$eventId/shopping",
            params: base,
          })
        }
        onOpenList={(id) =>
          navigate({
            to: "/organizations/$organizationId/events/$eventId/shopping/$shoppingListId",
            params: { ...base, shoppingListId: id },
          })
        }
        onUnauthenticated={onUnauthenticated}
        organizationId={organizationId}
        shoppingListId={shoppingListId}
        userId={identity.id}
      />
    </OrganizationGate>
  );
}
function EventCostsRoute() {
  const { organizationId, identity, onUnauthenticated } = useOrganization();
  const { eventId } = useParams({ strict: false }) as { eventId: string };
  const navigate = useNavigate();
  const params = { organizationId, eventId };
  return (
    <OrganizationGate>
      <EventCostsPage
        eventId={eventId}
        onUnauthenticated={onUnauthenticated}
        onBack={() =>
          navigate({
            to: "/organizations/$organizationId/events/$eventId/planner",
            params,
          })
        }
        onOpenReceipts={() =>
          navigate({
            to: "/organizations/$organizationId/events/$eventId/receipts",
            params,
          })
        }
        organizationId={organizationId}
        userId={identity.id}
      />
    </OrganizationGate>
  );
}
function EventReceiptsRoute() {
  const { organizationId, identity, onUnauthenticated } = useOrganization();
  const { eventId } = useParams({ strict: false }) as { eventId: string };
  const navigate = useNavigate();
  return (
    <OrganizationGate>
      <EventReceipts
        eventId={eventId}
        onBack={() =>
          navigate({
            to: "/organizations/$organizationId/events/$eventId/planner",
            params: { organizationId, eventId },
          })
        }
        onUnauthenticated={onUnauthenticated}
        organizationId={organizationId}
        userId={identity.id}
      />
    </OrganizationGate>
  );
}
function EventSettingsRoute() {
  const { organizationId, identity, onUnauthenticated } = useOrganization();
  const { eventId } = useParams({ strict: false }) as { eventId: string };
  const navigate = useNavigate();
  return (
    <OrganizationGate>
      <EventSettingsPage
        eventId={eventId}
        onUnauthenticated={onUnauthenticated}
        onOpenPlanner={() =>
          navigate({
            to: "/organizations/$organizationId/events/$eventId/planner",
            params: { organizationId, eventId },
          })
        }
        onOpenCosts={() =>
          navigate({
            to: "/organizations/$organizationId/events/$eventId/costs",
            params: { organizationId, eventId },
          })
        }
        organizationId={organizationId}
        userId={identity.id}
      />
    </OrganizationGate>
  );
}

function RecipesRoute({ edit = false }: { edit?: boolean }) {
  const {
    organizationId,
    identity,
    onUnauthenticated,
    discardToken,
    reportRecipeDirty,
  } = useOrganization();
  const { recipeId } = useParams({ strict: false }) as { recipeId?: string };
  const search = useSearch({ strict: false }) as {
    version?: string;
    from?: "planner";
  };
  const navigate = useNavigate();
  const router = useRouter();
  const pinnedVersionId = edit ? search.version : undefined;
  return (
    <OrganizationGate>
      <RecipeCatalog
        editRecipeId={edit ? recipeId : undefined}
        onBackToCatalog={
          recipeId
            ? () =>
                search.from === "planner"
                  ? router.history.back()
                  : navigate({
                      to: "/organizations/$organizationId/recipes",
                      params: { organizationId },
                    })
            : undefined
        }
        onDirtyChange={reportRecipeDirty}
        onUnauthenticated={onUnauthenticated}
        organizationId={organizationId}
        selectedRecipeId={recipeId}
        pinnedVersionId={pinnedVersionId}
        discardToken={discardToken}
        userId={identity.id}
      />
    </OrganizationGate>
  );
}
function IngredientsRoute() {
  const {
    organizationId,
    identity,
    onUnauthenticated,
    discardToken,
    reportIngredientDirty,
  } = useOrganization();
  const { ingredientId } = useParams({ strict: false }) as {
    ingredientId?: string;
  };
  const navigate = useNavigate();
  return (
    <OrganizationGate>
      <IngredientCatalog
        onBackToCatalog={
          ingredientId
            ? () =>
                navigate({
                  to: "/organizations/$organizationId/ingredients",
                  params: { organizationId },
                })
            : undefined
        }
        onDirtyChange={reportIngredientDirty}
        onOpenIngredient={(id) =>
          navigate({
            to: "/organizations/$organizationId/ingredients/$ingredientId",
            params: { organizationId, ingredientId: id },
          })
        }
        discardToken={discardToken}
        onUnauthenticated={onUnauthenticated}
        organizationId={organizationId}
        selectedIngredientId={ingredientId}
        userId={identity.id}
      />
    </OrganizationGate>
  );
}
function OrganizationSettingsRoute() {
  const { i18n } = useTranslation();
  const { organizationId, identity, onUnauthenticated, systemAdmin } =
    useOrganization();
  return (
    <OrganizationGate>
      <OrganizationMetadataSettings
        organizationId={organizationId}
        userId={identity.id}
      />
      <CatalogAdministration
        locale={(i18n.resolvedLanguage ?? "cs") === "en" ? "en" : "cs"}
        organizationId={organizationId}
        userId={identity.id}
      />
      <OrganizationMemberships
        onUnauthenticated={onUnauthenticated}
        organizationId={organizationId}
        userId={identity.id}
        systemAdmin={systemAdmin}
      />
    </OrganizationGate>
  );
}
function SystemOrganizationsRoute() {
  const { t } = useTranslation();
  const { identity, onUnauthenticated, systemAdmin, refreshOrganizations } =
    useContext(RouteShellContext);
  return (
    <GlobalAccessGate>
      {systemAdmin ? (
        <SystemOrganizationCreate
          onCreated={refreshOrganizations}
          onUnauthenticated={onUnauthenticated}
          userId={identity.id}
        />
      ) : (
        <p role="alert">{t("systemOrganizations.routeForbidden")}</p>
      )}
    </GlobalAccessGate>
  );
}
function McpRoute() {
  const { onUnauthenticated } = useContext(RouteShellContext);
  return (
    <GlobalAccessGate>
      <McpGrantsPage onUnauthenticated={onUnauthenticated} />
    </GlobalAccessGate>
  );
}

const rootRoute = createRootRoute({
  component: App,
  notFoundComponent: NotFound,
});
const authMcpRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "auth/mcp-grants",
  component: McpRoute,
});
const systemRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "system/organizations",
  component: SystemOrganizationsRoute,
});
const orgRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "organizations/$organizationId",
  beforeLoad: validateUuid,
  component: Outlet,
});
const eventsRoute = createRoute({
  getParentRoute: () => orgRoute,
  path: "events",
  component: EventsRoute,
});
const eventRoute = createRoute({
  getParentRoute: () => orgRoute,
  path: "events/$eventId",
  beforeLoad: validateUuid,
  component: Outlet,
});
const plannerRoute = createRoute({
  getParentRoute: () => eventRoute,
  path: "planner",
  component: EventPlannerRoute,
});
const shoppingRoute = createRoute({
  getParentRoute: () => eventRoute,
  path: "shopping",
  component: EventShoppingRoute,
});
const shoppingListRoute = createRoute({
  getParentRoute: () => eventRoute,
  path: "shopping/$shoppingListId",
  beforeLoad: validateUuid,
  component: EventShoppingRoute,
});
const costsRoute = createRoute({
  getParentRoute: () => eventRoute,
  path: "costs",
  component: EventCostsRoute,
});
const receiptsRoute = createRoute({
  getParentRoute: () => eventRoute,
  path: "receipts",
  component: EventReceiptsRoute,
});
const eventSettingsRoute = createRoute({
  getParentRoute: () => eventRoute,
  path: "settings",
  component: EventSettingsRoute,
});
const recipesRoute = createRoute({
  getParentRoute: () => orgRoute,
  path: "recipes",
  component: () => <RecipesRoute />,
});
const recipeRoute = createRoute({
  getParentRoute: () => orgRoute,
  path: "recipes/$recipeId",
  beforeLoad: validateUuid,
  component: () => <RecipesRoute />,
});
const recipeEditRoute = createRoute({
  getParentRoute: () => orgRoute,
  path: "recipes/$recipeId/edit",
  beforeLoad: validateUuid,
  validateSearch: (search: Record<string, unknown>) => {
    const version = search.version;
    const from = search.from;
    if (version === undefined) {
      if (from !== undefined) throw notFound();
      return {};
    }
    if (
      typeof version !== "string" ||
      !uuid.test(version) ||
      (from !== undefined && from !== "planner")
    )
      throw notFound();
    return {
      version,
      ...(from === "planner" ? { from: "planner" as const } : {}),
    };
  },
  component: () => <RecipesRoute edit />,
});
const ingredientsRoute = createRoute({
  getParentRoute: () => orgRoute,
  path: "ingredients",
  component: IngredientsRoute,
});
const ingredientRoute = createRoute({
  getParentRoute: () => orgRoute,
  path: "ingredients/$ingredientId",
  beforeLoad: validateUuid,
  component: IngredientsRoute,
});
const settingsRoute = createRoute({
  getParentRoute: () => orgRoute,
  path: "settings",
  component: OrganizationSettingsRoute,
});

const routeTree = rootRoute.addChildren([
  authMcpRoute,
  systemRoute,
  orgRoute.addChildren([
    eventsRoute,
    eventRoute.addChildren([
      plannerRoute,
      shoppingRoute,
      shoppingListRoute,
      costsRoute,
      receiptsRoute,
      eventSettingsRoute,
    ]),
    recipesRoute,
    recipeRoute,
    recipeEditRoute,
    ingredientsRoute,
    ingredientRoute,
    settingsRoute,
  ]),
]);
export function createAppRouter() {
  return createRouter({ routeTree, defaultNotFoundComponent: NotFound });
}
export const router = createAppRouter();
declare module "@tanstack/react-router" {
  interface Register {
    router: typeof router;
  }
}
