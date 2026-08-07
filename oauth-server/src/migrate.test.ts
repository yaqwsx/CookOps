import assert from "node:assert/strict";
import { randomBytes } from "node:crypto";
import { spawnSync } from "node:child_process";
import test from "node:test";

import { Pool } from "pg";

import { OAUTH_SCHEMA_VERSION } from "./migrations.js";
import { schemaIsCurrent } from "./runtime.js";

const databaseUrl = process.env.TEST_DATABASE_URL;

test("versioned migration prepares the provider schema before the server starts", { skip: !databaseUrl }, async () => {
  const secret = randomBytes(32).toString("base64url");
  const result = spawnSync(process.execPath, ["--import", "tsx", "src/migrate.ts"], {
    cwd: process.cwd(),
    env: {
      ...process.env,
      OAUTH_ISSUER: "https://cookops.example/oauth",
      MCP_RESOURCE: "https://cookops.example/mcp",
      OAUTH_INTERACTION_URL: "https://cookops.example/auth/mcp-interactions",
      OAUTH_COOKIE_KEYS: `${"a".repeat(32)},${"b".repeat(32)}`,
      OAUTH_RESOURCE_SERVER_SECRET: "c".repeat(32),
      OAUTH_JWKS: JSON.stringify({ keys: [{}] }),
      OAUTH_DATABASE_URL: databaseUrl,
      OAUTH_ADAPTER_SECRET_BASE64URL: secret,
      OAUTH_INTERACTION_APPROVAL_SECRET_BASE64URL: secret,
      OAUTH_APPROVAL_API_CREDENTIAL_BASE64URL: secret,
    },
    encoding: "utf8",
  });
  assert.equal(result.status, 0, result.stderr);

  const pool = new Pool({ connectionString: databaseUrl });
  try {
    const version = await pool.query<{ version: number }>(
      "SELECT version FROM oauth_schema_migrations WHERE version = $1",
      [OAUTH_SCHEMA_VERSION],
    );
    assert.equal(version.rowCount, 1);
    const providerTable = await pool.query<{ exists: boolean }>(
      "SELECT to_regclass('oidc_provider_records') IS NOT NULL AS exists",
    );
    assert.equal(providerTable.rows[0]?.exists, true);
    assert.equal(await schemaIsCurrent(pool), true);
    await pool.query("INSERT INTO oauth_schema_migrations (version) VALUES ($1)", [
      OAUTH_SCHEMA_VERSION + 1,
    ]);
    assert.equal(await schemaIsCurrent(pool), false);
    await pool.query("DELETE FROM oauth_schema_migrations WHERE version > $1", [
      OAUTH_SCHEMA_VERSION,
    ]);
    await pool.query("INSERT INTO oauth_schema_migrations (version) VALUES (0)");
    assert.equal(await schemaIsCurrent(pool), false);
    await pool.query("DELETE FROM oauth_schema_migrations WHERE version = 0");
  } finally {
    await pool.end();
  }
});
