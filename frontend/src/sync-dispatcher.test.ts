import { beforeEach, describe, expect, it, vi } from "vitest";

import { appendOutboxCommand, localDb } from "./local-db";
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
  commandOrganizationId = organizationId,
) {
  await localDb.outbox.add({
    id,
    userId,
    organizationId: commandOrganizationId,
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

  it("keeps a same-millisecond recipe create before its dependent schedule", async () => {
    const createdAt = "2026-08-07T10:00:00.000Z";
    await appendOutboxCommand({
      id: "z-create",
      userId,
      organizationId,
      commandType: "recipe.create",
      payload: {},
      actionAt: createdAt,
      createdAt,
      state: "pending",
    });
    await appendOutboxCommand({
      id: "a-schedule",
      userId,
      organizationId,
      commandType: "scheduled_recipe.schedule",
      payload: {},
      actionAt: createdAt,
      createdAt,
      state: "pending",
    });
    const send = vi.fn<typeof fetch>(async (_input, init) => {
      const commands = JSON.parse(String(init?.body)).commands;
      return response(
        commands.map(
          (command: { mutation_id: string; command_kind: string }) => ({
            mutation_id: command.mutation_id,
            command_kind: command.command_kind,
            status: "accepted",
            error: null,
          }),
        ),
      );
    });

    await dispatchOutbox(organizationId, {
      userId,
      clientInstallationId: installationId,
      fetch: send,
    });

    expect(JSON.parse(String(send.mock.calls[0]?.[1]?.body)).commands).toEqual([
      expect.objectContaining({ mutation_id: "z-create" }),
      expect.objectContaining({ mutation_id: "a-schedule" }),
    ]);
  });

  it("sends ordered commands with the authenticated browser contract and removes accepted work", async () => {
    await addCommand("later", "2026-08-07T10:01:00.000Z");
    await addCommand("first", "2026-08-07T10:00:00.000Z");
    await localDb.syncMetadata.add({
      userId,
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
      localDb.syncMetadata.get([userId, organizationId]),
    ).resolves.toMatchObject({
      activity: "caughtUp",
      cursor: "durable-cursor",
      changeCursorHint: "change-cursor",
      lastSuccessfulServerContact: "2026-08-07T12:00:00.000Z",
    });
  });

  it("isolates an organization's outbox commands", async () => {
    await addCommand("organization-a-command", "2026-08-07T10:00:00.000Z");
    await addCommand(
      "organization-b-command",
      "2026-08-07T10:01:00.000Z",
      {},
      "organization-b",
    );
    const originalB = await localDb.outbox.get("organization-b-command");
    expect(originalB).toBeDefined();
    const send = vi.fn<typeof fetch>(async (_input, init) => {
      const commands = JSON.parse(String(init?.body)).commands;
      return response(
        commands.map((command: { mutation_id: string; command_kind: string }) => ({
          mutation_id: command.mutation_id,
          command_kind: command.command_kind,
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

    expect(JSON.parse(String(send.mock.calls[0]?.[1]?.body))).toMatchObject({
      organization_id: organizationId,
      commands: [expect.objectContaining({ mutation_id: "organization-a-command" })],
    });
    expect(JSON.parse(String(send.mock.calls[0]?.[1]?.body)).commands).toHaveLength(1);
    await expect(localDb.outbox.get("organization-a-command")).resolves.toBeUndefined();
    await expect(localDb.outbox.get("organization-b-command")).resolves.toEqual(originalB);
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
      localDb.syncMetadata.get([userId, organizationId]),
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
      localDb.syncMetadata.get([userId, organizationId]),
    ).resolves.toMatchObject({
      activity: "retrying",
    });
  });

  it("retries a 503 with the unchanged command and clears it after acceptance", async () => {
    await addCommand("retry-me", "2026-08-07T10:00:00.000Z", {
      title: "Retry this command",
      count: 2,
    });
    const send = vi.fn<typeof fetch>(async (_input, init) => {
      if (send.mock.calls.length === 1) return new Response(null, { status: 503 });
      return response([
        {
          mutation_id: "retry-me",
          command_kind: "event.create",
          status: "accepted",
          error: null,
        },
      ]);
    });
    const options = {
      userId,
      clientInstallationId: installationId,
      fetch: send,
    };

    await expect(dispatchOutbox(organizationId, options)).rejects.toThrow(
      "Sync push failed.",
    );
    await dispatchOutbox(organizationId, options);

    expect(send).toHaveBeenCalledTimes(2);
    const requests = send.mock.calls.map((call) =>
      JSON.parse(String(call[1]?.body)).commands,
    );
    expect(requests[0]).toEqual(requests[1]);
    expect(requests[1]).toEqual([
      {
        mutation_id: "retry-me",
        command_kind: "event.create",
        command_schema_version: 1,
        client_wall_time: "2026-08-07T10:00:00.000Z",
        payload: { title: "Retry this command", count: 2 },
      },
    ]);
    await expect(localDb.outbox.count()).resolves.toBe(0);
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
