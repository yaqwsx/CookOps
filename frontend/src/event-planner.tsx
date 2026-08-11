import { liveQuery } from "dexie";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useTranslation } from "react-i18next";

import {
  readEventPlanner,
  type EventPlannerProjection,
} from "./planner-projections";
import {
  readEventCosts,
  type EventCostsProjection,
} from "./event-cost-projections";
import {
  eventPriceRefreshPending,
  queueEventPriceRefresh,
} from "./event-price-refresh";
import {
  queueRecipeSchedule,
  queueScheduledRecipeAttendance,
  queueScheduledRecipeContext,
  queueScheduledRecipeLifecycle,
  queueScheduledRecipeMove,
} from "./scheduled-recipe";
import {
  queueAddedOverride,
  queueReplacementOverride,
} from "./scheduled-ingredient-override";
import { queueEventDayCreate, queueEventDayLifecycle, queueEventDayNote, queueEventDayVisibility } from "./event-day";
import { queueEventMealRoleCreate, queueEventMealRoleLifecycle, queueEventMealRoleName, queueEventMealRolePosition } from "./event-meal-role";
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

function EventCosts({
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
  const [costs, setCosts] = useState<EventCostsProjection>();
  const [pending, setPending] = useState(false);
  const [error, setError] = useState(false);
  const refreshing = useRef(false);
  useEffect(() => {
    const subscription = liveQuery(async () => ({
      costs: await readEventCosts(userId, organizationId, eventId),
      pending: await eventPriceRefreshPending(userId, organizationId, eventId),
    })).subscribe({
      next: (next) => {
        setCosts(next.costs);
        setPending(next.pending);
      },
      error: () => setError(true),
    });
    return () => subscription.unsubscribe();
  }, [eventId, organizationId, userId]);
  async function refresh() {
    if (refreshing.current) return;
    refreshing.current = true;
    try {
      await queueEventPriceRefresh(userId, organizationId, eventId);
      setError(false);
    } catch {
      setError(true);
    } finally {
      refreshing.current = false;
    }
  }
  if (!costs) return null;
  return (
    <section className="event-costs" aria-labelledby="event-costs-heading">
      <h2 id="event-costs-heading">{t("costs.heading")}</h2>
      <dl>
        <div>
          <dt>{t("costs.budget")}</dt>
          <dd>
            {t("costs.amount", {
              amount: costs.budget,
              currency: costs.currency,
            })}
          </dd>
        </div>
        <div>
          <dt>{t("costs.scheduled")}</dt>
          <dd>
            {t("costs.amount", {
              amount: costs.total,
              currency: costs.currency,
            })}
          </dd>
        </div>
        <div>
          <dt>{t("costs.shopping")}</dt>
          <dd>
            {t("costs.amount", {
              amount: costs.expectedShopping,
              currency: costs.currency,
            })}
          </dd>
        </div>
        <div>
          <dt>{t("costs.actual")}</dt>
          <dd>
            {t("costs.amount", {
              amount: costs.actual,
              currency: costs.currency,
            })}
          </dd>
        </div>
        <div>
          <dt>{t("costs.remaining")}</dt>
          <dd>
            {t("costs.amount", {
              amount: costs.remaining,
              currency: costs.currency,
            })}
          </dd>
        </div>
      </dl>
      {costs.missingIngredients.length ? (
        <p role="status">
          {t("costs.missing", {
            ingredients: costs.missingIngredients.join(", "),
          })}
        </p>
      ) : null}
      {planner.lifecycle === "active" ? (
        <button disabled={pending} onClick={() => void refresh()} type="button">
          {t(pending ? "costs.refreshPending" : "costs.refresh")}
        </button>
      ) : null}
      {error ? <p role="alert">{t("costs.error")}</p> : null}
      {planner.scheduled.length ? (
        <ul className="event-costs__recipes">
          {planner.scheduled.map((item) => {
            const cost = costs.scheduled.get(item.id);
            return cost ? (
              <li key={item.id}>
                {item.name}:{" "}
                {t("costs.amount", {
                  amount: cost.total,
                  currency: costs.currency,
                })}
                {cost.perDiner
                  ? ` · ${t("costs.perDiner", { amount: cost.perDiner, currency: costs.currency })}`
                  : ""}
                {cost.missing ? ` · ${t("costs.partial")}` : ""}
              </li>
            ) : null;
          })}
        </ul>
      ) : null}
    </section>
  );
}

function DayVisibility({
  day,
  eventId,
  organizationId,
  userId,
}: {
  day: EventPlannerProjection["days"][number];
  eventId: string;
  organizationId: string;
  userId: string;
}) {
  const { t } = useTranslation();
  const [error, setError] = useState(false);
  async function setVisibility(isVisible: boolean) {
    try {
      await queueEventDayVisibility(userId, organizationId, { eventDayId: day.id, eventId, isVisible });
      setError(false);
    } catch {
      setError(true);
    }
  }
  return <><button onClick={() => void setVisibility(!day.visible)} type="button">{t(day.visible ? "planner.hideDay" : "planner.restoreDay")}</button>{error ? <p role="alert">{t("planner.errors.unavailable")}</p> : null}</>;
}

function DayLifecycle({ day, eventId, organizationId, userId }: { day: EventPlannerProjection["days"][number]; eventId: string; organizationId: string; userId: string }) {
  const { t } = useTranslation();
  const [error, setError] = useState(false);
  return <><button onClick={() => void queueEventDayLifecycle(userId, organizationId, { eventDayId: day.id, eventId, operation: "retire" }).then(() => setError(false)).catch(() => setError(true))} type="button">{t("planner.retireDay")}</button>{error ? <p role="alert">{t("planner.errors.unavailable")}</p> : null}</>;
}

function RestoreDay({ planner, eventId, organizationId, userId }: { planner: EventPlannerProjection; eventId: string; organizationId: string; userId: string }) {
  const { t } = useTranslation();
  const [dayId, setDayId] = useState(planner.retiredDays[0]?.id ?? "");
  const [error, setError] = useState(false);
  useEffect(() => setDayId((current) => planner.retiredDays.some((day) => day.id === current) ? current : (planner.retiredDays[0]?.id ?? "")), [planner.retiredDays]);
  if (planner.lifecycle !== "active" || !dayId) return null;
  return <form onSubmit={(event) => { event.preventDefault(); void queueEventDayLifecycle(userId, organizationId, { eventDayId: dayId, eventId, operation: "restore" }).then(() => setError(false)).catch(() => setError(true)); }}><label>{t("planner.day")}<select value={dayId} onChange={(event) => setDayId(event.target.value)}>{planner.retiredDays.map((day) => <option key={day.id} value={day.id}>{day.date}</option>)}</select></label><button type="submit">{t("planner.restoreDay")}</button>{error ? <p role="alert">{t("planner.errors.unavailable")}</p> : null}</form>;
}

function DayNote({ day, eventId, organizationId, userId }: { day: EventPlannerProjection["days"][number]; eventId: string; organizationId: string; userId: string }) {
  const { t } = useTranslation();
  const [note, setNote] = useState(day.note ?? "");
  const [error, setError] = useState(false);
  useEffect(() => setNote(day.note ?? ""), [day.note]);
  async function submit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    try {
      await queueEventDayNote(userId, organizationId, { eventDayId: day.id, eventId, note: note || null });
      setError(false);
    } catch {
      setError(true);
    }
  }
  return <form onSubmit={(event) => void submit(event)}><label>{t("planner.dayNote")}<textarea maxLength={4000} onChange={(event) => setNote(event.target.value)} value={note} /></label><button type="submit">{t("planner.saveDayNote")}</button>{error ? <p role="alert">{t("planner.errors.unavailable")}</p> : null}</form>;
}

