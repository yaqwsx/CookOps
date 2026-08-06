import assert from "node:assert/strict";
import { generateKeyPairSync } from "node:crypto";
import { createServer } from "node:http";
import { once } from "node:events";
import test from "node:test";

import {
  createProvider,
  MCP_SCOPE,
  PUBLIC_CLIENT_ID,
  providerHttpHandler,
  RESOURCE_SERVER_CLIENT_ID,
  serverPort,
} from "./provider-profile.js";

const COOKIE_KEYS = ["a".repeat(32), "b".repeat(32)];
const RESOURCE_SERVER_SECRET = "c".repeat(32);
const privateJwk = generateKeyPairSync("rsa", { modulusLength: 2048 }).privateKey.export({
  format: "jwk",
});
const JWKS = { keys: [{ ...privateJwk, alg: "RS256", kid: "spike-test", use: "sig" }] };

async function runningProvider(
  check: (baseUrl: string) => Promise<void>,
): Promise<void> {
  let handler: ReturnType<typeof providerHttpHandler> = (_request, response) => {
    response.writeHead(503).end();
  };
  const server = createServer((request, response) => handler(request, response));
  server.listen(0, "127.0.0.1");
  await once(server, "listening");
  const address = server.address();
  assert(address && typeof address !== "string");
  const port = address.port;

  const baseUrl = `http://127.0.0.1:${port}`;
  const issuer = `${baseUrl}/oauth`;
  const provider = createProvider({
    issuer,
    resource: `${baseUrl}/mcp`,
    redirectUri: `${baseUrl}/callback`,
    cookieKeys: COOKIE_KEYS,
    resourceServerSecret: RESOURCE_SERVER_SECRET,
    jwks: JWKS,
  });
  handler = providerHttpHandler(provider, issuer);
  try {
    await check(baseUrl);
  } finally {
    await server[Symbol.asyncDispose]();
  }
}

test("publishes the constrained MCP OAuth profile at a path issuer", async () => {
  await runningProvider(async (baseUrl) => {
    const response = await fetch(`${baseUrl}/oauth/.well-known/openid-configuration`);
    assert.equal(response.status, 200);
    const metadata = (await response.json()) as Record<string, unknown>;

    assert.equal(metadata.issuer, `${baseUrl}/oauth`);
    assert.deepEqual(metadata.response_types_supported, ["code"]);
    assert.deepEqual(metadata.code_challenge_methods_supported, ["S256"]);
    assert.deepEqual(metadata.scopes_supported, [MCP_SCOPE, "openid"]);
    assert.equal(metadata.client_id_metadata_document_supported, true);
    assert.equal(metadata.authorization_endpoint, `${baseUrl}/oauth/authorize`);
    assert.equal(metadata.token_endpoint, `${baseUrl}/oauth/token`);
    assert.equal(metadata.registration_endpoint, `${baseUrl}/oauth/register`);
    assert.equal(metadata.introspection_endpoint, `${baseUrl}/oauth/introspect`);
    assert.equal(metadata.revocation_endpoint, `${baseUrl}/oauth/revoke`);

    const oauthMetadata = await fetch(
      `${baseUrl}/.well-known/oauth-authorization-server/oauth`,
    );
    assert.equal(oauthMetadata.status, 200);
    assert.equal((await oauthMetadata.json()).issuer, `${baseUrl}/oauth`);

    const insertedOidcMetadata = await fetch(
      `${baseUrl}/.well-known/openid-configuration/oauth`,
    );
    assert.equal(insertedOidcMetadata.status, 200);
    assert.equal((await insertedOidcMetadata.json()).issuer, `${baseUrl}/oauth`);

    const jwksResponse = await fetch(metadata.jwks_uri as string);
    assert.equal(jwksResponse.status, 200);
    const publishedJwks = (await jwksResponse.json()) as { keys: { kid?: string; d?: string }[] };
    assert.equal(publishedJwks.keys[0]?.kid, "spike-test");
    assert.equal(publishedJwks.keys[0]?.d, undefined);
  });
});

test("registers separate public and resource-server clients", async () => {
  const provider = createProvider({
    issuer: "https://cookops.example/oauth",
    resource: "https://cookops.example/mcp",
    redirectUri: "http://127.0.0.1:9876/callback",
    cookieKeys: COOKIE_KEYS,
    resourceServerSecret: RESOURCE_SERVER_SECRET,
    jwks: JWKS,
  });

  assert(await provider.Client.find(PUBLIC_CLIENT_ID));
  assert(await provider.Client.find(RESOURCE_SERVER_CLIENT_ID));
});

test("rejects authorization without the mandatory MCP resource", async () => {
  await runningProvider(async (baseUrl) => {
    const parameters = new URLSearchParams({
      client_id: PUBLIC_CLIENT_ID,
      code_challenge: "x".repeat(43),
      code_challenge_method: "S256",
      redirect_uri: `${baseUrl}/callback`,
      response_type: "code",
      scope: MCP_SCOPE,
      state: "test-state",
    });
    const response = await fetch(`${baseUrl}/oauth/authorize?${parameters}`, {
      redirect: "manual",
    });
    assert.equal(response.status, 303);
    const location = response.headers.get("location");
    assert(location);
    const redirect = new URL(location);
    assert.equal(redirect.searchParams.get("error"), "invalid_target");
    assert.equal(redirect.searchParams.get("state"), "test-state");
  });
});

test("rejects unsafe profile endpoints and weak secrets", () => {
  const valid = {
    issuer: "https://cookops.example/oauth",
    resource: "https://cookops.example/mcp",
    redirectUri: "http://127.0.0.1:9876/callback",
    cookieKeys: COOKIE_KEYS,
    resourceServerSecret: RESOURCE_SERVER_SECRET,
    jwks: JWKS,
  };

  assert.throws(() => createProvider({ ...valid, issuer: "http://cookops.example/oauth" }), /HTTPS/);
  assert.throws(() => createProvider({ ...valid, resource: "not a URL" }), /absolute URL/);
  assert.throws(() => createProvider({ ...valid, cookieKeys: ["short"] }), /cookieKeys/);
  assert.throws(() => createProvider({ ...valid, resourceServerSecret: "short" }), /resourceServerSecret/);
  assert.throws(() => createProvider({ ...valid, jwks: { keys: [] } }), /jwks/);
});

test("selects and validates the listening port", () => {
  assert.equal(serverPort("https://cookops.example/oauth"), 3000);
  assert.equal(serverPort("https://cookops.example:8443/oauth"), 8443);
  assert.equal(serverPort("https://cookops.example/oauth", "4100"), 4100);
  assert.throws(() => serverPort("https://cookops.example/oauth", "0"), /PORT/);
  assert.throws(() => serverPort("https://cookops.example/oauth", "invalid"), /PORT/);
});
