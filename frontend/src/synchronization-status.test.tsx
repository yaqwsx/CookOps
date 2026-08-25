import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import i18n, { defaultLocale } from "./i18n";
import { localDb } from "./local-db";
import {
  resetPersistentStorageCheckForTests,
  SynchronizationStatus,
} from "./synchronization-status";

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

type StorageStub = Pick<StorageManager, "persisted" | "persist">;
const originalStorageDescriptor = Object.getOwnPropertyDescriptor(
  navigator,
  "storage",
);

function setStorage(storage: StorageStub | undefined) {
  Object.defineProperty(navigator, "storage", {
    configurable: true,
    value: storage,
  });
}

function restoreStorage() {
  Reflect.deleteProperty(navigator, "storage");
  if (originalStorageDescriptor) {
    Object.defineProperty(navigator, "storage", originalStorageDescriptor);
  }
}

describe("SynchronizationStatus", () => {
  beforeEach(async () => {
    await i18n.changeLanguage(defaultLocale);
    setOnline(true);
    await clearLocalDatabase();
    resetPersistentStorageCheckForTests();
    restoreStorage();
  });

  afterEach(async () => {
    setOnline(true);
    await clearLocalDatabase();
    resetPersistentStorageCheckForTests();
    restoreStorage();
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
      userId: "user-a",
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

  it("opens scoped rejected work without showing payload and supports export or confirmed discard", async () => {
    await localDb.outbox.bulkAdd([
      {
        id: "failed-a",
        userId: "user-a",
        organizationId: "organization-a",
        commandType: "event.update",
        payload: { secret: "hidden" },
        actionAt: "2026-08-07T10:00:00.000Z",
        createdAt: "2026-08-07T10:00:00.000Z",
        state: "failed",
        failureReason: "stale_precondition",
      },
      {
        id: "failed-b",
        userId: "user-a",
        organizationId: "organization-b",
        commandType: "event.update",
        payload: { secret: "other" },
        actionAt: "2026-08-07T10:00:00.000Z",
        createdAt: "2026-08-07T10:00:00.000Z",
        state: "failed",
      },
    ]);
    const createObjectURL = vi
      .spyOn(URL, "createObjectURL")
      .mockReturnValue("blob:test");
    const revokeObjectURL = vi
      .spyOn(URL, "revokeObjectURL")
      .mockImplementation(() => {});
    const click = vi
      .spyOn(HTMLAnchorElement.prototype, "click")
      .mockImplementation(() => {});
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);
    render(
      <SynchronizationStatus organizationId="organization-a" userId="user-a" />,
    );
    fireEvent.click(
      await screen.findByRole("button", {
        name: "Zobrazit odmítnuté změny (1)",
      }),
    );
    const opener = screen.getByRole("button", {
      name: "Zobrazit odmítnuté změny (1)",
    });
    const dialog = screen.getByRole("dialog");
    expect(dialog).toHaveTextContent("stale_precondition");
    expect(dialog).not.toHaveTextContent("hidden");
    fireEvent.click(screen.getByRole("button", { name: "Exportovat JSON" }));
    expect(createObjectURL).toHaveBeenCalled();
    expect(await localDb.outbox.get("failed-a")).toBeDefined();
    fireEvent.click(screen.getByRole("button", { name: "Zavřít" }));
    expect(opener).toHaveFocus();
    fireEvent.click(
      screen.getByRole("button", { name: "Zobrazit odmítnuté změny (1)" }),
    );
    fireEvent.click(screen.getByRole("button", { name: "Zahodit" }));
    expect(confirm).toHaveBeenCalled();
    expect(await localDb.outbox.get("failed-a")).toBeDefined();
    confirm.mockReturnValue(true);
    fireEvent.click(screen.getByRole("button", { name: "Zahodit" }));
    await waitFor(async () =>
      expect(await localDb.outbox.get("failed-a")).toBeUndefined(),
    );
    expect(await localDb.outbox.get("failed-b")).toBeDefined();
    createObjectURL.mockRestore();
    revokeObjectURL.mockRestore();
    click.mockRestore();
    confirm.mockRestore();
  });

  it.each([
    ["unavailable", undefined, "Trvalé úložiště není podporováno."],
    [
      "denied",
      {
        persisted: async (): Promise<boolean> => false,
        persist: async (): Promise<boolean> => false,
      },
      "Trvalé úložiště nebylo povoleno.",
    ],
    [
      "check failure",
      {
        persisted: async (): Promise<boolean> => {
          throw new Error("storage check failed");
        },
        persist: async (): Promise<boolean> => false,
      },
      "Trvalé úložiště se nepodařilo ověřit.",
    ],
  ] as const)(
    "warns when storage persistence is %s",
    async (_, storage, message) => {
      setStorage(storage);
      render(<SynchronizationStatus organizationId="organization-a" />);

      expect(
        await screen.findByTestId("storage-persistence-warning"),
      ).toHaveTextContent(message);
    },
  );

  it("does not warn when storage is already persisted or becomes persisted", async () => {
    const persist = vi.fn(async (): Promise<boolean> => true);
    setStorage({ persisted: async (): Promise<boolean> => false, persist });
    render(<SynchronizationStatus organizationId="organization-a" />);
    await waitFor(() => expect(persist).toHaveBeenCalledTimes(1));
    expect(screen.queryByTestId("storage-persistence-warning")).toBeNull();

    resetPersistentStorageCheckForTests();
    setStorage({ persisted: async (): Promise<boolean> => true, persist });
    render(<SynchronizationStatus organizationId="organization-a" />);
    await waitFor(() =>
      expect(screen.queryByTestId("storage-persistence-warning")).toBeNull(),
    );
    expect(persist).toHaveBeenCalledTimes(1);
  });
});
