import { liveQuery } from "dexie";
import { useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import {
  eventPriceRefreshPending,
  queueEventPriceRefresh,
} from "./event-price-refresh";

export function EventPriceRefreshControl({
  eventId,
  organizationId,
  userId,
}: {
  eventId: string;
  organizationId: string;
  userId: string;
}) {
  const { t } = useTranslation();
  const identity = `${userId}:${organizationId}:${eventId}`;
  const [pendingState, setPendingState] = useState({
    identity,
    pending: false,
  });
  const [errorState, setErrorState] = useState({ identity, error: false });
  const refreshing = useRef(false);
  useEffect(() => {
    const effectIdentity = identity;
    const subscription = liveQuery(() =>
      eventPriceRefreshPending(userId, organizationId, eventId),
    ).subscribe({
      next: (pending) => setPendingState({ identity: effectIdentity, pending }),
      error: () => setErrorState({ identity: effectIdentity, error: true }),
    });
    return () => subscription.unsubscribe();
  }, [eventId, identity, organizationId, userId]);
  const pending = pendingState.identity === identity && pendingState.pending;
  const error = errorState.identity === identity && errorState.error;
  async function refresh() {
    if (refreshing.current) return;
    refreshing.current = true;
    try {
      await queueEventPriceRefresh(userId, organizationId, eventId);
      setErrorState({ identity, error: false });
    } catch {
      setErrorState({ identity, error: true });
    } finally {
      refreshing.current = false;
    }
  }
  return (
    <div>
      <button disabled={pending} onClick={() => void refresh()} type="button">
        {t(pending ? "costs.refreshPending" : "costs.refresh")}
      </button>
      {error ? <p role="alert">{t("costs.error")}</p> : null}
    </div>
  );
}
