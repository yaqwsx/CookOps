import assert from "node:assert/strict";
import { randomUUID } from "node:crypto";
import { once } from "node:events";
import { createServer } from "node:http";
import test from "node:test";

import type { Interaction } from "oidc-provider";
import { Pool } from "pg";

import {
  InteractionApprovalStore,
  initializeInteractionApprovals,
  interactionBinding,
  privateCredentialMatches,
} from "./interaction-approvals.js";
import { handleInteractionBridgeRequest } from "./interaction-bridge.js";

const APPROVAL_SECRET = Buffer.alloc(32, 0x7a);
const API_CREDENTIAL = Buffer.alloc(32, 0x6b);
const DETAILS_CREDENTIAL = Buffer.alloc(32, 0x6c);
const databaseUrl = process.env.TEST_DATABASE_URL;

function interaction(overrides: Partial<Interaction> = {}): Interaction {
  return {
    uid: "N9E_oxk7dD9t7rR10dj-3",
    exp: Math.floor(Date.now() / 1_000) + 120,
    params: {
      client_id: "cookops-spike-client",
      resource: "https://cookops.example/mcp",
      scope: "cookops:mcp",
    },
    prompt: { name: "login", reasons: [], details: {} },
    ...overrides,
  } as Interaction;
}

test("private approval credential is exact, constant-time, and separate from the stored binding key", () => {
  assert(privateCredentialMatches(API_CREDENTIAL, `Bearer ${API_CREDENTIAL.toString("base64url")}`));
  assert.equal(
    privateCredentialMatches(API_CREDENTIAL, `Bearer ${APPROVAL_SECRET.toString("base64url")}`),
    false,
  );
  assert.equal(privateCredentialMatches(API_CREDENTIAL, "Bearer wrong"), false);
  assert.equal(
    privateCredentialMatches(API_CREDENTIAL, `Bearer ${API_CREDENTIAL.toString("base64url")}=`,),
    false,
  );
  assert.equal(privateCredentialMatches(API_CREDENTIAL, undefined), false);
});

test(
  "approval persistence is one-time and bound to the current interaction context",
  { skip: databaseUrl ? false : "TEST_DATABASE_URL is not configured" },
  async () => {
    assert.ok(databaseUrl);
    const schema = `oauth_approval_${randomUUID().replaceAll("-", "")}`;
    const administrator = new Pool({ connectionString: databaseUrl });
    const scopedUrl = new URL(databaseUrl);
    scopedUrl.searchParams.set("options", `-c search_path=${schema}`);
    const database = new Pool({ connectionString: scopedUrl.href });
    try {
      await administrator.query(`CREATE SCHEMA ${schema}`);
      await initializeInteractionApprovals(database);
      const approvals = new InteractionApprovalStore(database, APPROVAL_SECRET);
      const original = interaction();
      const binding = interactionBinding(original);
      const subject = "018f7cc9-4a90-7fa0-b7e4-77f6c42d5731";

      assert.equal(
        await approvals.record(original, {
          interactionUid: binding.interactionUid,
          subject,
          decision: "approve",
        }),
        true,
      );
      assert.equal(
        await approvals.record(original, {
          interactionUid: binding.interactionUid,
          subject,
          decision: "deny",
        }),
        false,
      );

      const mixedUp = { ...binding, clientId: "other-client" };
      assert.equal(await approvals.consume(mixedUp), undefined);
      assert.deepEqual(await approvals.consume(binding), {
        ...binding,
        subject,
        decision: "approve",
      });
      assert.equal(await approvals.consume(binding), undefined);
    } finally {
      await database.end();
      await administrator.query(`DROP SCHEMA IF EXISTS ${schema} CASCADE`);
      await administrator.end();
    }
  },
);

