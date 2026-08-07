import { render, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { localDb } from "./local-db";
import {
  SYNC_RETRY_DELAY_MS,
  useOutboxSynchronization,
} from "./sync-lifecycle";

const dispatchOutbox = vi.hoisted(() => vi.fn());

vi.mock("./sync-dispatcher", () => ({ dispatchOutbox }));

function Lifecycle({ userId }: { userId: string }) {
  useOutboxSynchronization(userId);
  return null;
}

async function addPendingCommand(
  organizationId = "organization-a",
  userId = "user-a",
) {
  await localDb.outbox.add({
    id: crypto.randomUUID(),
    userId,
    organizationId,
    commandType: "event.create",
    payload: {},
    actionAt: "2026-08-07T10:00:00.000Z",
    createdAt: "2026-08-07T10:00:00.000Z",
    state: "pending",
  });
}

function setOnline(value: boolean) {
  Object.defineProperty(navigator, "onLine", { configurable: true, value });
  window.dispatchEvent(new Event(value ? "online" : "offline"));
}

describe("authenticated outbox synchronization lifecycle", () => {
  beforeEach(async () => {
    dispatchOutbox.mockReset();
    await localDb.outbox.clear();
    setOnline(true);
  });

  it("delivers all pending organization work for the authenticated identity", async () => {
    await addPendingCommand("organization-b");
    await addPendingCommand("organization-a");
    render(<Lifecycle userId="user-a" />);

    await waitFor(() =>
      expect(dispatchOutbox).toHaveBeenNthCalledWith(1, "organization-a", {
        userId: "user-a",
      }),
    );
    expect(dispatchOutbox).toHaveBeenNthCalledWith(2, "organization-b", {
      userId: "user-a",
    });
  });

  it("waits offline and retries delivery when connectivity returns", async () => {
    setOnline(false);
    await addPendingCommand();
    render(<Lifecycle userId="user-a" />);
    expect(dispatchOutbox).not.toHaveBeenCalled();

    setOnline(true);
    await waitFor(() =>
      expect(dispatchOutbox).toHaveBeenCalledWith("organization-a", {
        userId: "user-a",
      }),
    );
  });

  it("uses the browser-wide lock when it is available", async () => {
    const request = vi.fn(async (_name, _options, callback) => callback({}));
    Object.defineProperty(navigator, "locks", {
      configurable: true,
      value: { request },
    });
    await addPendingCommand();
    render(<Lifecycle userId="user-a" />);

    await waitFor(() => expect(request).toHaveBeenCalledOnce());
    expect(request).toHaveBeenCalledWith(
      "cookops-outbox-sync",
      { ifAvailable: true },
      expect.any(Function),
    );
    Object.defineProperty(navigator, "locks", {
      configurable: true,
      value: undefined,
    });
  });

  it("does not enter a delayed previous user's lock after an account switch", async () => {
    let enter: ((lock: object) => Promise<void>) | undefined;
    Object.defineProperty(navigator, "locks", {
      configurable: true,
      value: {
        request: vi.fn(async (_name, _options, callback) => {
          enter = callback;
        }),
      },
    });
    await addPendingCommand("organization-a", "user-a");
    const { rerender } = render(<Lifecycle userId="user-a" />);

    await waitFor(() => expect(enter).toBeDefined());
    rerender(<Lifecycle userId="user-b" />);
    await enter?.({});
    expect(dispatchOutbox).not.toHaveBeenCalled();
    Object.defineProperty(navigator, "locks", {
      configurable: true,
      value: undefined,
    });
  });

  it("does not deliver a previous browser user's retained intent", async () => {
    await addPendingCommand("organization-a", "user-a");
    await addPendingCommand("organization-b", "user-b");
    render(<Lifecycle userId="user-b" />);

    await waitFor(() =>
      expect(dispatchOutbox).toHaveBeenCalledWith("organization-b", {
        userId: "user-b",
      }),
    );
    expect(dispatchOutbox).not.toHaveBeenCalledWith("organization-a", {
      userId: "user-b",
    });
  });

  it("stops an in-flight user's remaining work after an account switch", async () => {
    let release: (() => void) | undefined;
    dispatchOutbox.mockImplementationOnce(
      () => new Promise<void>((resolve) => (release = resolve)),
    );
    await addPendingCommand("organization-a");
    await addPendingCommand("organization-c");
    const { rerender } = render(<Lifecycle userId="user-a" />);

    await waitFor(() => expect(dispatchOutbox).toHaveBeenCalledOnce());
    rerender(<Lifecycle userId="user-b" />);
    release?.();
    await new Promise((resolve) => setTimeout(resolve));
    expect(dispatchOutbox).toHaveBeenCalledOnce();
  });

  it("schedules a transient failure for retry and cancels it at unmount", async () => {
    const setTimeoutMock = vi.spyOn(window, "setTimeout");
    const clearTimeoutMock = vi.spyOn(window, "clearTimeout");
    dispatchOutbox.mockRejectedValueOnce(new Error("temporary failure"));
    await addPendingCommand();
    const { unmount } = render(<Lifecycle userId="user-a" />);

    await waitFor(() => expect(dispatchOutbox).toHaveBeenCalledOnce());
    await waitFor(() =>
      expect(setTimeoutMock).toHaveBeenCalledWith(
        expect.any(Function),
        SYNC_RETRY_DELAY_MS,
      ),
    );
    unmount();
    expect(clearTimeoutMock).toHaveBeenCalled();
  });
});
