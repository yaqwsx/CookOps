import { liveQuery } from "dexie";
import { useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import type { EventSummary } from "./api/events";
import { EventAttendance } from "./event-attendance-form";
import { EventLifecycle } from "./event-lifecycle-form";
import { EventMetadata } from "./event-metadata-form";
import { EventPriceRefreshControl } from "./event-price-refresh-control";
import { EventDietaryExceptions } from "./event-dietary-exception-form";
import { EventSectionNavigation } from "./event-section-navigation";
import {
  canCreateEvents,
  readVisibleEventSummaries,
} from "./event-projections";
import { ensureArchivedEventCached } from "./archive-cache";
import { pullOrganization, SyncRequestError } from "./sync-bootstrap";

export function EventSettingsPage({
  eventId,
  organizationId,
  userId,
  onUnauthenticated,
}: {
  eventId: string;
  organizationId: string;
  userId: string;
  onUnauthenticated?: () => void;
  onOpenPlanner?: () => void;
  onOpenCosts?: () => void;
}) {
  const { t } = useTranslation();
  const identity = `${userId}:${organizationId}:${eventId}`;
  const [state, setState] = useState<{
    identity: string;
    event?: EventSummary;
    canManage?: boolean;
    error?: boolean;
  }>({ identity });
  const generation = useRef(0);
  useEffect(() => {
    const current = ++generation.current;
    const controller = new AbortController();
    void (async () => {
      if (!navigator.onLine) return;
      try {
        const initial = (await readVisibleEventSummaries(userId, organizationId)).find(
          (candidate) => candidate.id === eventId,
        );
        if (initial?.lifecycle === "active") return;
        await pullOrganization(userId, organizationId);
        if (current !== generation.current) return;
        const event = (await readVisibleEventSummaries(userId, organizationId)).find(
          (candidate) => candidate.id === eventId,
        );
        if (event?.lifecycle !== "archived") return;
        await ensureArchivedEventCached(userId, organizationId, eventId, fetch, controller.signal);
      } catch (error) {
        if (controller.signal.aborted || current !== generation.current) return;
        if (error instanceof SyncRequestError && error.status === 401)
          return onUnauthenticated?.();
        // Keep the cached projection available when refresh fails.
      }
    })();
    return () => controller.abort();
  }, [eventId, onUnauthenticated, organizationId, userId]);
  useEffect(() => {
    const effectIdentity = identity;
    const subscription = liveQuery(async () => ({
      event: (await readVisibleEventSummaries(userId, organizationId)).find(
        (event) => event.id === eventId,
      ),
      canManage: await canCreateEvents(userId, organizationId),
    })).subscribe({
      next: ({ event, canManage }) =>
        setState({ identity: effectIdentity, event, canManage }),
      error: () => setState({ identity: effectIdentity, error: true }),
    });
    return () => subscription.unsubscribe();
  }, [eventId, identity, organizationId, userId]);
  const event = state.identity === identity ? state.event : undefined;
  const error = state.identity === identity && state.error;
  const canManage = state.identity === identity && state.canManage;
  if (!event && !error)
    return <p role="status">{t("eventSettings.loading")}</p>;
  if (!event) return <p role="alert">{t("eventSettings.unavailable")}</p>;
  return (
    <section aria-labelledby="event-settings-heading">
      <EventSectionNavigation current="settings" eventId={eventId} organizationId={organizationId} />
      <h2 id="event-settings-heading">{t("eventSettings.heading")}</h2>
      <p>
        {t("eventSettings.lifecycle")}:{" "}
        {t(`eventsOverview.lifecycle.${event.lifecycle}`)}
      </p>
      {event.lifecycle === "active" ? (
        <>
          <EventMetadata
            budgetAmount={event.budgetAmount}
            eventId={event.id}
            generalNote={event.generalNote ?? null}
            location={event.location ?? null}
            name={event.name}
            startDate={event.startDate}
            endDate={event.endDate}
            organizationId={organizationId}
            userId={userId}
          />
          <EventAttendance
            attendance={event.baseExpectedAttendance}
            eventId={event.id}
            organizationId={organizationId}
            userId={userId}
          />
          <EventPriceRefreshControl
            eventId={event.id}
            organizationId={organizationId}
            userId={userId}
          />
          <EventDietaryExceptions eventId={event.id} organizationId={organizationId} userId={userId} />
          {canManage ? (
            <EventLifecycle
              eventId={event.id}
              lifecycle={event.lifecycle}
              organizationId={organizationId}
              userId={userId}
            />
          ) : null}
        </>
      ) : (
        <>
          <aside role="status">{t("eventSettings.archivedReadOnly")}</aside>
          <dl>
            <div>
              <dt>{t("eventsEdit.attendance")}</dt>
              <dd>{event.baseExpectedAttendance}</dd>
            </div>
            <div>
              <dt>{t("eventsEdit.name")}</dt>
              <dd>{event.name}</dd>
            </div>
            <div>
              <dt>{t("eventsEdit.location")}</dt>
              <dd>{event.location ?? "—"}</dd>
            </div>
            <div>
              <dt>{t("eventsEdit.budget")}</dt>
              <dd>
                {event.budgetAmount} {event.currency}
              </dd>
            </div>
            <div>
              <dt>{t("eventsEdit.note")}</dt>
              <dd>{event.generalNote ?? "—"}</dd>
            </div>
          </dl>
          {canManage ? (
            <EventLifecycle
              eventId={event.id}
              lifecycle={event.lifecycle}
              organizationId={organizationId}
              userId={userId}
            />
          ) : null}
        </>
      )}
    </section>
  );
}
