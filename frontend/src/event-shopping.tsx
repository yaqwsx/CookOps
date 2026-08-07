import { liveQuery } from "dexie";
import { useCallback, useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";

import {
  readEventPlanner,
  type EventPlannerProjection,
} from "./planner-projections";
import { queueShoppingList } from "./shopping-list";
import {
  readShoppingList,
  readShoppingLists,
  type ShoppingListProjection,
  type ShoppingListSummary,
} from "./shopping-projections";
import { pullOrganization, SyncRequestError } from "./sync-bootstrap";
import { SynchronizationStatus } from "./synchronization-status";

type ShoppingState = "loading" | "ready" | "offline" | "error";

function ShoppingCreate({
  planner,
  eventId,
  organizationId,
  userId,
}: {
  planner: EventPlannerProjection;
  eventId: string;
  organizationId: string;
  userId: string;
}) {
  const { t } = useTranslation();
  const [name, setName] = useState("");
  const [selected, setSelected] = useState<string[]>([]);
  const [error, setError] = useState<string>();
  const [saved, setSaved] = useState(false);
  const inFlight = useRef(false);

  function toggle(id: string, checked: boolean) {
    setSelected((current) =>
      checked
        ? [...new Set([...current, id])]
        : current.filter((item) => item !== id),
    );
  }

  async function submit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (inFlight.current) return;
    inFlight.current = true;
    try {
      await queueShoppingList(userId, organizationId, {
        eventId,
        name,
        scheduledRecipeIds: selected,
      });
      setName("");
      setSelected([]);
      setError(undefined);
      setSaved(true);
    } catch {
      setSaved(false);
      setError("unavailable");
    } finally {
      inFlight.current = false;
    }
  }

  if (planner.lifecycle !== "active") return null;
  return (
    <form className="shopping-create" onSubmit={(event) => void submit(event)}>
      <h3>{t("shopping.createHeading")}</h3>
      <label>
        {t("shopping.name")}
        <input
          maxLength={200}
          onChange={(event) => setName(event.target.value)}
          required
          value={name}
        />
      </label>
      <fieldset>
        <legend>{t("shopping.sources")}</legend>
        {planner.scheduled.length ? (
          planner.scheduled.map((recipe) => (
            <label className="shopping-create__source" key={recipe.id}>
              <input
                checked={selected.includes(recipe.id)}
                onChange={(event) => toggle(recipe.id, event.target.checked)}
                type="checkbox"
              />
              <span>
                {recipe.name} ·{" "}
                {t("planner.diners", { count: recipe.dinerCount })}
              </span>
            </label>
          ))
        ) : (
          <p>{t("shopping.noSources")}</p>
        )}
      </fieldset>
      <button type="submit">{t("shopping.create")}</button>
      {error ? <p role="alert">{t(`shopping.errors.${error}`)}</p> : null}
      {saved ? <p role="status">{t("shopping.saved")}</p> : null}
    </form>
  );
}

function ShoppingIndex({
  lists,
  onOpenList,
}: {
  lists: ShoppingListSummary[];
  onOpenList: (shoppingListId: string) => void;
}) {
  const { t } = useTranslation();
  if (!lists.length) return <p>{t("shopping.empty")}</p>;
  return (
    <ul className="shopping-list-index">
      {lists.map((list) => (
        <li key={list.id}>
          <div>
            <h3>{list.name}</h3>
            <p>{t("shopping.sourceCount", { count: list.sourceCount })}</p>
          </div>
          <button onClick={() => onOpenList(list.id)} type="button">
            {t("shopping.open")}
          </button>
        </li>
      ))}
    </ul>
  );
}

function ShoppingDetail({
  shoppingList,
  onBack,
}: {
  shoppingList: ShoppingListProjection;
  onBack: () => void;
}) {
  const { t } = useTranslation();
  const sections = new Map<string, ShoppingListProjection["rows"]>();
  for (const row of shoppingList.rows) {
    const section = row.sectionName ?? t("shopping.noSection");
    sections.set(section, [...(sections.get(section) ?? []), row]);
  }
  return (
    <section
      className="shopping-detail"
      aria-labelledby="shopping-detail-heading"
    >
      <button className="shopping-detail__back" onClick={onBack} type="button">
        {t("shopping.back")}
      </button>
      <h3 id="shopping-detail-heading">{shoppingList.name}</h3>
      <p>{t("shopping.readOnly")}</p>
      {shoppingList.rows.length ? (
        [...sections].map(([section, rows]) => (
          <section className="shopping-section" key={section}>
            <h4>{section}</h4>
            <ul className="shopping-rows">
              {rows.map((row) => (
                <li key={row.id}>
                  <h5>{row.ingredientName}</h5>
                  <dl>
                    <div>
                      <dt>{t("shopping.remaining")}</dt>
                      <dd>
                        {t("shopping.quantity", {
                          amount: row.remaining,
                          unit: row.unit,
                        })}
                      </dd>
                    </div>
                  </dl>
                </li>
              ))}
            </ul>
          </section>
        ))
      ) : (
        <p>{t("shopping.noRows")}</p>
      )}
    </section>
  );
}