test(
  "private approval endpoint rejects unauthenticated, malformed, replayed, and mixed-up input",
  { skip: databaseUrl ? false : "TEST_DATABASE_URL is not configured" },
  async () => {
    assert.ok(databaseUrl);
    const schema = `oauth_approval_http_${randomUUID().replaceAll("-", "")}`;
    const administrator = new Pool({ connectionString: databaseUrl });
    const scopedUrl = new URL(databaseUrl);
    scopedUrl.searchParams.set("options", `-c search_path=${schema}`);
    const database = new Pool({ connectionString: scopedUrl.href });
    const original = interaction();
    const finished: unknown[] = [];
    const fakeProvider = {
      Interaction: { find: async (uid: string) => (uid === original.uid ? original : undefined) },
      Client: { find: async (id: string) => (id === "cookops-spike-client" ? { clientName: "CookOps test client" } : undefined) },
      interactionDetails: async () => original,
      interactionFinished: async (_request: unknown, response: { writeHead(status: number): { end(): void } }, result: unknown) => {
        finished.push(result);
        response.writeHead(204).end();
      },
    };
    const approvals = new InteractionApprovalStore(database, APPROVAL_SECRET);
    const server = createServer((request, response) => {
      void handleInteractionBridgeRequest(
        fakeProvider as never,
        approvals,
        API_CREDENTIAL,
        DETAILS_CREDENTIAL,
        "/oauth",
        request,
        response,
      ).then((handled) => {
        if (!handled) response.writeHead(404).end();
      });
    });
    try {
      await administrator.query(`CREATE SCHEMA ${schema}`);
      await initializeInteractionApprovals(database);
      server.listen(0, "127.0.0.1");
      await once(server, "listening");
      const address = server.address();
      assert(address && typeof address !== "string");
      const endpoint = `http://127.0.0.1:${address.port}/oauth/private/interactions/approval`;
      const details = endpoint.replace("/approval", `/${original.uid}`);
      assert.equal((await fetch(details)).status, 401);
      assert.equal(
        (await fetch(details, { headers: { authorization: `Bearer ${API_CREDENTIAL.toString("base64url")}` } })).status,
        401,
      );
      const detail = await fetch(details, {
        headers: { authorization: `Bearer ${DETAILS_CREDENTIAL.toString("base64url")}` },
      });
      assert.equal(detail.status, 200);
      assert.deepEqual(await detail.json(), {
        interactionUid: original.uid,
        clientId: "cookops-spike-client",
        clientName: "CookOps test client",
        resource: "https://cookops.example/mcp",
        scopes: ["cookops:mcp"],
        prompt: "login",
      });
      assert.equal((await fetch(endpoint, { method: "POST" })).status, 401);
      assert.equal(
        (
          await fetch(endpoint, {
            headers: { authorization: `Bearer ${API_CREDENTIAL.toString("base64url")}` },
            method: "POST",
          })
        ).status,
        400,
      );
      const payload = {
        interactionUid: original.uid,
        subject: "018f7cc9-4a90-7fa0-b7e4-77f6c42d5731",
        decision: "approve",
      };
      const request = {
        body: JSON.stringify(payload),
        headers: {
          authorization: `Bearer ${API_CREDENTIAL.toString("base64url")}`,
          "content-type": "application/json",
        },
        method: "POST",
      };
      assert.equal((await fetch(endpoint, request)).status, 204);
      const completion = `${endpoint.replace("/private/interactions/approval", `/interaction/${original.uid}/complete`)}`;
      assert.equal((await fetch(completion)).status, 204);
      assert.deepEqual(finished, [{ login: { accountId: payload.subject } }]);
      assert.equal((await fetch(completion)).status, 403);
      assert.equal((await fetch(endpoint, request)).status, 409);
      assert.equal(
        (
          await fetch(endpoint, {
            ...request,
            body: JSON.stringify({ ...payload, interactionUid: "x".repeat(21) }),
          })
        ).status,
        404,
      );
    } finally {
      await server[Symbol.asyncDispose]();
      await database.end();
      await administrator.query(`DROP SCHEMA IF EXISTS ${schema} CASCADE`);
      await administrator.end();
    }
  },
);

