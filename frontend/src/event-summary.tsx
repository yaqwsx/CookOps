import { liveQuery } from "dexie";
import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import type { EventCostsProjection } from "./event-cost-projections";
import type { EventPlannerProjection } from "./planner-projections";
import { localDb } from "./local-db";
import { EventDuplicate } from "./event-duplicate-form";
import { EventLifecycle } from "./event-lifecycle-form";
import { readEventCapabilities, readVisibleEventSummaries } from "./event-projections";

export function formattedDate(value: string, locale: string) {
  const date = new Date(`${value}T00:00:00Z`);
  return Number.isNaN(date.valueOf()) ? value : new Intl.DateTimeFormat(locale, { day: "numeric", month: "long", year: "numeric", timeZone: "UTC" }).format(date);
}

export function useEventPendingSync(userId: string, organizationId: string, eventId: string) {
  const [status, setStatus] = useState({ pending: 0, failed: 0 });
  useEffect(() => {
    let active = true;
    setStatus({ pending: 0, failed: 0 });
    const subscription = liveQuery(async () => {
      const [commands, lists, overlays, archives] = await Promise.all([
        localDb.outbox.where("organizationId").equals(organizationId).toArray(),
        localDb.canonicalRecords.where("[userId+organizationId]").equals([userId, organizationId]).toArray(),
        localDb.optimisticOverlays.where("[userId+organizationId]").equals([userId, organizationId]).toArray(),
        localDb.archiveRecords.toArray(),
      ]);
      const eventLists = new Set([...lists, ...overlays].filter((record) => record.userId === userId && record.entityType === "shopping_list" && record.fields.event_id === eventId).map((record) => record.entityId));
      for (const record of archives) if (record.userId === userId && record.organizationId === organizationId && record.entityType === "shopping_list" && record.eventId === eventId) eventLists.add(record.entityId);
      return commands.reduce((result, command) => {
        if (command.userId !== userId) return result;
        const payload = command.payload;
        const belongs = payload.event_id === eventId || (typeof payload.shopping_list_id === "string" && eventLists.has(payload.shopping_list_id));
        if (belongs) command.state === "pending" ? result.pending++ : result.failed++;
        return result;
      }, { pending: 0, failed: 0 });
    }).subscribe({ next: (next) => active && setStatus(next) });
    return () => { active = false; subscription.unsubscribe(); };
  }, [eventId, organizationId, userId]);
  return status;
}

export function EventSummary({ planner, costs, pendingSync, eventId, organizationId, userId }: { planner: EventPlannerProjection; costs?: EventCostsProjection; pendingSync: { pending: number; failed: number }; eventId?: string; organizationId?: string; userId?: string }) {
  const { i18n, t } = useTranslation();
  const locale = i18n.resolvedLanguage ?? "cs";
  const [controls, setControls] = useState<{ canManage: boolean; canDuplicate: boolean; snapshotId: string | null }>({ canManage: false, canDuplicate: false, snapshotId: null });
  useEffect(() => {
    if (!eventId || !organizationId || !userId) return;
    const subscription = liveQuery(async () => {
      const [capabilities, event] = await Promise.all([
        readEventCapabilities(userId, organizationId),
        readVisibleEventSummaries(userId, organizationId),
      ]);
      return { ...capabilities, snapshotId: event.find((candidate) => candidate.id === eventId)?.currentArchiveSnapshotId ?? null };
    }).subscribe({ next: setControls });
    return () => subscription.unsubscribe();
  }, [eventId, organizationId, userId]);
  const sync = [pendingSync.failed && t("planner.pendingSyncFailed", { count: pendingSync.failed }), pendingSync.pending && t("planner.pendingSyncCount", { count: pendingSync.pending })].filter(Boolean).join(" · ") || t("planner.pendingSyncCount", { count: 0 });
  return <header className="event-workspace__summary"><div><h2>{planner.name}</h2><p>{t("planner.dateRange", { start: formattedDate(planner.startDate, locale), end: formattedDate(planner.endDate, locale) })}</p></div><dl><div><dt>{t("planner.attendance")}</dt><dd>{planner.attendance}</dd></div><div><dt>{t("planner.lifecycle")}</dt><dd>{t(`eventsOverview.lifecycle.${planner.lifecycle}`)}</dd></div>{costs ? <><div><dt>{t("costs.budget")}</dt><dd>{t("costs.amount", { amount: costs.budget, currency: costs.currency })}</dd></div><div><dt>{t("costs.scheduled")}</dt><dd>{t("costs.amount", { amount: costs.total, currency: costs.currency })}</dd></div><div><dt>{t("costs.actual")}</dt><dd>{t("costs.amount", { amount: costs.actual, currency: costs.currency })}</dd></div><div><dt>{t("costs.remaining")}</dt><dd>{t("costs.amount", { amount: costs.remaining, currency: costs.currency })}</dd></div></> : null}<div><dt>{t("planner.pendingSync")}</dt><dd>{sync}</dd></div></dl>{planner.lifecycle === "archived" ? <p className="event-workspace__archived" role="status">{t("eventSettings.archivedReadOnly")}</p> : null}{eventId && organizationId && userId ? <div className="event-workspace__lifecycle">{controls.canManage ? <EventLifecycle eventId={eventId} lifecycle={planner.lifecycle} organizationId={organizationId} userId={userId} /> : null}{controls.canDuplicate && planner.lifecycle === "archived" ? <EventDuplicate eventId={eventId} name={planner.name} organizationId={organizationId} snapshotId={controls.snapshotId} userId={userId} /> : null}</div> : null}</header>;
}