function AddDay({ eventId, organizationId, userId, active }: { eventId: string; organizationId: string; userId: string; active: boolean }) {
  const { t } = useTranslation();
  const [calendarDate, setCalendarDate] = useState("");
  const [error, setError] = useState(false);
  if (!active) return null;
  return <form onSubmit={(event) => { event.preventDefault(); void queueEventDayCreate(userId, organizationId, { eventId, calendarDate }).then(() => setError(false)).catch(() => setError(true)); }}><label>{t("planner.day")}<input type="date" required value={calendarDate} onChange={(event) => setCalendarDate(event.target.value)} /></label><button type="submit">{t("planner.addDay")}</button>{error ? <p role="alert">{t("planner.errors.unavailable")}</p> : null}</form>;
}

function AddMealRole({ eventId, organizationId, userId, active }: { eventId: string; organizationId: string; userId: string; active: boolean }) {
  const { t } = useTranslation();
  const [customName, setCustomName] = useState("");
  const [error, setError] = useState(false);
  if (!active) return null;
  async function submit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    try {
      await queueEventMealRoleCreate(userId, organizationId, { eventId, customName });
      setCustomName("");
      setError(false);
    } catch {
      setError(true);
    }
  }
  return <form onSubmit={(event) => void submit(event)}><label>{t("planner.mealRoleName")}<input maxLength={200} onChange={(event) => setCustomName(event.target.value)} required value={customName} /></label><button type="submit">{t("planner.addMealRole")}</button>{error ? <p role="alert">{t("planner.errors.unavailable")}</p> : null}</form>;
}

