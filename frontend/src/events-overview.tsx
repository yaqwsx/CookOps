import { liveQuery } from "dexie";
import { useCallback, useEffect, useRef, useState, type MouseEvent } from "react";
import { useTranslation } from "react-i18next";

import type { EventSummary } from "./api/events";
import { EventRequestError, getEventPage } from "./api/events";
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
          startDate={event.startDate}
          endDate={event.endDate}
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
  onOpenRecipes,
  onOpenIngredients,
  organizationId,
  userId,
  onUnauthenticated,
}: {
  onOpen?: (eventId: string) => void;
  onOpenRecipes?: (event: MouseEvent<HTMLAnchorElement>) => void;
  onOpenIngredients?: (event: MouseEvent<HTMLAnchorElement>) => void;
  organizationId: string;
  userId: string;
  onUnauthenticated: () => void;
}) {
  const { t } = useTranslation();
  const [state, setState] = useState<EventOverviewState>("loading");
  const [events, setEvents] = useState<EventSummary[]>([]);
  const [archiveEvents, setArchiveEvents] = useState<EventSummary[]>([]);
  const [archiveCursor, setArchiveCursor] = useState<string | null>(null);
  const [archiveLoading, setArchiveLoading] = useState(false);
  const [archiveError, setArchiveError] = useState(false);
  const [archiveQuery, setArchiveQuery] = useState("");
  const [canCreate, setCanCreate] = useState(false);
  const generation = useRef(0);
  const loadArchivePage = useCallback(async (cursor?: string, expectedGeneration = generation.current) => {
    if (expectedGeneration !== generation.current) return;
    const currentGeneration = expectedGeneration;
    if (!navigator.onLine) return;
    setArchiveLoading(true);
    setArchiveError(false);
    try {
      const page = await getEventPage(organizationId, cursor);
      if (currentGeneration !== generation.current) return;
      setArchiveEvents((current) => {
        const merged = new Map(current.map((event) => [event.id, event]));
        for (const event of page.events) merged.set(event.id, event);
        return [...merged.values()];
      });
      setArchiveCursor(page.nextCursor);
    } catch (error) {
      if (currentGeneration !== generation.current) return;
      if (error instanceof EventRequestError && error.status === 401) {
        onUnauthenticated();
        return;
      }
      setArchiveError(true);
    } finally {
      if (currentGeneration === generation.current) setArchiveLoading(false);
    }
  }, [onUnauthenticated, organizationId]);
  const synchronize = useCallback(async () => {
    const currentGeneration = generation.current;
    if (!navigator.onLine) {
      if (currentGeneration === generation.current) setState("offline");
      return;
    }
    try {
      await pullOrganization(userId, organizationId);
      await loadArchivePage(undefined, currentGeneration);
      if (currentGeneration === generation.current) setState("ready");
    } catch (error) {
      if (error instanceof SyncRequestError && error.status === 401) {
        if (currentGeneration === generation.current) onUnauthenticated();
        return;
      }
      if (currentGeneration === generation.current) setState("error");
    }
  }, [loadArchivePage, onUnauthenticated, organizationId, userId]);

  useEffect(() => {
    let active = true;
    generation.current += 1;
    setArchiveEvents([]);
    setArchiveCursor(null);
    setArchiveError(false);
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

  const localEventIds = new Set(events.map((event) => event.id));
  const allEvents = [...events, ...archiveEvents.filter((event) => !localEventIds.has(event.id))];
  const hasArchivedEvents = allEvents.some((event) => event.lifecycle === "archived");
  const normalizedArchiveQuery = archiveQuery.trim().normalize("NFC").toLocaleLowerCase();
  const visibleEvents = allEvents.filter((event) =>
    event.lifecycle === "active" ||
    !normalizedArchiveQuery ||
    [event.name, event.id].some((value) =>
      value.normalize("NFC").toLocaleLowerCase().includes(normalizedArchiveQuery),
    ),
  );
  const catalogLinks = (
    <p className="event-overview__catalog-links">
      <a href={`/organizations/${organizationId}/recipes`} onClick={onOpenRecipes}>
        {t("shell.recipes")}
      </a>
      <a href={`/organizations/${organizationId}/ingredients`} onClick={onOpenIngredients}>
        {t("shell.ingredients")}
      </a>
    </p>
  );

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
        {catalogLinks}
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
  if (state === "ready" && allEvents.length === 0) {
    return (
      <div className="event-overview">
        {catalogLinks}
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
      {catalogLinks}
      {canCreate ? (
        <EventCreate organizationId={organizationId} userId={userId} />
      ) : null}
      {state === "offline" ? (
        <p aria-live="polite" role="status">
          {t("eventsOverview.offline")}
        </p>
      ) : null}
      {hasArchivedEvents ? (
        <div className="event-overview__archive-search">
          <label htmlFor="archived-event-search">
            {t("eventsOverview.archiveSearch")}
          </label>
          <input
            id="archived-event-search"
            onChange={(event) => setArchiveQuery(event.target.value)}
            type="search"
            value={archiveQuery}
          />
          {archiveQuery ? (
            <button
              onClick={() => setArchiveQuery("")}
              type="button"
            >
              {t("eventsOverview.clearArchiveSearch")}
            </button>
          ) : null}
        </div>
      ) : null}
      {archiveError ? (
        <div className="event-overview-error" role="alert">
          <p>{t("eventsOverview.archiveError")}</p>
          <button onClick={() => void loadArchivePage()} type="button">
            {t("eventsOverview.retry")}
          </button>
        </div>
      ) : null}
      <div className="event-list">
        {visibleEvents.map((event) => (
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
      {archiveCursor ? (
        <button
          disabled={archiveLoading}
          onClick={() => void loadArchivePage(archiveCursor)}
          type="button"
        >
          {archiveLoading ? t("eventsOverview.loading") : t("eventsOverview.loadMore")}
        </button>
      ) : null}
      {hasArchivedEvents && visibleEvents.every((event) => event.lifecycle === "active") && normalizedArchiveQuery ? (
        <p role="status">{t("eventsOverview.archiveSearchEmpty")}</p>
      ) : null}
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
