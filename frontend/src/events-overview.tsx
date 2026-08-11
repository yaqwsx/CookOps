import { liveQuery } from "dexie";
import { useCallback, useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";

import type { EventSummary } from "./api/events";
import { EventAttendance } from "./event-attendance-form";
import { EventCreate } from "./event-create-form";
import { EventLifecycle } from "./event-lifecycle-form";
import { EventDuplicate } from "./event-duplicate-form";
import { EventMetadata } from "./event-metadata-form";
import {
  canCreateEvents,
  readVisibleEventSummaries,
} from "./event-projections";
import { pullOrganization, SyncRequestError } from "./sync-bootstrap";

type EventOverviewState = "loading" | "ready" | "offline" | "error";

function formattedDate(value: string, locale: string): string {
  const date = new Date(`${value}T00:00:00Z`);
  if (Number.isNaN(date.valueOf())) return value;
  return new Intl.DateTimeFormat(locale, {
    day: "numeric",
    month: "long",
    year: "numeric",
    timeZone: "UTC",
  }).format(date);
}

function EventCard({
  event,
  onOpen,
  organizationId,
  userId,
  canManage,
}: {
  event: EventSummary;
  onOpen: (eventId: string) => void;
  organizationId: string;
  userId: string;
  canManage: boolean;
}) {
  const { i18n, t } = useTranslation();
  const dateRange =
    event.startDate === event.endDate
      ? formattedDate(event.startDate, i18n.resolvedLanguage ?? "cs")
      : t("eventsOverview.dateRange", {
          start: formattedDate(event.startDate, i18n.resolvedLanguage ?? "cs"),
          end: formattedDate(event.endDate, i18n.resolvedLanguage ?? "cs"),
        });

  return (
    <article className="event-card">
      <div>
        <h3>{event.name}</h3>
        <p className="event-card__dates">{dateRange}</p>
      </div>
      <button
        className="event-card__open"
        onClick={() => onOpen(event.id)}
        type="button"
      >
        {t("eventsOverview.open")}
      </button>
      <dl className="event-card__details">
        <div>
          <dt>{t("eventsOverview.attendance")}</dt>
          <dd>{event.baseExpectedAttendance}</dd>
        </div>
        <div>
          <dt>{t("eventsOverview.budget")}</dt>
          <dd>{`${event.budgetAmount} ${event.currency}`}</dd>
        </div>
      </dl>
      {event.lifecycle === "active" ? (
        <EventMetadata
          budgetAmount={event.budgetAmount}
          eventId={event.id}
          generalNote={event.generalNote ?? null}
          location={event.location ?? null}
          name={event.name}
          organizationId={organizationId}
          userId={userId}
        />
      ) : null}
      <span
        className={`event-card__lifecycle event-card__lifecycle--${event.lifecycle}`}
      >
        {t(`eventsOverview.lifecycle.${event.lifecycle}`)}
      </span>
      {event.lifecycle === "active" ? (
        <EventAttendance
          attendance={event.baseExpectedAttendance}
          eventId={event.id}
          organizationId={organizationId}
          userId={userId}
        />
      ) : null}
      {canManage ? (
        <EventLifecycle
          eventId={event.id}
          lifecycle={event.lifecycle}
          organizationId={organizationId}
          userId={userId}
        />
      ) : null}
      {canManage && event.lifecycle === "archived" ? (
        <EventDuplicate
          eventId={event.id}
          name={event.name}
          organizationId={organizationId}
          snapshotId={event.currentArchiveSnapshotId ?? null}
          userId={userId}
        />
      ) : null}
    </article>
  );
}

export function EventOverview({
  onOpen = () => undefined,
  organizationId,
  userId,
  onUnauthenticated,
}: {
  onOpen?: (eventId: string) => void;
  organizationId: string;
  userId: string;
  onUnauthenticated: () => void;
}) {
  const { t } = useTranslation();
  const [state, setState] = useState<EventOverviewState>("loading");
  const [events, setEvents] = useState<EventSummary[]>([]);
  const [canCreate, setCanCreate] = useState(false);
  const generation = useRef(0);
  const synchronize = useCallback(async () => {
    const currentGeneration = generation.current;
    if (!navigator.onLine) {
      if (currentGeneration === generation.current) setState("offline");
      return;
    }
    try {
      await pullOrganization(userId, organizationId);
      if (currentGeneration === generation.current) setState("ready");
    } catch (error) {
      if (error instanceof SyncRequestError && error.status === 401) {
        if (currentGeneration === generation.current) onUnauthenticated();
        return;
      }
      if (currentGeneration === generation.current) setState("error");
    }
  }, [onUnauthenticated, organizationId, userId]);

  useEffect(() => {
    let active = true;
    generation.current += 1;
    const subscription = liveQuery(async () => ({
      canCreate: await canCreateEvents(userId, organizationId),
      events: await readVisibleEventSummaries(userId, organizationId),
    })).subscribe({
      next: (projection) => {
        if (active) {
          setCanCreate(projection.canCreate);
          setEvents(projection.events);
        }
      },
      error: () => {
        if (active) setState("error");
      },
    });
    function offline() {
      if (active) setState("offline");
    }
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
  }, [organizationId, synchronize, userId]);

  if (state === "loading" && events.length === 0) {
    return (
      <p aria-live="polite" role="status">
        {t("eventsOverview.loading")}
      </p>
    );
  }
  if (state === "error" && events.length === 0) {
    return (
      <div className="event-overview">
        {canCreate ? (
          <EventCreate organizationId={organizationId} userId={userId} />
        ) : null}
        <div className="event-overview-error" role="alert">
          <p>{t("eventsOverview.error")}</p>
          <button onClick={() => void synchronize()} type="button">
            {t("eventsOverview.retry")}
          </button>
        </div>
      </div>
    );
  }
  if (state === "ready" && events.length === 0) {
    return (
      <div className="event-overview">
        {canCreate ? (
          <EventCreate organizationId={organizationId} userId={userId} />
        ) : null}
        <p>{t("eventsOverview.empty")}</p>
      </div>
    );
  }
  return (
    <div className="event-overview">
      <p className="event-overview__scope" role="note">
        {t("eventsOverview.scope")}
      </p>
      {canCreate ? (
        <EventCreate organizationId={organizationId} userId={userId} />
      ) : null}
      {state === "offline" ? (
        <p aria-live="polite" role="status">
          {t("eventsOverview.offline")}
        </p>
      ) : null}
      <div className="event-list">
        {events.map((event) => (
          <EventCard
            event={event}
            key={event.id}
            onOpen={onOpen}
            organizationId={organizationId}
            userId={userId}
            canManage={canCreate}
          />
        ))}
      </div>
      {state === "error" ? (
        <div className="event-overview-error" role="alert">
          <p>{t("eventsOverview.error")}</p>
          <button onClick={() => void synchronize()} type="button">
            {t("eventsOverview.retry")}
          </button>
        </div>
      ) : null}
    </div>
  );
}