function OrderMealRoles({ planner, eventId, organizationId, userId }: { planner: EventPlannerProjection; eventId: string; organizationId: string; userId: string }) {
  const { t } = useTranslation();
  const [roleId, setRoleId] = useState(planner.roles[0]?.id ?? "");
  const [positionKey, setPositionKey] = useState(planner.roles[0]?.position ?? "");
  const [error, setError] = useState(false);
  useEffect(() => {
    const role = planner.roles.find((item) => item.id === roleId) ?? planner.roles[0];
    if (role) { setRoleId(role.id); setPositionKey(role.position); }
  }, [planner.roles, roleId]);
  if (planner.lifecycle !== "active" || !planner.roles.length) return null;
  return <form onSubmit={(event) => { event.preventDefault(); void queueEventMealRolePosition(userId, organizationId, { eventId, eventMealRoleId: roleId, positionKey }).then(() => setError(false)).catch(() => setError(true)); }}><label>{t("planner.role")}<select value={roleId} onChange={(event) => setRoleId(event.target.value)}>{planner.roles.map((role) => <option key={role.id} value={role.id}>{role.name}</option>)}</select></label><label>{t("planner.rolePosition")}<input maxLength={255} pattern="[0-9A-Za-z]+" required value={positionKey} onChange={(event) => setPositionKey(event.target.value)} /></label><button type="submit">{t("planner.saveRolePosition")}</button>{error ? <p role="alert">{t("planner.errors.unavailable")}</p> : null}</form>;
}

function RenameMealRole({ planner, eventId, organizationId, userId }: { planner: EventPlannerProjection; eventId: string; organizationId: string; userId: string }) {
  const { t } = useTranslation();
  const roles = planner.roles.filter((role) => role.custom);
  const [roleId, setRoleId] = useState(roles[0]?.id ?? "");
  const [name, setName] = useState(roles[0]?.name ?? "");
  const [error, setError] = useState(false);
  const selectedRole = roles.find((item) => item.id === roleId) ?? roles[0];
  const selectedRoleId = selectedRole?.id;
  const selectedRoleName = selectedRole?.name;
  useEffect(() => { if (selectedRoleId && selectedRoleName) { setRoleId(selectedRoleId); setName(selectedRoleName); } else { setRoleId(""); setName(""); } }, [selectedRoleId, selectedRoleName]);
  if (planner.lifecycle !== "active" || !roleId) return null;
  return <form onSubmit={(event) => { event.preventDefault(); void queueEventMealRoleName(userId, organizationId, { eventId, eventMealRoleId: roleId, customName: name }).then(() => setError(false)).catch(() => setError(true)); }}><label>{t("planner.role")}<select value={roleId} onChange={(event) => setRoleId(event.target.value)}>{roles.map((role) => <option key={role.id} value={role.id}>{role.name}</option>)}</select></label><label>{t("planner.mealRoleName")}<input maxLength={200} required value={name} onChange={(event) => setName(event.target.value)} /></label><button type="submit">{t("planner.saveMealRoleName")}</button>{error ? <p role="alert">{t("planner.errors.unavailable")}</p> : null}</form>;
}

