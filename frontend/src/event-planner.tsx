import { liveQuery } from "dexie";
import { useCallback, useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";

import {
  readEventPlanner,
  type EventPlannerProjection,
} from "./planner-projections";
import {
  queueRecipeSchedule,
  queueScheduledRecipeMove,
} from "./scheduled-recipe";
import { pullOrganization, SyncRequestError } from "./sync-bootstrap";

type PlannerState = "loading" | "ready" | "offline" | "error";

function EventSummary({ planner }: { planner: EventPlannerProjection }) {
  const { t } = useTranslation();
  return (
    <header className="event-workspace__summary">
      <div>
        <h2>{planner.name}</h2>
        <p>
          {t("planner.dateRange", {
            start: planner.startDate,
            end: planner.endDate,
          })}
        </p>
      </div>
      <dl>
        <div>
          <dt>{t("planner.attendance")}</dt>
          <dd>{planner.attendance}</dd>
        </div>
        <div>
          <dt>{t("planner.lifecycle")}</dt>
          <dd>{t(`eventsOverview.lifecycle.${planner.lifecycle}`)}</dd>
        </div>
      </dl>
    </header>
  );
}

function AddRecipe({
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
  const [dayId, setDayId] = useState("");
  const [roleId, setRoleId] = useState("");
  const [recipeId, setRecipeId] = useState("");
  const [error, setError] = useState<string>();
  const [saved, setSaved] = useState(false);
  const inFlight = useRef(false);

  useEffect(() => {
    setDayId((current) =>
      planner.days.some((item) => item.id === current)
        ? current
        : (planner.days[0]?.id ?? ""),
    );
    setRoleId((current) =>
      planner.roles.some((item) => item.id === current)
        ? current
        : (planner.roles[0]?.id ?? ""),
    );
    setRecipeId((current) =>
      planner.recipes.some((item) => item.id === current)
        ? current
        : (planner.recipes[0]?.id ?? ""),
    );
  }, [planner.days, planner.recipes, planner.roles]);

  async function submit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (inFlight.current) return;
    inFlight.current = true;
    try {
      await queueRecipeSchedule(userId, organizationId, {
        eventId,
        eventDayId: dayId,
        eventMealRoleId: roleId,
        recipeId,
      });
      setSaved(true);
      setError(undefined);
    } catch {
      setSaved(false);
      setError("unavailable");
    } finally {
      inFlight.current = false;
    }
  }

  if (planner.lifecycle !== "active") return null;
  if (!planner.days.length || !planner.roles.length || !planner.recipes.length)
    return <p role="status">{t("planner.noAddOptions")}</p>;
  return (
    <form className="planner-add" onSubmit={(event) => void submit(event)}>
      <h3>{t("planner.addHeading")}</h3>
      <label>
        {t("planner.day")}
        <select
          onChange={(event) => setDayId(event.target.value)}
          value={dayId}
        >
          {planner.days.map((day) => (
            <option key={day.id} value={day.id}>
              {day.date}
            </option>
          ))}
        </select>
      </label>
      <label>
        {t("planner.role")}
        <select
          onChange={(event) => setRoleId(event.target.value)}
          value={roleId}
        >
          {planner.roles.map((role) => (
            <option key={role.id} value={role.id}>
              {role.name}
            </option>
          ))}
        </select>
      </label>
      <label>
        {t("planner.recipe")}
        <select
          onChange={(event) => setRecipeId(event.target.value)}
          value={recipeId}
        >
          {planner.recipes.map((recipe) => (
            <option key={recipe.id} value={recipe.id}>
              {recipe.name}
            </option>
          ))}
        </select>
      </label>
      <button type="submit">{t("planner.add")}</button>
      {error ? <p role="alert">{t(`planner.errors.${error}`)}</p> : null}
      {saved ? <p role="status">{t("planner.saved")}</p> : null}
    </form>
  );
}

function MoveRecipe({
  planner,
  eventId,
  organizationId,
  userId,
  scheduled,
}: {
  planner: EventPlannerProjection;
  eventId: string;
  organizationId: string;
  userId: string;
  scheduled: EventPlannerProjection["scheduled"][number];
}) {
  const { t } = useTranslation();
  const [dayId, setDayId] = useState(scheduled.dayId);
  const [roleId, setRoleId] = useState(scheduled.roleId);
  const [positionKey, setPositionKey] = useState(scheduled.position);
  const [error, setError] = useState(false);
  const inFlight = useRef(false);

  if (planner.lifecycle !== "active") return null;

  async function submit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (inFlight.current) return;
    inFlight.current = true;
    try {
      await queueScheduledRecipeMove(userId, organizationId, {
        scheduledRecipeId: scheduled.id,
        eventId,
        eventDayId: dayId,
        eventMealRoleId: roleId,
        positionKey,
      });
      setError(false);
    } catch {
      setError(true);
    } finally {
      inFlight.current = false;
    }
  }

  return (
    <details>
      <summary>{t("planner.move")}</summary>
      <form onSubmit={(event) => void submit(event)}>
        <label>
          {t("planner.day")}
          <select
            onChange={(event) => setDayId(event.target.value)}
            value={dayId}
          >
            {planner.days.map((day) => (
              <option key={day.id} value={day.id}>
                {day.date}
              </option>
            ))}
          </select>
        </label>
        <label>
          {t("planner.role")}
          <select
            onChange={(event) => setRoleId(event.target.value)}
            value={roleId}
          >
            {planner.roles.map((role) => (
              <option key={role.id} value={role.id}>
                {role.name}
              </option>
            ))}
          </select>
        </label>
        <label>
          {t("planner.position")}
          <input
            maxLength={255}
            onChange={(event) => setPositionKey(event.target.value)}
            pattern="[0-9A-Za-z]+"
            required
            value={positionKey}
          />
        </label>
        <button type="submit">{t("planner.moveTo")}</button>
        {error ? <p role="alert">{t("planner.errors.unavailable")}</p> : null}
      </form>
    </details>
  );
}

