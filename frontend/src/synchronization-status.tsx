import { liveQuery } from "dexie";
import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";

import {
  readSynchronizationSummary,
  type SynchronizationSummary,
} from "./local-db";

type StoragePersistenceState = "unavailable" | "denied" | "checkFailed";

let storagePersistenceCheck:
  | Promise<StoragePersistenceState | null>
  | undefined;

function checkPersistentStorage() {
  if (!storagePersistenceCheck) {
    storagePersistenceCheck = (async () => {
      try {
        const storage = navigator.storage;
        if (!storage) return "unavailable";
        if (await storage.persisted()) return null;
        return (await storage.persist()) ? null : "denied";
      } catch {
        return "checkFailed";
      }
    })();
  }
  return storagePersistenceCheck;
}

export function resetPersistentStorageCheckForTests() {
  storagePersistenceCheck = undefined;
}

function browserIsOnline() {
  return typeof navigator === "undefined" || navigator.onLine;
}

function useOnline() {
  const [online, setOnline] = useState(browserIsOnline);

  useEffect(() => {
    const update = () => setOnline(browserIsOnline());
    window.addEventListener("online", update);
    window.addEventListener("offline", update);
    return () => {
      window.removeEventListener("online", update);
      window.removeEventListener("offline", update);
    };
  }, []);

  return online;
}

function useSynchronizationSummary(organizationId?: string, userId?: string) {
  const [summary, setSummary] = useState<SynchronizationSummary | null>(null);
  const [storageUnavailable, setStorageUnavailable] = useState(false);

  useEffect(() => {
    const subscription = liveQuery(() =>
      readSynchronizationSummary(organizationId, userId),
    ).subscribe({
      next: (nextSummary) => {
        setSummary(nextSummary);
        setStorageUnavailable(false);
      },
      error: () => {
        setSummary(null);
        setStorageUnavailable(true);
      },
    });
    return () => subscription.unsubscribe();
  }, [organizationId, userId]);

  return { storageUnavailable, summary };
}

function useStoragePersistenceWarning() {
  const [warning, setWarning] = useState<StoragePersistenceState | null>(null);

  useEffect(() => {
    void checkPersistentStorage().then(setWarning);
  }, []);

  return warning;
}

export function SynchronizationStatus({
  organizationId,
  userId,
}: {
  organizationId?: string;
  userId?: string;
}) {
  const { t } = useTranslation();
  const online = useOnline();
  const { storageUnavailable, summary } = useSynchronizationSummary(
    organizationId,
    userId,
  );
  const storagePersistenceWarning = useStoragePersistenceWarning();
  const pendingCount =
    (summary?.pendingCommands ?? 0) + (summary?.pendingUploads ?? 0);
  const failedCount =
    (summary?.failedCommands ?? 0) + (summary?.failedUploads ?? 0);

  let message = t("synchronization.caughtUp");
  let tone = "caught-up";
  if (storageUnavailable) {
    message = t("synchronization.storageUnavailable");
    tone = "failed";
  } else if (!online) {
    message = t("synchronization.offline");
    tone = "offline";
  } else if (summary?.activity === "upgradeRequired") {
    message = t("synchronization.upgradeRequired");
    tone = "failed";
  } else if (failedCount > 0 || summary?.activity === "blocked") {
    message = t("synchronization.failed", { count: failedCount });
    tone = "failed";
  } else if (summary?.activity === "retrying") {
    message = t("synchronization.retrying");
    tone = "pending";
  } else if (summary?.activity === "syncing") {
    message = t("synchronization.syncing");
    tone = "pending";
  } else if (pendingCount > 0) {
    message = t("synchronization.pending", { count: pendingCount });
    tone = "pending";
  }

  return (
    <aside
      aria-live="polite"
      className={`synchronization-status synchronization-status--${tone}`}
      data-testid="synchronization-status"
      role="status"
    >
      <span>{message}</span>
      {summary?.pendingUploads ? (
        <span className="synchronization-status__detail">
          {t("synchronization.pendingUploads", {
            count: summary.pendingUploads,
          })}
        </span>
      ) : null}
      {summary?.clockSkewWarning ? (
        <span className="synchronization-status__detail">
          {t("synchronization.clockSkew")}
        </span>
      ) : null}
      {storagePersistenceWarning ? (
        <span
          className="synchronization-status__detail"
          data-testid="storage-persistence-warning"
          role="alert"
        >
          {t(`synchronization.storagePersistence.${storagePersistenceWarning}`)}
        </span>
      ) : null}
    </aside>
  );
}
