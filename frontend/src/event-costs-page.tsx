import { liveQuery } from "dexie";
import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";

import { EventCosts } from "./event-planner";
import {
  readEventPlanner,
  type EventPlannerProjection,
} from "./planner-projections";

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
  const [errorState, setErrorState] = useState({ identity, error: false });
  // biome-ignore lint/correctness/useExhaustiveDependencies: identity is derived from the listed route dependencies.
  useEffect(() => {
    const effectIdentity = identity;
    const subscription = liveQuery(() =>
      readEventPlanner(userId, organizationId, eventId),
    ).subscribe({
      next: (next) => setPlannerState({ identity: effectIdentity, planner: next }),
      error: () => setErrorState({ identity: effectIdentity, error: true }),
    });
    return () => subscription.unsubscribe();
  }, [eventId, organizationId, userId]);
  const planner = plannerState?.identity === identity ? plannerState.planner : undefined;
  const error = errorState.identity === identity && errorState.error;
  if (!planner && !error) return <p role="status">{t("costs.loading")}</p>;
  if (!planner) return <p role="alert">{t("costs.unavailable")}</p>;
  return (
    <>
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
      />
    </>
  );
}