function MealRoleLifecycle({ planner, eventId, organizationId, userId }: { planner: EventPlannerProjection; eventId: string; organizationId: string; userId: string }) {
  const { t } = useTranslation();
  const roles = useMemo(() => [...planner.roles, ...planner.retiredRoles], [planner.roles, planner.retiredRoles]);
  const [roleId, setRoleId] = useState(roles[0]?.id ?? "");
  const [error, setError] = useState(false);
  useEffect(() => setRoleId((current) => roles.some((role) => role.id === current) ? current : (roles[0]?.id ?? "")), [roles]);
  const role = roles.find((item) => item.id === roleId);
  if (planner.lifecycle !== "active" || !role) return null;
  return <form onSubmit={(event) => { event.preventDefault(); void queueEventMealRoleLifecycle(userId, organizationId, { eventId, eventMealRoleId: role.id, operation: role.retired ? "restore" : "retire" }).then(() => setError(false)).catch(() => setError(true)); }}><label>{t("planner.role")}<select value={roleId} onChange={(event) => setRoleId(event.target.value)}>{roles.map((item) => <option key={item.id} value={item.id}>{item.name}{item.retired ? ` (${t("planner.retired")})` : ""}</option>)}</select></label><button type="submit">{t(role.retired ? "planner.restoreMealRole" : "planner.retireMealRole")}</button>{error ? <p role="alert">{t("planner.errors.unavailable")}</p> : null}</form>;
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

function Attendance({ eventId, organizationId, userId, scheduled, active }: { eventId: string; organizationId: string; userId: string; scheduled: EventPlannerProjection["scheduled"][number]; active: boolean }) {
  const { t } = useTranslation();
  const [count, setCount] = useState(String(scheduled.dinerCount));
  const [error, setError] = useState(false);
  if (!active) return null;
  async function save(dinerCount: number | null) {
    try { await queueScheduledRecipeAttendance(userId, organizationId, { scheduledRecipeId: scheduled.id, eventId, dinerCount }); setError(false); } catch { setError(true); }
  }
  return <details><summary>{t("planner.attendanceEdit")}</summary><label>{t("planner.attendance")}<input value={count} inputMode="numeric" pattern="[0-9]+" onChange={(event) => setCount(event.target.value)} /></label><button type="button" onClick={() => void save(Number(count))}>{t("planner.saveAttendance")}</button><button type="button" onClick={() => void save(null)}>{t("planner.followEvent")}</button>{error ? <p role="alert">{t("planner.errors.unavailable")}</p> : null}</details>;
}

function Scaling({
  eventId,
  organizationId,
  userId,
  scheduled,
  active,
}: {
  eventId: string;
  organizationId: string;
  userId: string;
  scheduled: EventPlannerProjection["scheduled"][number];
  active: boolean;
}) {
  const { t } = useTranslation();
  const [consumption, setConsumption] = useState(
    scheduled.consumptionPercentage,
  );
  const [scale, setScale] = useState(scheduled.selectedScaleAmount);
  const [error, setError] = useState(false);
  if (!active) return null;
  async function save(selectedScaleAmount: string | null) {
    try {
      await queueScheduledRecipeContext(userId, organizationId, {
        scheduledRecipeId: scheduled.id,
        eventId,
        consumptionPercentage: consumption,
        selectedScaleAmount,
      });
      setError(false);
    } catch {
      setError(true);
    }
  }
  return (
    <details>
      <summary>{t("planner.scalingEdit")}</summary>
      <label>
        {t("planner.consumption")}
        <input
          value={consumption}
          inputMode="decimal"
          pattern="(?:0|[1-9][0-9]*)(?:\.[0-9]+)?"
          onChange={(event) => setConsumption(event.target.value)}
        />
      </label>
      <label>
        {t("planner.scale")}
        <input
          value={scale}
          inputMode="decimal"
          pattern="(?:0|[1-9][0-9]*)(?:\.[0-9]+)?"
          onChange={(event) => setScale(event.target.value)}
        />
      </label>
      <button type="button" onClick={() => void save(scale)}>
        {t("planner.saveScale")}
      </button>
      <button type="button" onClick={() => void save(null)}>
        {t("planner.useSuggestion")}
      </button>
      {error ? <p role="alert">{t("planner.errors.unavailable")}</p> : null}
    </details>
  );
}

function ReplacementOverride({
  eventId,
  active,
  organizationId,
  userId,
  scheduled,
}: {
  eventId: string;
  active: boolean;
  organizationId: string;
  userId: string;
  scheduled: EventPlannerProjection["scheduled"][number];
}) {
  const { t } = useTranslation();
  const lines = scheduled.lines ?? [];
  const [line, setLine] = useState(lines[0]?.id ?? "");
  const [amount, setAmount] = useState(lines[0]?.quantity ?? "0");
  const [error, setError] = useState(false);
  const inFlight = useRef(false);
  if (!active || !lines.length) return null;
  async function submit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (inFlight.current) return;
    inFlight.current = true;
    try {
      await queueReplacementOverride(userId, organizationId, {
        eventId,
        scheduledRecipeId: scheduled.id,
        targetLineKey: line,
        quantity: amount,
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
      <summary>{t("planner.overrideQuantity")}</summary>
      <form onSubmit={(event) => void submit(event)}>
        <label>
          {t("planner.ingredientLine")}
          <select
            value={line}
            onChange={(event) => setLine(event.target.value)}
          >
            {lines.map((item) => (
              <option key={item.id} value={item.id}>
                {item.quantity}
              </option>
            ))}
          </select>
        </label>
        <label>
          {t("planner.quantity")}
          <input
            value={amount}
            onChange={(event) => setAmount(event.target.value)}
            inputMode="decimal"
            pattern="(?:0|[1-9][0-9]*)(?:\.[0-9]+)?"
            required
          />
        </label>
        <button type="submit">{t("planner.saveOverride")}</button>
        {error ? <p role="alert">{t("planner.errors.unavailable")}</p> : null}
      </form>
    </details>
  );
}

function AddedOverride({
  eventId,
  active,
  organizationId,
  userId,
  planner,
  scheduled,
}: {
  eventId: string;
  active: boolean;
  organizationId: string;
  userId: string;
  planner: EventPlannerProjection;
  scheduled: EventPlannerProjection["scheduled"][number];
}) {
  const { t } = useTranslation();
  const ingredients = (planner.ingredients ?? []).filter(
    (ingredient) =>
      !scheduled.lines?.some((line) => line.ingredientId === ingredient.id),
  );
  const [ingredientId, setIngredientId] = useState(ingredients[0]?.id ?? "");
  const [amount, setAmount] = useState("0");
  const [included, setIncluded] = useState(true);
  const [error, setError] = useState(false);
  const inFlight = useRef(false);
  useEffect(() => {
    setIngredientId((current) =>
      ingredients.some((item) => item.id === current)
        ? current
        : (ingredients[0]?.id ?? ""),
    );
  }, [ingredients]);
  if (!active || !ingredients.length) return null;
  const ingredient = ingredients.find((item) => item.id === ingredientId);
  async function submit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (inFlight.current || !ingredient) return;
    inFlight.current = true;
    try {
      await queueAddedOverride(userId, organizationId, {
        eventId,
        scheduledRecipeId: scheduled.id,
        ingredientId: ingredient.id,
        ingredientVersionId: ingredient.versionId,
        quantity: amount,
        includeInPortionWeight: included,
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
      <summary>{t("planner.addIngredientOverride")}</summary>
      <form onSubmit={(event) => void submit(event)}>
        <label>
          {t("planner.ingredient")}
          <select
            value={ingredientId}
            onChange={(event) => setIngredientId(event.target.value)}
          >
            {ingredients.map((item) => (
              <option key={item.id} value={item.id}>
                {item.name}
              </option>
            ))}
          </select>
        </label>
        <label>
          {t("planner.quantity")}
          <input
            value={amount}
            onChange={(event) => setAmount(event.target.value)}
            inputMode="decimal"
            pattern="(?:0|[1-9][0-9]*)(?:\.[0-9]+)?"
            required
          />
        </label>
        <label>
          <input
            checked={included}
            onChange={(event) => setIncluded(event.target.checked)}
            type="checkbox"
          />
          {t("planner.includeInPortionWeight")}
        </label>
        <button type="submit">{t("planner.saveAddedIngredient")}</button>
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
  onOpenReceipts,
}: {
  eventId: string;
  organizationId: string;
  userId: string;
  onUnauthenticated: () => void;
  onOpenShopping?: () => void;
  onOpenReceipts?: () => void;
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
      {onOpenReceipts ? (
        <button onClick={onOpenReceipts} type="button">
          {t("planner.receipts")}
        </button>
      ) : null}
      <EventCosts
        eventId={eventId}
        organizationId={organizationId}
        planner={planner}
        userId={userId}
      />
      <AddRecipe
        eventId={eventId}
        organizationId={organizationId}
        planner={planner}
        userId={userId}
      />
      <AddDay eventId={eventId} organizationId={organizationId} userId={userId} active={planner.lifecycle === "active"} />
      <RestoreDay eventId={eventId} organizationId={organizationId} planner={planner} userId={userId} />
      <AddMealRole eventId={eventId} organizationId={organizationId} userId={userId} active={planner.lifecycle === "active"} />
      <OrderMealRoles eventId={eventId} organizationId={organizationId} planner={planner} userId={userId} />
      <RenameMealRole eventId={eventId} organizationId={organizationId} planner={planner} userId={userId} />
      <MealRoleLifecycle eventId={eventId} organizationId={organizationId} planner={planner} userId={userId} />
      <div className="planner-days">
        {planner.days.map((day) => (
          <section
            className="planner-day"
            key={day.id}
            aria-labelledby={`day-${day.id}`}
          >
            <h3 id={`day-${day.id}`}>{day.date}</h3>
            {day.note ? <p>{day.note}</p> : null}
            {planner.lifecycle === "active" ? <DayVisibility day={day} eventId={eventId} organizationId={organizationId} userId={userId} /> : null}
            {planner.lifecycle === "active" ? <DayLifecycle day={day} eventId={eventId} organizationId={organizationId} userId={userId} /> : null}
            {planner.lifecycle === "active" ? <DayNote day={day} eventId={eventId} organizationId={organizationId} userId={userId} /> : null}
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
                          {item.retired ? ` · ${t("planner.retired")}` : null}
                          {planner.lifecycle === "active" ? <button onClick={() => void queueScheduledRecipeLifecycle(userId, organizationId, { scheduledRecipeId: item.id, eventId, operation: item.retired ? "restore" : "retire" })} type="button">{t(item.retired ? "planner.restoreRecipe" : "planner.retireRecipe")}</button> : null}
                          {!item.retired && <>
                          <MoveRecipe
                            eventId={eventId}
                            organizationId={organizationId}
                            planner={planner}
                            scheduled={item}
                            userId={userId}
                          />
                          <Attendance
                            eventId={eventId}
                            organizationId={organizationId}
                            userId={userId}
                            scheduled={item}
                            active={planner.lifecycle === "active"}
                          />
                          <Scaling
                            eventId={eventId}
                            organizationId={organizationId}
                            userId={userId}
                            scheduled={item}
                            active={planner.lifecycle === "active"}
                          />
                          <ReplacementOverride
                            active={planner.lifecycle === "active"}
                            eventId={eventId}
                            organizationId={organizationId}
                            userId={userId}
                            scheduled={item}
                          />
                          </>}
                          {(item.localAddedIngredients?.length ?? 0) ? (
                            <ul className="planner-local-ingredients">
                              {(item.localAddedIngredients ?? []).map((ingredient) => (
                                <li key={ingredient.id}>
                                  {t("planner.localIngredient", {
                                    name: ingredient.name,
                                    quantity: ingredient.quantity,
                                  })}
                                </li>
                              ))}
                            </ul>
                          ) : null}
                          <AddedOverride
                            active={planner.lifecycle === "active"}
                            eventId={eventId}
                            organizationId={organizationId}
                            planner={planner}
                            userId={userId}
                            scheduled={item}
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
      {planner.lifecycle === "active" && planner.hiddenDays.length ? <details><summary>{t("planner.hiddenDays")}</summary><ul>{planner.hiddenDays.map((day) => <li key={day.id}>{day.date} <DayVisibility day={day} eventId={eventId} organizationId={organizationId} userId={userId} /></li>)}</ul></details> : null}
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
