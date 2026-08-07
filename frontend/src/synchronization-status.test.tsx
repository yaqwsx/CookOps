import { render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import i18n, { defaultLocale } from "./i18n";
import { localDb } from "./local-db";
import { SynchronizationStatus } from "./synchronization-status";

async function clearLocalDatabase() {
  await localDb.transaction(
    "rw",
    localDb.outbox,
    localDb.pendingUploads,
    localDb.syncMetadata,
    async () => {
      await Promise.all([
        localDb.outbox.clear(),
        localDb.pendingUploads.clear(),
        localDb.syncMetadata.clear(),
      ]);
    },
  );
}

function setOnline(value: boolean) {
  Object.defineProperty(navigator, "onLine", {
    configurable: true,
    value,
  });
  window.dispatchEvent(new Event(value ? "online" : "offline"));
}

describe("SynchronizationStatus", () => {
  beforeEach(async () => {
    await i18n.changeLanguage(defaultLocale);
    setOnline(true);
    await clearLocalDatabase();
  });

  afterEach(async () => {
    setOnline(true);
    await clearLocalDatabase();
  });

  it("shows the Czech caught-up state and updates to pending work", async () => {
    render(<SynchronizationStatus organizationId="organization-a" />);

    expect(await screen.findByRole("status")).toHaveTextContent(
      "Synchronizováno",
    );
    await localDb.outbox.add({
      id: "mutation-a",
      userId: "user-a",
      organizationId: "organization-a",
      commandType: "event.update",
      payload: {},
      actionAt: "2026-08-07T10:00:00.000Z",
      createdAt: "2026-08-07T10:00:00.000Z",
      state: "pending",
    });

    await waitFor(() => {
      expect(screen.getByRole("status")).toHaveTextContent("Čekají změny: 1");
    });
  });

  it("shows offline and failed states without hiding pending photo uploads", async () => {
    await localDb.pendingUploads.add({
      id: "upload-a",
      organizationId: "organization-a",
      attachmentId: "receipt-a",
      blob: new Blob(["receipt"], { type: "image/jpeg" }),
      createdAt: "2026-08-07T10:00:00.000Z",
      state: "failed",
      failureReason: "network_error",
    });
    render(<SynchronizationStatus organizationId="organization-a" />);

    await waitFor(() => {
      expect(screen.getByRole("status")).toHaveTextContent(
        "Změny vyžadují pozornost: 1",
      );
    });
    setOnline(false);
    await waitFor(() => {
      expect(screen.getByRole("status")).toHaveTextContent("Bez připojení");
    });
  });
});