test(
  "completion honors deny, retains a mismatched consent approval, and consumes concurrent approval once",
  { skip: databaseUrl ? false : "TEST_DATABASE_URL is not configured" },
  async () => {
    assert.ok(databaseUrl);
    const schema = `oauth_approval_completion_${randomUUID().replaceAll("-", "")}`;
    const administrator = new Pool({ connectionString: databaseUrl });
    const scopedUrl = new URL(databaseUrl);
    scopedUrl.searchParams.set("options", `-c search_path=${schema}`);
    const database = new Pool({ connectionString: scopedUrl.href });
    let current = interaction({ uid: "A".repeat(21) });
    const finished: unknown[] = [];
    const fakeProvider = {
      Interaction: { find: async (uid: string) => (uid === current.uid ? current : undefined) },
      interactionDetails: async () => current,
      interactionFinished: async (
        _request: unknown,
        response: { writeHead(status: number): { end(): void } },
        result: unknown,
      ) => {
        finished.push(result);
        response.writeHead(204).end();
      },
    };
    const approvals = new InteractionApprovalStore(database, APPROVAL_SECRET);
    const server = createServer((request, response) => {
      void handleInteractionBridgeRequest(
        fakeProvider as never,
        approvals,
        API_CREDENTIAL,
        DETAILS_CREDENTIAL,
        "/oauth",
        request,
        response,
      ).then((handled) => {
        if (!handled) response.writeHead(404).end();
      });
    });
    const subject = "018f7cc9-4a90-7fa0-b7e4-77f6c42d5731";
    const request = (decision: "approve" | "deny") => ({
      body: JSON.stringify({ interactionUid: current.uid, subject, decision }),
      headers: {
        authorization: `Bearer ${API_CREDENTIAL.toString("base64url")}`,
        "content-type": "application/json",
      },
      method: "POST",
    });
    try {
      await administrator.query(`CREATE SCHEMA ${schema}`);
      await initializeInteractionApprovals(database);
      server.listen(0, "127.0.0.1");
      await once(server, "listening");
      const address = server.address();
      assert(address && typeof address !== "string");
      const origin = `http://127.0.0.1:${address.port}/oauth`;
      const complete = () => fetch(`${origin}/interaction/${current.uid}/complete`);
      const approve = (decision: "approve" | "deny") =>
        fetch(`${origin}/private/interactions/approval`, request(decision));

      assert.equal((await approve("deny")).status, 204);
      assert.equal((await complete()).status, 204);
      assert.deepEqual(finished, [{ error: "access_denied" }]);

      const wrongSubject = "018f7cc9-4a90-7fa0-b7e4-77f6c42d5732";
      current = interaction({
        uid: "B".repeat(21),
        prompt: { name: "consent", reasons: [], details: {} },
        session: { accountId: wrongSubject, uid: "session", cookie: "cookie" },
      });
      assert.equal((await approve("approve")).status, 204);
      assert.equal((await complete()).status, 403);
      assert.deepEqual(finished, [{ error: "access_denied" }]);
      assert.equal((await approvals.consume(interactionBinding(current), subject))?.subject, subject);

      current = interaction({ uid: "C".repeat(21) });
      assert.equal((await approve("approve")).status, 204);
      const statuses = (await Promise.all([complete(), complete()])).map(({ status }) => status).sort();
      assert.deepEqual(statuses, [204, 403]);
      assert.deepEqual(finished.at(-1), { login: { accountId: subject } });
      assert.equal(finished.filter((value) => "login" in (value as object)).length, 1);
    } finally {
      await server[Symbol.asyncDispose]();
      await database.end();
      await administrator.query(`DROP SCHEMA IF EXISTS ${schema} CASCADE`);
      await administrator.end();
    }
  },
);

test(
  "expired interaction approvals are never consumed",
  { skip: databaseUrl ? false : "TEST_DATABASE_URL is not configured" },
  async () => {
    assert.ok(databaseUrl);
    const schema = `oauth_approval_expiry_${randomUUID().replaceAll("-", "")}`;
    const administrator = new Pool({ connectionString: databaseUrl });
    const scopedUrl = new URL(databaseUrl);
    scopedUrl.searchParams.set("options", `-c search_path=${schema}`);
    const database = new Pool({ connectionString: scopedUrl.href });
    try {
      await administrator.query(`CREATE SCHEMA ${schema}`);
      await initializeInteractionApprovals(database);
      const approvals = new InteractionApprovalStore(database, APPROVAL_SECRET);
      const original = interaction();
      const binding = interactionBinding(original);
      assert(
        await approvals.record(original, {
          interactionUid: binding.interactionUid,
          subject: "018f7cc9-4a90-7fa0-b7e4-77f6c42d5731",
          decision: "deny",
        }),
      );
      await database.query(
        "UPDATE oidc_interaction_approvals SET expires_at = CURRENT_TIMESTAMP - interval '1 second'",
      );
      assert.equal(await approvals.consume(binding), undefined);
    } finally {
      await database.end();
      await administrator.query(`DROP SCHEMA IF EXISTS ${schema} CASCADE`);
      await administrator.end();
    }
  },
);
