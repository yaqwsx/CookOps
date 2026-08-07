import { liveQuery } from "dexie";
import { useCallback, useEffect, useRef } from "react";

import { localDb } from "./local-db";
import { dispatchOutbox } from "./sync-dispatcher";

export const SYNC_RETRY_DELAY_MS = 5_000;
const syncLockName = "cookops-outbox-sync";

function browserIsOnline() {
  return typeof navigator === "undefined" || navigator.onLine;
}

/** Dispatch pending work whenever an authenticated browser can reach the server. */
export function useOutboxSynchronization(userId: string) {
  const active = useRef(true);
  const generation = useRef(0);
  const running = useRef(false);
  const rerun = useRef(false);
  const retry = useRef<number | undefined>(undefined);
  const retrySynchronization = useRef<() => void>(() => undefined);

  const dispatchPending = useCallback(
    async (currentGeneration: number) => {
      if (
        !active.current ||
        currentGeneration !== generation.current ||
        !browserIsOnline()
      )
        return;
      if (running.current) {
        rerun.current = true;
        return;
      }
      running.current = true;
      try {
        const pending = await localDb.outbox
          .where("[userId+state]")
          .equals([userId, "pending"])
          .toArray();
        const organizationIds = [
          ...new Set(pending.map((command) => command.organizationId)),
        ].sort();
        for (const organizationId of organizationIds) {
          if (!active.current || currentGeneration !== generation.current)
            return;
          await dispatchOutbox(organizationId, { userId });
        }
      } catch {
        if (!active.current || currentGeneration !== generation.current) return;
        retry.current = window.setTimeout(
          retrySynchronization.current,
          SYNC_RETRY_DELAY_MS,
        );
      } finally {
        running.current = false;
        if (rerun.current && active.current) {
          rerun.current = false;
          retrySynchronization.current();
        }
      }
    },
    [userId],
  );

  const synchronize = useCallback(async () => {
    const currentGeneration = generation.current;
    if (!navigator.locks) return dispatchPending(currentGeneration);
    await navigator.locks.request(
      syncLockName,
      { ifAvailable: true },
      async (lock) => {
        if (lock && currentGeneration === generation.current) {
          await dispatchPending(currentGeneration);
        }
      },
    );
  }, [dispatchPending]);
  retrySynchronization.current = () => void synchronize();

  useEffect(() => {
    active.current = true;
    const subscription = liveQuery(() => localDb.outbox.toArray()).subscribe({
      next: () => void synchronize(),
    });
    window.addEventListener("online", synchronize);
    return () => {
      active.current = false;
      generation.current += 1;
      subscription.unsubscribe();
      window.removeEventListener("online", synchronize);
      if (retry.current !== undefined) window.clearTimeout(retry.current);
    };
  }, [synchronize]);
}
