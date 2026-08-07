import { beforeEach, describe, expect, it, vi } from "vitest";

import { localDb } from "./local-db";
import { dispatchOutbox } from "./sync-dispatcher";

const organizationId = "organization-a";
const installationId = "installation-a";
const userId = "user-a";

async function clearLocalDatabase() {
  await localDb.transaction(
    "rw",
    localDb.outbox,
    localDb.syncMetadata,
    async () => {
      await Promise.all([localDb.outbox.clear(), localDb.syncMetadata.clear()]);
    },
  );
}

async function addCommand(
  id: string,
  createdAt: string,
  payload: Record<string, unknown> = {},
) {
  await localDb.outbox.add({
    id,
    userId,
    organizationId,
    commandType: "event.create",
    payload,
    actionAt: createdAt,
    createdAt,
    state: "pending",
  });
}

function response(outcomes: object[]) {
  return new Response(
    JSON.stringify({
      sync_schema_version: 1,
      server_time: "2026-08-07T12:00:00.000Z",
      change_cursor: "change-cursor",
      clock_skew_warning: null,
      outcomes,
    }),
    { status: 200, headers: { "content-type": "application/json" } },
  );
}

describe("dispatchOutbox", () => {
  beforeEach(clearLocalDatabase);

  it("sends ordered commands with the authenticated browser contract and removes accepted work", async () => {
    await addCommand("later", "2026-08-07T10:01:00.000Z");
    await addCommand("first", "2026-08-07T10:00:00.000Z");
    await localDb.syncMetadata.add({
      organizationId,
      activity: "caughtUp",
      cursor: "durable-cursor",
    });
    const send = vi.fn<typeof fetch>(async () =>
      response([
        {
          mutation_id: "first",
          command_kind: "event.create",
          status: "accepted",
          error: null,
        },
        {
          mutation_id: "later",
          command_kind: "event.create",
          status: "partially_superseded",
          error: null,
        },
      ]),
    );

    await dispatchOutbox(organizationId, {
      userId,
      clientInstallationId: installationId,
      fetch: send,
      now: () => new Date("2026-08-07T11:00:00.000Z"),
    });

    expect(send).toHaveBeenCalledOnce();
    expect(JSON.parse(String(send.mock.calls[0]?.[1]?.body))).toMatchObject({
      organization_id: organizationId,
      client_installation_id: installationId,
      request_sent_at: "2026-08-07T11:00:00.000Z",
      sync_schema_version: 1,
      commands: [
        { mutation_id: "first", command_kind: "event.create" },
        { mutation_id: "later", command_kind: "event.create" },
      ],
    });
    expect(await localDb.outbox.count()).toBe(0);
    await expect(
      localDb.syncMetadata.get(organizationId),
    ).resolves.toMatchObject({
      activity: "caughtUp",
      cursor: "durable-cursor",
      changeCursorHint: "change-cursor",
      lastSuccessfulServerContact: "2026-08-07T12:00:00.000Z",
    });
  });

  it("keeps a rejected intent as recoverable work while accepting later commands", async () => {
    await addCommand("rejected", "2026-08-07T10:00:00.000Z");
    await addCommand("accepted", "2026-08-07T10:01:00.000Z");
    const send = vi.fn<typeof fetch>(async () =>
      response([
        {
          mutation_id: "rejected",
          command_kind: "event.create",
          status: "rejected",
          error: { code: "stale_precondition" },
        },
        {
          mutation_id: "accepted",
          command_kind: "event.create",
          status: "accepted",
          error: null,
        },
      ]),
    );

    await dispatchOutbox(organizationId, {
      userId,
      clientInstallationId: installationId,
      fetch: send,
    });

    await expect(localDb.outbox.get("rejected")).resolves.toMatchObject({
      state: "failed",
      failureReason: "stale_precondition",
    });
    await expect(localDb.outbox.get("accepted")).resolves.toBeUndefined();
    await expect(
      localDb.syncMetadata.get(organizationId),
    ).resolves.toMatchObject({
      activity: "blocked",
    });
  });

  it("splits more than one hundred commands without reordering them", async () => {
    for (let index = 0; index < 101; index += 1) {
      await addCommand(
        `command-${String(index).padStart(3, "0")}`,
        `2026-08-07T10:${String(Math.floor(index / 60)).padStart(2, "0")}:${String(index % 60).padStart(2, "0")}.000Z`,
      );
    }
    const send = vi.fn<typeof fetch>(async (_input, init) => {
      const { commands } = JSON.parse(String(init?.body)) as {
        commands: { mutation_id: string; command_kind: string }[];
      };
      return response(
        commands.map((command) => ({
          ...command,
          status: "accepted",
          error: null,
        })),
      );
    });

    await dispatchOutbox(organizationId, {
      userId,
      clientInstallationId: installationId,
      fetch: send,
    });

    expect(send).toHaveBeenCalledTimes(2);
    expect(
      JSON.parse(String(send.mock.calls[0]?.[1]?.body)).commands,
    ).toHaveLength(100);
    expect(JSON.parse(String(send.mock.calls[1]?.[1]?.body)).commands).toEqual([
      expect.objectContaining({ mutation_id: "command-100" }),
    ]);
  });

  it("retains every pending command when transport outcomes are unknown", async () => {
    await addCommand("pending", "2026-08-07T10:00:00.000Z");
    const send = vi.fn<typeof fetch>(
      async () => new Response(null, { status: 503 }),
    );

    await expect(
      dispatchOutbox(organizationId, {
        userId,
        clientInstallationId: installationId,
        fetch: send,
      }),
    ).rejects.toThrow("Sync push failed.");

    await expect(localDb.outbox.get("pending")).resolves.toMatchObject({
      state: "pending",
    });
    await expect(
      localDb.syncMetadata.get(organizationId),
    ).resolves.toMatchObject({
      activity: "retrying",
    });
  });

  it("keeps work pending when a response does not preserve command order", async () => {
    await addCommand("first", "2026-08-07T10:00:00.000Z");
    await addCommand("later", "2026-08-07T10:01:00.000Z");
    const send = vi.fn<typeof fetch>(async () =>
      response([
        {
          mutation_id: "later",
          command_kind: "event.create",
          status: "accepted",
          error: null,
        },
        {
          mutation_id: "first",
          command_kind: "event.create",
          status: "accepted",
          error: null,
        },
      ]),
    );

    await expect(
      dispatchOutbox(organizationId, {
        userId,
        clientInstallationId: installationId,
        fetch: send,
      }),
    ).rejects.toThrow("Invalid sync response.");

    await expect(localDb.outbox.count()).resolves.toBe(2);
  });

  it("does not bypass the decoded payload limit for one oversized command", async () => {
    await addCommand("too-large", "2026-08-07T10:00:00.000Z", {
      note: "x".repeat(1024 * 1024),
    });
    const send = vi.fn();

    await dispatchOutbox(organizationId, {
      userId,
      clientInstallationId: installationId,
      fetch: send,
    });

    expect(send).not.toHaveBeenCalled();
    await expect(localDb.outbox.get("too-large")).resolves.toMatchObject({
      state: "failed",
      failureReason: "command_too_large",
    });
  });
});