export function EventPlanner({
  eventId,
  organizationId,
  userId,
  onUnauthenticated,
  onOpenShopping,
}: {
  eventId: string;
  organizationId: string;
  userId: string;
  onUnauthenticated: () => void;
  onOpenShopping?: () => void;
}) {
  const { t } = useTranslation();
  const [state, setState] = useState<PlannerState>("loading");
  const [planner, setPlanner] = useState<EventPlannerProjection>();
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
    const subscription = liveQuery(() =>
      readEventPlanner(userId, organizationId, eventId),
    ).subscribe({
      next: (next) => active && setPlanner(next),
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
  }, [eventId, organizationId, synchronize, userId]);
  if (!planner && state === "loading")
    return <p role="status">{t("planner.loading")}</p>;
  if (!planner)
    return (
      <div role="alert">
        <p>{t("planner.unavailable")}</p>
        <button onClick={() => void synchronize()} type="button">
          {t("eventsOverview.retry")}
        </button>
      </div>
    );
  return (
    <section className="event-workspace" aria-labelledby="planner-heading">
      <EventSummary planner={planner} />
      {planner.lifecycle === "archived" ? (
        <p className="planner-archived" role="status">
          {t("planner.archived")}
        </p>
      ) : null}
      {state === "offline" ? <p role="status">{t("planner.offline")}</p> : null}
      <h2 id="planner-heading">{t("planner.heading")}</h2>
      {onOpenShopping ? (
        <button onClick={onOpenShopping} type="button">
          {t("planner.shopping")}
        </button>
      ) : null}
      <AddRecipe
        eventId={eventId}
        organizationId={organizationId}
        planner={planner}
        userId={userId}
      />
      <div className="planner-days">
        {planner.days.map((day) => (
          <section
            className="planner-day"
            key={day.id}
            aria-labelledby={`day-${day.id}`}
          >
            <h3 id={`day-${day.id}`}>{day.date}</h3>
            {day.note ? <p>{day.note}</p> : null}
            {planner.roles.map((role) => {
              const scheduled = planner.scheduled.filter(
                (item) => item.dayId === day.id && item.roleId === role.id,
              );
              return (
                <section
                  className="planner-role"
                  key={role.id}
                  aria-labelledby={`role-${day.id}-${role.id}`}
                >
                  <h4 id={`role-${day.id}-${role.id}`}>{role.name}</h4>
                  {scheduled.length ? (
                    <ul>
                      {scheduled.map((item) => (
                        <li key={item.id}>
                          {item.name} ·{" "}
                          {t("planner.diners", { count: item.dinerCount })}
                          <MoveRecipe
                            eventId={eventId}
                            organizationId={organizationId}
                            planner={planner}
                            scheduled={item}
                            userId={userId}
                          />
                        </li>
                      ))}
                    </ul>
                  ) : (
                    <p>{t("planner.emptyRole")}</p>
                  )}
                </section>
              );
            })}
          </section>
        ))}
      </div>
      {state === "error" ? (
        <div role="alert">
          <p>{t("planner.error")}</p>
          <button onClick={() => void synchronize()} type="button">
            {t("eventsOverview.retry")}
          </button>
        </div>
      ) : null}
    </section>
  );
}
