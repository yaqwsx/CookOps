import { render, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { localDb } from "./local-db";
import {
  SYNC_RETRY_DELAY_MS,
  useOutboxSynchronization,
} from "./sync-lifecycle";

const dispatchOutbox = vi.hoisted(() => vi.fn());
const pullOrganization = vi.hoisted(() => vi.fn(async () => false));

class HintSocket {
  static instances: HintSocket[] = [];
  onopen: (() => void) | null = null;
  onmessage: ((event: MessageEvent) => void) | null = null;
  onclose: (() => void) | null = null;
  readonly sent: string[] = [];

  constructor(readonly url: string) {
    HintSocket.instances.push(this);
  }

  send(value: string) {
    this.sent.push(value);
  }

  close() {}

  disconnect() {
    this.onclose?.();
  }

  open() {
    this.onopen?.();
  }

  message(value: unknown) {
    this.onmessage?.(new MessageEvent("message", { data: value }));
  }
}

vi.mock("./sync-dispatcher", () => ({ dispatchOutbox }));
vi.mock("./sync-bootstrap", () => ({ pullOrganization }));

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
    pullOrganization.mockClear();
    HintSocket.instances = [];
    await Promise.all([localDb.outbox.clear(), localDb.syncMetadata.clear()]);
    setOnline(true);
  });

  it("subscribes cached organizations and turns valid hints into the existing pull loop", async () => {
    Object.defineProperty(window, "WebSocket", {
      configurable: true,
      value: HintSocket,
    });
    await localDb.syncMetadata.add({
      userId: "user-a",
      organizationId: "organization-a",
      activity: "caughtUp",
      cursor: "a",
    });
    render(<Lifecycle userId="user-a" />);

    await waitFor(() => expect(HintSocket.instances).toHaveLength(1));
    const socket = HintSocket.instances[0];
    socket.open();
    expect(socket.url).toContain("/api/v1/sync/hints");
    expect(socket.sent).toEqual([
      JSON.stringify({
        type: "subscribe",
        organization_ids: ["organization-a"],
      }),
    ]);

    await waitFor(() => expect(pullOrganization).toHaveBeenCalled());
    pullOrganization.mockClear();
    socket.message(
      JSON.stringify({
        type: "change_available",
        organization_id: "organization-a",
        cursor: "v1.cursor",
      }),
    );
    await waitFor(() => expect(pullOrganization).toHaveBeenCalled());
    pullOrganization.mockClear();
    socket.message(
      JSON.stringify({
        type: "change_available",
        organization_id: "organization-other",
        cursor: "v1.cursor",
      }),
    );
    await new Promise((resolve) => setTimeout(resolve));
    expect(pullOrganization).not.toHaveBeenCalled();
  });

  it("splits more than twenty cached organizations into bounded subscriptions", async () => {
    Object.defineProperty(window, "WebSocket", {
      configurable: true,
      value: HintSocket,
    });
    await localDb.syncMetadata.bulkAdd(
      Array.from({ length: 21 }, (_, index) => ({
        userId: "user-a",
        organizationId: `organization-${index.toString().padStart(2, "0")}`,
        activity: "caughtUp" as const,
        cursor: `${index}`,
      })),
    );
    render(<Lifecycle userId="user-a" />);

    await waitFor(() => expect(HintSocket.instances).toHaveLength(2));
    HintSocket.instances[0].open();
    await waitFor(() => expect(pullOrganization).toHaveBeenCalled());
    pullOrganization.mockClear();
    HintSocket.instances[1].open();
    await waitFor(() => expect(pullOrganization).toHaveBeenCalled());
    expect(
      JSON.parse(HintSocket.instances[0].sent[0]).organization_ids,
    ).toHaveLength(20);
    expect(
      JSON.parse(HintSocket.instances[1].sent[0]).organization_ids,
    ).toHaveLength(1);
  });

  it("rebuilds offline-expired hint subscriptions when connectivity returns", async () => {
    Object.defineProperty(window, "WebSocket", {
      configurable: true,
      value: HintSocket,
    });
    await localDb.syncMetadata.add({
      userId: "user-a",
      organizationId: "organization-a",
      activity: "caughtUp",
      cursor: "a",
    });
    render(<Lifecycle userId="user-a" />);
    await waitFor(() => expect(HintSocket.instances).toHaveLength(1));
    HintSocket.instances[0].open();
    setOnline(false);
    const setTimeoutMock = vi.spyOn(window, "setTimeout");
    HintSocket.instances[0].disconnect();
    await waitFor(() =>
      expect(setTimeoutMock).toHaveBeenCalledWith(
        expect.any(Function),
        SYNC_RETRY_DELAY_MS,
      ),
    );
    const retry = setTimeoutMock.mock.calls.find(
      (call) => call[1] === SYNC_RETRY_DELAY_MS,
    )?.[0] as () => void;
    retry();
    expect(HintSocket.instances).toHaveLength(1);

    pullOrganization.mockClear();
    setOnline(true);
    await waitFor(() => expect(HintSocket.instances).toHaveLength(2));
    HintSocket.instances[1].open();
    expect(HintSocket.instances[1].sent).toEqual([
      JSON.stringify({
        type: "subscribe",
        organization_ids: ["organization-a"],
      }),
    ]);
    await waitFor(() => expect(pullOrganization).toHaveBeenCalled());
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
    expect(pullOrganization).toHaveBeenCalledWith("user-a", "organization-a");
  });

  it("pulls every cached organization for the authenticated user on reconnect", async () => {
    await localDb.syncMetadata.bulkAdd([
      {
        userId: "user-a",
        organizationId: "organization-a",
        activity: "caughtUp",
        cursor: "a",
      },
      {
        userId: "user-b",
        organizationId: "organization-b",
        activity: "caughtUp",
        cursor: "b",
      },
    ]);
    render(<Lifecycle userId="user-a" />);

    await waitFor(() =>
      expect(pullOrganization).toHaveBeenCalledWith("user-a", "organization-a"),
    );
    expect(pullOrganization).not.toHaveBeenCalledWith(
      "user-a",
      "organization-b",
    );
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

  it("retries a change hint after another tab releases the sync lock", async () => {
    const request = vi.fn(async (_name, _options, callback) => callback(null));
    Object.defineProperty(navigator, "locks", {
      configurable: true,
      value: { request },
    });
    const setTimeoutMock = vi.spyOn(window, "setTimeout");
    await addPendingCommand();
    render(<Lifecycle userId="user-a" />);

    await waitFor(() =>
      expect(setTimeoutMock).toHaveBeenCalledWith(
        expect.any(Function),
        SYNC_RETRY_DELAY_MS,
      ),
    );
    expect(dispatchOutbox).not.toHaveBeenCalled();
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
