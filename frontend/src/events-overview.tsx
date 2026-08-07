import { liveQuery } from "dexie";
import { useCallback, useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";

import type { EventSummary } from "./api/events";
import { readVisibleEventSummaries } from "./event-projections";
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

function EventCard({ event }: { event: EventSummary }) {
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
      <span
        className={`event-card__lifecycle event-card__lifecycle--${event.lifecycle}`}
      >
        {t(`eventsOverview.lifecycle.${event.lifecycle}`)}
      </span>
    </article>
  );
}

export function EventOverview({
  organizationId,
  userId,
  onUnauthenticated,
}: {
  organizationId: string;
  userId: string;
  onUnauthenticated: () => void;
}) {
  const { t } = useTranslation();
  const [state, setState] = useState<EventOverviewState>("loading");
  const [events, setEvents] = useState<EventSummary[]>([]);
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
    const subscription = liveQuery(() =>
      readVisibleEventSummaries(userId, organizationId),
    ).subscribe({
      next: (nextEvents) => {
        if (active) setEvents(nextEvents);
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
      <div className="event-overview-error" role="alert">
        <p>{t("eventsOverview.error")}</p>
        <button onClick={() => void synchronize()} type="button">
          {t("eventsOverview.retry")}
        </button>
      </div>
    );
  }
  if (state === "ready" && events.length === 0) {
    return <p>{t("eventsOverview.empty")}</p>;
  }
  return (
    <div className="event-overview">
      <p className="event-overview__scope" role="note">
        {t("eventsOverview.scope")}
      </p>
      {state === "offline" ? (
        <p aria-live="polite" role="status">
          {t("eventsOverview.offline")}
        </p>
      ) : null}
      <div className="event-list">
        {events.map((event) => (
          <EventCard event={event} key={event.id} />
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