export function EventShopping({
  eventId,
  organizationId,
  shoppingListId,
  userId,
  onOpenList,
  onOpenPlanner,
  onBack,
  onUnauthenticated,
}: {
  eventId: string;
  organizationId: string;
  shoppingListId?: string;
  userId: string;
  onOpenList: (shoppingListId: string) => void;
  onOpenPlanner: () => void;
  onBack: () => void;
  onUnauthenticated: () => void;
}) {
  const { t } = useTranslation();
  const [state, setState] = useState<ShoppingState>("loading");
  const [planner, setPlanner] = useState<EventPlannerProjection>();
  const [lists, setLists] = useState<ShoppingListSummary[]>([]);
  const [shoppingList, setShoppingList] = useState<ShoppingListProjection>();
  const generation = useRef(0);
  const synchronize = useCallback(async () => {
    const current = generation.current;
    if (!navigator.onLine) return setState("offline");
    try {
      await pullOrganization(userId, organizationId);
      if (current === generation.current) setState("ready");
    } catch (error) {
      if (error instanceof SyncRequestError && error.status === 401)
        return onUnauthenticated();
      if (current === generation.current) setState("error");
    }
  }, [onUnauthenticated, organizationId, userId]);
  useEffect(() => {
    let active = true;
    generation.current += 1;
    const subscription = liveQuery(async () => ({
      planner: await readEventPlanner(userId, organizationId, eventId),
      lists: await readShoppingLists(userId, organizationId, eventId),
      shoppingList: shoppingListId
        ? await readShoppingList(
            userId,
            organizationId,
            eventId,
            shoppingListId,
          )
        : undefined,
    })).subscribe({
      next: (next) => {
        if (!active) return;
        setPlanner(next.planner);
        setLists(next.lists);
        setShoppingList(next.shoppingList);
      },
      error: () => active && setState("error"),
    });
    const offline = () => setState("offline");
    window.addEventListener("online", synchronize);
    window.addEventListener("offline", offline);
    void synchronize();
    return () => {
      active = false;
      generation.current += 1;
      subscription.unsubscribe();
      window.removeEventListener("online", synchronize);
      window.removeEventListener("offline", offline);
    };
  }, [eventId, organizationId, shoppingListId, synchronize, userId]);
  if (!planner && state === "loading")
    return <p role="status">{t("shopping.loading")}</p>;
  if (!planner)
    return (
      <div role="alert">
        <p>{t("shopping.unavailable")}</p>
        <button onClick={() => void synchronize()} type="button">
          {t("eventsOverview.retry")}
        </button>
      </div>
    );
  return (
    <section className="event-shopping" aria-labelledby="shopping-heading">
      <header className="event-workspace__summary">
        <div>
          <h2 id="shopping-heading">{t("shopping.heading")}</h2>
          <p>{planner.name}</p>
        </div>
        <SynchronizationStatus
          organizationId={organizationId}
          userId={userId}
        />
      </header>
      <button onClick={onOpenPlanner} type="button">
        {t("shopping.planner")}
      </button>
      {planner.lifecycle === "archived" ? (
        <p className="planner-archived" role="status">
          {t("shopping.archived")}
        </p>
      ) : null}
      {state === "offline" ? (
        <p role="status">{t("shopping.offline")}</p>
      ) : null}
      {shoppingListId ? (
        shoppingList ? (
          <ShoppingDetail onBack={onBack} shoppingList={shoppingList} />
        ) : (
          <div role="alert">
            <p>{t("shopping.listUnavailable")}</p>
            <button onClick={onBack} type="button">
              {t("shopping.back")}
            </button>
          </div>
        )
      ) : (
        <>
          <ShoppingCreate
            eventId={eventId}
            organizationId={organizationId}
            planner={planner}
            userId={userId}
          />
          <ShoppingIndex lists={lists} onOpenList={onOpenList} />
        </>
      )}
      {state === "error" ? (
        <div role="alert">
          <p>{t("shopping.error")}</p>
          <button onClick={() => void synchronize()} type="button">
            {t("eventsOverview.retry")}
          </button>
        </div>
      ) : null}
    </section>
  );
}
