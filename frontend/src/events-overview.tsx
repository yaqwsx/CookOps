import { useCallback, useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";

import {
  type EventSummary,
  EventRequestError,
  getEventPage,
} from "./api/events";

type EventOverviewState =
  | { status: "loading"; events: EventSummary[] }
  | { status: "ready"; events: EventSummary[]; nextCursor: string | null }
  | { status: "error"; events: EventSummary[] };

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
  onUnauthenticated,
}: {
  organizationId: string;
  onUnauthenticated: () => void;
}) {
  const { t } = useTranslation();
  const [reload, setReload] = useState(0);
  const [state, setState] = useState<EventOverviewState>({
    status: "loading",
    events: [],
  });
  const requestGeneration = useRef(0);
  const fetchPage = useCallback(
    (cursor?: string) => getEventPage(organizationId, cursor, reload > 0),
    [organizationId, reload],
  );

  useEffect(() => {
    let active = true;
    const generation = requestGeneration.current + 1;
    requestGeneration.current = generation;
    setState({ status: "loading", events: [] });
    void fetchPage()
      .then((page) => {
        if (active && generation === requestGeneration.current)
          setState({
            status: "ready",
            events: page.events,
            nextCursor: page.nextCursor,
          });
      })
      .catch((error: unknown) => {
        if (!active || generation !== requestGeneration.current) return;
        if (error instanceof EventRequestError && error.status === 401) {
          onUnauthenticated();
          return;
        }
        setState({ status: "error", events: [] });
      });
    return () => {
      active = false;
      if (generation === requestGeneration.current)
        requestGeneration.current += 1;
    };
  }, [fetchPage, onUnauthenticated]);

  async function loadMore() {
    if (state.status !== "ready" || !state.nextCursor) return;
    const cursor = state.nextCursor;
    const generation = requestGeneration.current + 1;
    requestGeneration.current = generation;
    setState({ status: "loading", events: state.events });
    try {
      const page = await fetchPage(cursor);
      if (generation !== requestGeneration.current) return;
      setState({
        status: "ready",
        events: [...state.events, ...page.events],
        nextCursor: page.nextCursor,
      });
    } catch (error) {
      if (generation !== requestGeneration.current) return;
      if (error instanceof EventRequestError && error.status === 401) {
        onUnauthenticated();
        return;
      }
      setState({ status: "error", events: state.events });
    }
  }

  if (state.status === "loading" && state.events.length === 0) {
    return (
      <p aria-live="polite" role="status">
        {t("eventsOverview.loading")}
      </p>
    );
  }
  if (state.status === "error" && state.events.length === 0) {
    return (
      <div className="event-overview-error" role="alert">
        <p>{t("eventsOverview.error")}</p>
        <button onClick={() => setReload((value) => value + 1)} type="button">
          {t("eventsOverview.retry")}
        </button>
      </div>
    );
  }
  if (state.status === "ready" && state.events.length === 0) {
    return <p>{t("eventsOverview.empty")}</p>;
  }
  return (
    <div className="event-overview">
      <p className="event-overview__scope" role="note">
        {t("eventsOverview.scope")}
      </p>
      <div className="event-list">
        {state.events.map((event) => (
          <EventCard event={event} key={event.id} />
        ))}
      </div>
      {state.status === "loading" ? (
        <p aria-live="polite" role="status">
          {t("eventsOverview.loadingMore")}
        </p>
      ) : null}
      {state.status === "error" ? (
        <div className="event-overview-error" role="alert">
          <p>{t("eventsOverview.error")}</p>
          <button onClick={() => setReload((value) => value + 1)} type="button">
            {t("eventsOverview.retry")}
          </button>
        </div>
      ) : null}
      {state.status === "ready" && state.nextCursor ? (
        <button onClick={() => void loadMore()} type="button">
          {t("eventsOverview.more")}
        </button>
      ) : null}
    </div>
  );
}
