import { liveQuery } from "dexie";
import { useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";

import { EventCosts } from "./event-planner";
import {
  readEventCosts,
  type EventCostsProjection,
} from "./event-cost-projections";
import {
  readEventPlanner,
  type EventPlannerProjection,
} from "./planner-projections";
import { EventSummary, useEventPendingSync } from "./event-summary";
import { EventSectionNavigation } from "./event-section-navigation";
import { ensureArchivedEventCached } from "./archive-cache";
import { pullOrganization, SyncRequestError } from "./sync-bootstrap";
import { readVisibleEventSummaries } from "./event-projections";

export function EventCostsPage({
  eventId,
  organizationId,
  userId,
  onUnauthenticated,
  onOpenReceipts,
}: {
  eventId: string;
  organizationId: string;
  userId: string;
  onUnauthenticated?: () => void;
  onBack?: () => void;
  onOpenReceipts?: () => void;
}) {
  const { t } = useTranslation();
  const identity = `${userId}:${organizationId}:${eventId}`;
  const [plannerState, setPlannerState] = useState<{
    identity: string;
    planner?: EventPlannerProjection;
  }>();
  const [costsState, setCostsState] = useState<{
    identity: string;
    costs?: EventCostsProjection;
  }>();
  const [errorState, setErrorState] = useState({ identity, error: false });
  const [costsErrorState, setCostsErrorState] = useState({
    identity,
    error: false,
  });
  const pendingSync = useEventPendingSync(userId, organizationId, eventId);
  const generation = useRef(0);
  useEffect(() => {
    const current = ++generation.current;
    const controller = new AbortController();
    void (async () => {
      if (!navigator.onLine) return;
      try {
        const initial = (
          await readVisibleEventSummaries(userId, organizationId)
        ).find((candidate) => candidate.id === eventId);
        if (initial?.lifecycle === "active") return;
        await pullOrganization(userId, organizationId);
        if (current !== generation.current) return;
        const event = (
          await readVisibleEventSummaries(userId, organizationId)
        ).find((candidate) => candidate.id === eventId);
        if (event?.lifecycle !== "archived") return;
        await ensureArchivedEventCached(
          userId,
          organizationId,
          eventId,
          fetch,
          controller.signal,
        );
      } catch (error) {
        if (controller.signal.aborted || current !== generation.current) return;
        if (error instanceof SyncRequestError && error.status === 401)
          return onUnauthenticated?.();
        // Keep the cached projection available when refresh fails.
      }
    })();
    return () => controller.abort();
  }, [eventId, onUnauthenticated, organizationId, userId]);
  // biome-ignore lint/correctness/useExhaustiveDependencies: identity is derived from the listed route dependencies.
  useEffect(() => {
    const effectIdentity = identity;
    setCostsErrorState({ identity: effectIdentity, error: false });
    const subscription = liveQuery(() =>
      readEventPlanner(userId, organizationId, eventId),
    ).subscribe({
      next: (next) =>
        setPlannerState({ identity: effectIdentity, planner: next }),
      error: () => setErrorState({ identity: effectIdentity, error: true }),
    });
    const costsSubscription = liveQuery(() =>
      readEventCosts(userId, organizationId, eventId),
    ).subscribe({
      next: (next) => setCostsState({ identity: effectIdentity, costs: next }),
      error: () =>
        setCostsErrorState({ identity: effectIdentity, error: true }),
    });
    setCostsState(undefined);
    return () => {
      subscription.unsubscribe();
      costsSubscription.unsubscribe();
    };
  }, [eventId, organizationId, userId]);
  const planner =
    plannerState?.identity === identity ? plannerState.planner : undefined;
  const error = errorState.identity === identity && errorState.error;
  if (!planner && !error) return <p role="status">{t("costs.loading")}</p>;
  if (!planner) return <p role="alert">{t("costs.unavailable")}</p>;
  return (
    <>
      <EventSummary
        eventId={eventId}
        organizationId={organizationId}
        userId={userId}
        planner={planner}
        costs={costsState?.identity === identity ? costsState.costs : undefined}
        pendingSync={pendingSync}
      />
      <EventSectionNavigation
        current="costs"
        eventId={eventId}
        organizationId={organizationId}
      />
      <EventCosts
        eventId={eventId}
        organizationId={organizationId}
        planner={planner}
        userId={userId}
        providedCosts={
          costsState?.identity === identity ? costsState.costs : undefined
        }
      />
      {planner.lifecycle !== "archived" && onOpenReceipts ? (
        <button onClick={onOpenReceipts} type="button">
          {t("costs.openReceipts")}
        </button>
      ) : null}
      {planner.lifecycle === "archived" ? (
        <p className="planner-archived" role="status">
          {t("planner.archived")}
        </p>
      ) : null}
      {costsErrorState.identity === identity && costsErrorState.error ? (
        <p role="alert">{t("costs.unavailable")}</p>
      ) : null}
    </>
  );
}
