import assert from "node:assert/strict";

import type { AdapterPayload } from "oidc-provider";
import { Pool } from "pg";

import {
  createPostgresAdapter,
  initializePostgresAdapter,
} from "./postgres-adapter.js";

const SESSION_ID = "postgres-restart-session";
const SESSION_UID = "postgres-restart-session-uid";
const ADAPTER_SECRET = Buffer.from("cookops-postgres-restart-proof-secret");
const SESSION_STATE: AdapterPayload = {
  accountId: "postgres-restart-user",
  jti: SESSION_ID,
  loginTs: 1_725_000_000,
  remember: true,
  uid: SESSION_UID,
};

function requiredEnvironment(name: string): string {
  const value = process.env[name];
  if (!value) throw new Error(`${name} is required`);
  return value;
}

const mode = process.argv[2];
if (mode !== "write" && mode !== "read") {
  throw new Error("restart probe mode must be 'write' or 'read'");
}

const pool = new Pool({
  connectionString: requiredEnvironment("TEST_DATABASE_URL"),
  connectionTimeoutMillis: 5_000,
});

try {
  const factory = createPostgresAdapter(pool, { secret: ADAPTER_SECRET });
  const sessions = factory("Session");

  if (mode === "write") {
    await initializePostgresAdapter(pool);
    await sessions.upsert(SESSION_ID, SESSION_STATE, 3_600);
    assert.deepEqual(await sessions.find(SESSION_ID), SESSION_STATE);
    console.log(`wrote OAuth Session adapter state ${SESSION_ID}`);
  } else {
    assert.deepEqual(await sessions.find(SESSION_ID), SESSION_STATE);
    assert.deepEqual(await sessions.findByUid(SESSION_UID), SESSION_STATE);
    console.log(`read OAuth Session adapter state ${SESSION_ID} after restart`);
  }
} finally {
  await pool.end();
}
