import { liveQuery } from "dexie";
import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import type { EventCostsProjection } from "./event-cost-projections";
import type { EventPlannerProjection } from "./planner-projections";
import { localDb } from "./local-db";

export function formattedDate(value: string, locale: string) {
  const date = new Date(`${value}T00:00:00Z`);
  return Number.isNaN(date.valueOf()) ? value : new Intl.DateTimeFormat(locale, { day: "numeric", month: "long", year: "numeric", timeZone: "UTC" }).format(date);
}

export function useEventPendingSync(userId: string, organizationId: string, eventId: string) {
  const [status, setStatus] = useState({ pending: 0, failed: 0 });
  useEffect(() => {
    let active = true;
    setStatus({ pending: 0, failed: 0 });
    const subscription = liveQuery(async () => (await localDb.outbox.where("organizationId").equals(organizationId).toArray()).reduce((result, command) => {
      if (command.userId === userId && command.payload.event_id === eventId) command.state === "pending" ? result.pending++ : result.failed++;
      return result;
    }, { pending: 0, failed: 0 })).subscribe({ next: (next) => active && setStatus(next) });
    return () => { active = false; subscription.unsubscribe(); };
  }, [eventId, organizationId, userId]);
  return status;
}

export function EventSummary({ planner, costs, pendingSync }: { planner: EventPlannerProjection; costs?: EventCostsProjection; pendingSync: { pending: number; failed: number } }) {
  const { i18n, t } = useTranslation();
  const locale = i18n.resolvedLanguage ?? "cs";
  const sync = [pendingSync.failed && t("planner.pendingSyncFailed", { count: pendingSync.failed }), pendingSync.pending && t("planner.pendingSyncCount", { count: pendingSync.pending })].filter(Boolean).join(" · ") || t("planner.pendingSyncCount", { count: 0 });
  return <header className="event-workspace__summary"><div><h2>{planner.name}</h2><p>{t("planner.dateRange", { start: formattedDate(planner.startDate, locale), end: formattedDate(planner.endDate, locale) })}</p></div><dl><div><dt>{t("planner.attendance")}</dt><dd>{planner.attendance}</dd></div><div><dt>{t("planner.lifecycle")}</dt><dd>{t(`eventsOverview.lifecycle.${planner.lifecycle}`)}</dd></div>{costs ? <><div><dt>{t("costs.budget")}</dt><dd>{t("costs.amount", { amount: costs.budget, currency: costs.currency })}</dd></div><div><dt>{t("costs.scheduled")}</dt><dd>{t("costs.amount", { amount: costs.total, currency: costs.currency })}</dd></div><div><dt>{t("costs.actual")}</dt><dd>{t("costs.amount", { amount: costs.actual, currency: costs.currency })}</dd></div><div><dt>{t("costs.remaining")}</dt><dd>{t("costs.amount", { amount: costs.remaining, currency: costs.currency })}</dd></div></> : null}<div><dt>{t("planner.pendingSync")}</dt><dd>{sync}</dd></div></dl></header>;
}
