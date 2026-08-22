import { liveQuery } from "dexie";
import { useCallback, useEffect, useRef } from "react";

import { localDb } from "./local-db";
import { dispatchOutbox } from "./sync-dispatcher";
import { pullOrganization } from "./sync-bootstrap";
import { dispatchReceiptUploads } from "./receipt-media";
import { isUpgradeRequiredError } from "./sync-errors";

export const SYNC_RETRY_DELAY_MS = 5_000;
const syncLockName = "cookops-outbox-sync";
const maxHintOrganizations = 20;

type ChangeHint = {
  type: "change_available" | "access_changed";
  organization_id: string;
  cursor?: string;
};

function browserIsOnline() {
  return typeof navigator === "undefined" || navigator.onLine;
}

function hintSocketUrl() {
  const url = new URL("/api/v1/sync/hints", window.location.href);
  url.protocol = url.protocol === "https:" ? "wss:" : "ws:";
  return url.toString();
}

function parseChangeHint(value: unknown, organizationIds: Set<string>) {
  if (typeof value !== "string") return undefined;
  try {
    const hint = JSON.parse(value) as ChangeHint;
    if (
      (hint.type !== "change_available" && hint.type !== "access_changed") ||
      typeof hint.organization_id !== "string" ||
      !organizationIds.has(hint.organization_id) ||
      (hint.cursor !== undefined && typeof hint.cursor !== "string")
    )
      return undefined;
    return hint;
  } catch {
    return undefined;
  }
}

/** Dispatch pending work whenever an authenticated browser can reach the server. */
export function useOutboxSynchronization(userId: string, onUnauthenticated?: () => void) {
  const active = useRef(true);
  const generation = useRef(0);
  const running = useRef(false);
  const rerun = useRef(false);
  const retry = useRef<number | undefined>(undefined);
  const retrySynchronization = useRef<() => void>(() => undefined);

  const scheduleRetry = useCallback(() => {
    if (retry.current !== undefined) return;
    retry.current = window.setTimeout(() => {
      retry.current = undefined;
      retrySynchronization.current();
    }, SYNC_RETRY_DELAY_MS);
  }, []);

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
        const [pending, metadata] = await Promise.all([
          localDb.outbox
            .where("[userId+state]")
            .equals([userId, "pending"])
            .toArray(),
          localDb.syncMetadata.where("userId").equals(userId).toArray(),
        ]);
        const organizationIds = [
          ...new Set([
            ...pending.map((command) => command.organizationId),
            ...metadata.map((entry) => entry.organizationId),
          ]),
        ].sort();
        for (const organizationId of organizationIds) {
          if (!active.current || currentGeneration !== generation.current)
            return;
          if (
            (await localDb.syncMetadata.get([userId, organizationId]))
              ?.activity === "upgradeRequired"
          )
            continue;
          try {
            await pullOrganization(userId, organizationId);
          } catch (error) {
            if (isUpgradeRequiredError(error)) continue;
            throw error;
          }
          if (!active.current || currentGeneration !== generation.current)
            return;
          try {
            await dispatchOutbox(organizationId, { userId });
          } catch (error) {
            if (isUpgradeRequiredError(error)) continue;
            throw error;
          }
          if (!active.current || currentGeneration !== generation.current)
            return;
          await dispatchReceiptUploads(userId, organizationId);
          if (!active.current || currentGeneration !== generation.current)
            return;
          while (await pullOrganization(userId, organizationId)) {
            if (!active.current || currentGeneration !== generation.current)
              return;
          }
        }
      } catch (error) {
        if (!active.current || currentGeneration !== generation.current) return;
        if (error instanceof Error && (error as Error & { status?: number }).status === 401) {
          onUnauthenticated?.();
          return;
        }
        scheduleRetry();
      } finally {
        running.current = false;
        if (rerun.current && active.current) {
          rerun.current = false;
          retrySynchronization.current();
        }
      }
    },
    [onUnauthenticated, scheduleRetry, userId],
  );

  const synchronize = useCallback(async () => {
    const currentGeneration = generation.current;
    if (!navigator.locks) return dispatchPending(currentGeneration);
    let acquired = false;
    await navigator.locks.request(
      syncLockName,
      { ifAvailable: true },
      async (lock) => {
        if (lock && currentGeneration === generation.current) {
          acquired = true;
          await dispatchPending(currentGeneration);
        }
      },
    );
    if (active.current && currentGeneration === generation.current && !acquired)
      scheduleRetry();
  }, [dispatchPending, scheduleRetry]);
  retrySynchronization.current = () => void synchronize();

  useEffect(() => {
    active.current = true;
    const subscription = liveQuery(() =>
      Promise.all([localDb.outbox.toArray(), localDb.pendingUploads.toArray()]),
    ).subscribe({
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

  useEffect(() => {
    let disposed = false;
    let sockets: WebSocket[] = [];
    const reconnects: number[] = [];
    let subscribedKey = "";
    let desiredGroups: string[][] = [];
    const closeSockets = () => {
      reconnects.splice(0).forEach((timer) => {
        window.clearTimeout(timer);
      });
      const previousSockets = sockets;
      sockets = [];
      previousSockets.forEach((socket) => {
        socket.close();
      });
    };
    const connect = (organizations: string[]) => {
      if (disposed || !browserIsOnline()) return;
      const next = new WebSocket(hintSocketUrl());
      sockets.push(next);
      const subscribedOrganizations = new Set(organizations);
      next.onopen = () => {
        next.send(
          JSON.stringify({
            type: "subscribe",
            organization_ids: organizations,
          }),
        );
        void synchronize();
      };
      next.onmessage = (event) => {
        if (parseChangeHint(event.data, subscribedOrganizations))
          void synchronize();
      };
      next.onclose = () => {
        const current = sockets.includes(next);
        sockets = sockets.filter((socket) => socket !== next);
        if (!disposed && current)
          reconnects.push(
            window.setTimeout(
              () => connect(organizations),
              SYNC_RETRY_DELAY_MS,
            ),
          );
      };
    };
    const reconnectDesired = () => {
      if (disposed || !browserIsOnline()) return;
      closeSockets();
      desiredGroups.forEach((organizations) => {
        connect(organizations);
      });
    };
    window.addEventListener("online", reconnectDesired);
    const subscription = liveQuery(() =>
      localDb.syncMetadata.where("userId").equals(userId).toArray(),
    ).subscribe({
      next: (metadata) => {
        const organizationIds = [
          ...new Set(metadata.map((entry) => entry.organizationId)),
        ].sort();
        const nextKey = organizationIds.join(",");
        if (nextKey === subscribedKey) return;
        subscribedKey = nextKey;
        desiredGroups = [];
        for (
          let start = 0;
          start < organizationIds.length;
          start += maxHintOrganizations
        )
          desiredGroups.push(
            organizationIds.slice(start, start + maxHintOrganizations),
          );
        closeSockets();
        if (
          disposed ||
          organizationIds.length === 0 ||
          typeof WebSocket === "undefined"
        )
          return;
        desiredGroups.forEach((organizations) => {
          connect(organizations);
        });
      },
    });
    return () => {
      disposed = true;
      subscription.unsubscribe();
      window.removeEventListener("online", reconnectDesired);
      closeSockets();
    };
  }, [synchronize, userId]);
}
