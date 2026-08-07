import assert from "node:assert/strict";
import { generateKeyPairSync, randomUUID } from "node:crypto";
import test from "node:test";

import { Pool } from "pg";

import {
  decodeAdapterSecret,
  runtimeConfigurationFromEnvironment,
  startOAuthServer,
  type OAuthRuntimeConfiguration,
} from "./runtime.js";

const COOKIE_KEYS = ["a".repeat(32), "b".repeat(32)];
const RESOURCE_SERVER_SECRET = "c".repeat(32);
const ADAPTER_SECRET = Buffer.alloc(32, 0x5a);
const privateJwk = generateKeyPairSync("rsa", { modulusLength: 2048 }).privateKey.export({
  format: "jwk",
});
const JWKS = {
  keys: [{ ...privateJwk, alg: "RS256", kid: "runtime-test", use: "sig" }],
};

test("runtime settings require explicit canonical base64url adapter key material", () => {
  assert.deepEqual(
    decodeAdapterSecret(ADAPTER_SECRET.toString("base64url")),
    ADAPTER_SECRET,
  );
  for (const invalid of [
    "plain text is not an explicit encoding",
    "a".repeat(31),
    `${ADAPTER_SECRET.toString("base64url")}=`,
  ]) {
    assert.throws(() => decodeAdapterSecret(invalid), /base64url|32 bytes/);
  }

  const validEnvironment = {
    OAUTH_ISSUER: "http://127.0.0.1:3000/oauth",
    MCP_RESOURCE: "http://127.0.0.1:3000/mcp",
    MCP_CLIENT_REDIRECT_URI: "http://127.0.0.1:9876/callback",
    OAUTH_COOKIE_KEYS: COOKIE_KEYS.join(","),
    OAUTH_RESOURCE_SERVER_SECRET: RESOURCE_SERVER_SECRET,
    OAUTH_JWKS: JSON.stringify(JWKS),
    OAUTH_DATABASE_URL: "postgresql://cookops:secret@postgres/cookops",
    OAUTH_ADAPTER_SECRET_BASE64URL: ADAPTER_SECRET.toString("base64url"),
  };
  assert.equal(
    runtimeConfigurationFromEnvironment(validEnvironment).databaseUrl,
    validEnvironment.OAUTH_DATABASE_URL,
  );
  assert.throws(
    () => runtimeConfigurationFromEnvironment({ ...validEnvironment, NODE_ENV: "production" }),
    /must not run in production/,
  );
  assert.throws(
    () =>
      runtimeConfigurationFromEnvironment({
        ...validEnvironment,
        COOKOPS_ENVIRONMENT: "production",
      }),
    /must not run in production/,
  );
  for (const host of ["0.0.0.0", "::", "localhost", "oauth.internal"]) {
    assert.throws(
      () => runtimeConfigurationFromEnvironment({ ...validEnvironment, HOST: host }),
      /bind only to loopback/,
    );
  }
  assert.equal(
    runtimeConfigurationFromEnvironment({ ...validEnvironment, HOST: "::1" }).host,
    "::1",
  );
  assert.throws(
    () =>
      runtimeConfigurationFromEnvironment({
        ...validEnvironment,
        OAUTH_DATABASE_URL: "https://postgres.example/cookops",
      }),
    /PostgreSQL URL/,
  );
});

test("runtime refuses a direct public bind before connecting to PostgreSQL", async () => {
  await assert.rejects(
    startOAuthServer({
      issuer: "http://127.0.0.1:3000/oauth",
      resource: "http://127.0.0.1:3000/mcp",
      redirectUri: "http://127.0.0.1:9876/callback",
      cookieKeys: COOKIE_KEYS,
      resourceServerSecret: RESOURCE_SERVER_SECRET,
      jwks: JWKS,
      databaseUrl: "postgresql://unreachable:unreachable@127.0.0.1:1/unreachable",
      adapterSecret: ADAPTER_SECRET,
      host: "0.0.0.0",
      port: 0,
    }),
    /bind only to loopback/,
  );
});

const integrationDatabaseUrl = process.env.TEST_DATABASE_URL;

test(
  "provider-created session survives OAuth server and pool recreation",
  { skip: integrationDatabaseUrl ? false : "TEST_DATABASE_URL is not configured" },
  async () => {
    assert.ok(integrationDatabaseUrl);
    const schema = `oauth_runtime_${randomUUID().replaceAll("-", "")}`;
    const administrator = new Pool({ connectionString: integrationDatabaseUrl });
    await administrator.query(`CREATE SCHEMA ${schema}`);
    const url = new URL(integrationDatabaseUrl);
    url.searchParams.set("options", `-c search_path=${schema}`);
    const configuration: OAuthRuntimeConfiguration = {
      issuer: "http://127.0.0.1:3000/oauth",
      resource: "http://127.0.0.1:3000/mcp",
      redirectUri: "http://127.0.0.1:9876/callback",
      cookieKeys: COOKIE_KEYS,
      resourceServerSecret: RESOURCE_SERVER_SECRET,
      jwks: JWKS,
      databaseUrl: url.href,
      adapterSecret: ADAPTER_SECRET,
      host: "127.0.0.1",
      port: 0,
    };

    let firstRuntime: Awaited<ReturnType<typeof startOAuthServer>> | undefined;
    let secondRuntime: Awaited<ReturnType<typeof startOAuthServer>> | undefined;
    try {
      firstRuntime = await startOAuthServer(configuration);
      assert.equal(firstRuntime.server.listening, true);
      const session = new firstRuntime.provider.Session();
      session.loginAccount({ accountId: "cookops-user-id" });
      const sessionId = await session.save(60);

      const stored = await administrator.query<{ model: string }>(
        `SELECT model FROM ${schema}.oidc_provider_records`,
      );
      assert.deepEqual(stored.rows, [{ model: "Session" }]);

      await firstRuntime.close();
      assert.equal(firstRuntime.server.listening, false);
      await firstRuntime.close();
      firstRuntime = undefined;
      secondRuntime = await startOAuthServer(configuration);
      assert.equal(secondRuntime.server.listening, true);
      const restored = await secondRuntime.provider.Session.find(sessionId);
      assert.equal(restored?.accountId, "cookops-user-id");
      assert.equal(restored?.uid, session.uid);
      await secondRuntime.close();
      assert.equal(secondRuntime.server.listening, false);
      secondRuntime = undefined;

      await assert.rejects(
        startOAuthServer({ ...configuration, port: -1 }),
        /port|options/i,
      );
    } finally {
      await firstRuntime?.close();
      await secondRuntime?.close();
      await administrator.query(`DROP SCHEMA ${schema} CASCADE`);
      await administrator.end();
    }
  },
);
