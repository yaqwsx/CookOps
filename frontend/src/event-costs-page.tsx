import { liveQuery } from "dexie";
import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";

import { EventCosts } from "./event-planner";
import { readEventCosts, type EventCostsProjection } from "./event-cost-projections";
import {
  readEventPlanner,
  type EventPlannerProjection,
} from "./planner-projections";
import { EventSummary, useEventPendingSync } from "./event-summary";

export function EventCostsPage({
  eventId,
  organizationId,
  userId,
  onBack,
  onOpenReceipts,
}: {
  eventId: string;
  organizationId: string;
  userId: string;
  onBack: () => void;
  onOpenReceipts: () => void;
}) {
  const { t } = useTranslation();
  const identity = `${userId}:${organizationId}:${eventId}`;
  const [plannerState, setPlannerState] = useState<{
    identity: string;
    planner?: EventPlannerProjection;
  }>();
  const [costsState, setCostsState] = useState<{ identity: string; costs?: EventCostsProjection }>();
  const [errorState, setErrorState] = useState({ identity, error: false });
  const [costsErrorState, setCostsErrorState] = useState({ identity, error: false });
  const pendingSync = useEventPendingSync(userId, organizationId, eventId);
  // biome-ignore lint/correctness/useExhaustiveDependencies: identity is derived from the listed route dependencies.
  useEffect(() => {
    const effectIdentity = identity;
    setCostsErrorState({ identity: effectIdentity, error: false });
    const subscription = liveQuery(() =>
      readEventPlanner(userId, organizationId, eventId),
    ).subscribe({
      next: (next) => setPlannerState({ identity: effectIdentity, planner: next }),
      error: () => setErrorState({ identity: effectIdentity, error: true }),
    });
    const costsSubscription = liveQuery(() => readEventCosts(userId, organizationId, eventId)).subscribe({
      next: (next) => setCostsState({ identity: effectIdentity, costs: next }),
      error: () => setCostsErrorState({ identity: effectIdentity, error: true }),
    });
    setCostsState(undefined);
    return () => { subscription.unsubscribe(); costsSubscription.unsubscribe(); };
  }, [eventId, organizationId, userId]);
  const planner = plannerState?.identity === identity ? plannerState.planner : undefined;
  const error = errorState.identity === identity && errorState.error;
  if (!planner && !error) return <p role="status">{t("costs.loading")}</p>;
  if (!planner) return <p role="alert">{t("costs.unavailable")}</p>;
  return (
    <>
      <EventSummary planner={planner} costs={costsState?.identity === identity ? costsState.costs : undefined} pendingSync={pendingSync} />
      <nav aria-label={t("costs.navigation")}>
        <button onClick={onBack} type="button">
          {t("costs.planner")}
        </button>
        <button onClick={onOpenReceipts} type="button">
          {t("costs.receipts")}
        </button>
      </nav>
      <EventCosts
        eventId={eventId}
        organizationId={organizationId}
        planner={planner}
        userId={userId}
        providedCosts={costsState?.identity === identity ? costsState.costs : undefined}
      />
      {planner.lifecycle === "archived" ? (
        <p className="planner-archived" role="status">
          {t("planner.archived")}
        </p>
      ) : null}
      {costsErrorState.identity === identity && costsErrorState.error ? <p role="alert">{t("costs.unavailable")}</p> : null}
    </>
  );
}
