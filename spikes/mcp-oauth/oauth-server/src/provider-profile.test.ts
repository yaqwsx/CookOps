import assert from "node:assert/strict";
import { generateKeyPairSync } from "node:crypto";
import { createServer } from "node:http";
import { once } from "node:events";
import test from "node:test";

import type {
  Adapter,
  AdapterFactory,
  AdapterPayload,
} from "oidc-provider";

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

function createTestAdapter(): AdapterFactory {
  return (_model: string): Adapter => {
    const records = new Map<string, AdapterPayload>();
    return {
      async upsert(id, payload) {
        records.set(id, structuredClone(payload));
      },
      async find(id) {
        const payload = records.get(id);
        return payload && structuredClone(payload);
      },
      async findByUserCode(userCode) {
        const payload = [...records.values()].find(
          (payload) => payload.userCode === userCode,
        );
        return payload && structuredClone(payload);
      },
      async findByUid(uid) {
        const payload = [...records.values()].find(
          (payload) => payload.uid === uid,
        );
        return payload && structuredClone(payload);
      },
      async consume(id) {
        const payload = records.get(id);
        if (payload) payload.consumed = Math.floor(Date.now() / 1_000);
      },
      async destroy(id) {
        records.delete(id);
      },
      async revokeByGrantId(grantId) {
        for (const [id, payload] of records) {
          if (payload.grantId === grantId) records.delete(id);
        }
      },
    };
  };
}

async function runningProvider(
  check: (baseUrl: string) => Promise<void>,
  metadataFetch?: typeof fetch,
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
    adapter: createTestAdapter(),
    ...(metadataFetch ? { fetch: metadataFetch } : {}),
  });
  handler = providerHttpHandler(provider, issuer);
  try {
    await check(baseUrl);
  } finally {
    await server[Symbol.asyncDispose]();
  }
}

function authorizationParameters(
  baseUrl: string,
  clientId: string,
  redirectUri: string,
): URLSearchParams {
  return new URLSearchParams({
    client_id: clientId,
    code_challenge: "x".repeat(43),
    code_challenge_method: "S256",
    redirect_uri: redirectUri,
    resource: `${baseUrl}/mcp`,
    response_type: "code",
    scope: MCP_SCOPE,
    state: "test-state",
  });
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
    adapter: createTestAdapter(),
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

test("CIMD rejects private targets, redirects, malformed documents, and secrets", async () => {
  const requests: string[] = [];
  const secret = "must-never-appear-in-an-oauth-error";
  const metadataFetch: typeof fetch = async (input) => {
    const url = String(input);
    requests.push(url);
    if (url.endsWith("/redirect")) {
      return new Response(null, {
        headers: { location: "https://127.0.0.1/metadata" },
        status: 302,
      });
    }
    if (url.endsWith("/oversized")) {
      return new Response("x".repeat(5 * 1024 + 1), {
        headers: { "content-length": String(5 * 1024 + 1) },
      });
    }
    if (url.endsWith("/malformed")) return new Response("not JSON");
    return new Response(JSON.stringify({ client_id: url, client_secret: secret }));
  };

  await runningProvider(async (baseUrl) => {
    const response = await fetch(
      `${baseUrl}/oauth/authorize?${authorizationParameters(
        baseUrl,
        "https://127.0.0.1/metadata",
        `${baseUrl}/callback`,
      )}`,
      { redirect: "manual" },
    );
    assert.equal(response.status, 400);
  });

  await runningProvider(async (baseUrl) => {
    for (const clientId of [
      "https://metadata.example/redirect",
      "https://metadata.example/oversized",
      "https://metadata.example/malformed",
      "https://metadata.example/forbidden-secret",
    ]) {
      const response = await fetch(
        `${baseUrl}/oauth/authorize?${authorizationParameters(
          baseUrl,
          clientId,
          `${baseUrl}/callback`,
        )}`,
        { redirect: "manual" },
      );
      assert.equal(response.status, 400);
      const body = await response.text();
      assert.equal(body.includes(secret), false);
    }
  }, metadataFetch);

  assert.equal(requests.some((url) => url.includes("127.0.0.1")), false);
  assert.deepEqual(
    new Set(requests),
    new Set([
      "https://metadata.example/redirect",
      "https://metadata.example/oversized",
      "https://metadata.example/malformed",
      "https://metadata.example/forbidden-secret",
    ]),
  );
});

test("DCR keeps a public client secretless and enforces its exact redirect URI", async () => {
  await runningProvider(async (baseUrl) => {
    const redirectUri = `${baseUrl}/dcr-callback`;
    const registered = await fetch(`${baseUrl}/oauth/register`, {
      body: JSON.stringify({
        application_type: "native",
        client_name: "DCR fixture",
        grant_types: ["authorization_code", "refresh_token"],
        redirect_uris: [redirectUri],
        response_types: ["code"],
        token_endpoint_auth_method: "none",
      }),
      headers: { "content-type": "application/json" },
      method: "POST",
    });
    assert.equal(registered.status, 201);
    const client = (await registered.json()) as Record<string, string>;
    assert.ok(client.client_id);
    assert.equal("client_secret" in client, false);

    const exact = await fetch(
      `${baseUrl}/oauth/authorize?${authorizationParameters(
        baseUrl,
        client.client_id,
        redirectUri,
      )}`,
      { redirect: "manual" },
    );
    assert.equal(exact.status, 303);
    assert.match(exact.headers.get("location") ?? "", /\/interaction\//);

    const altered = await fetch(
      `${baseUrl}/oauth/authorize?${authorizationParameters(
        baseUrl,
        client.client_id,
        `${redirectUri}?unexpected=1`,
      )}`,
      { redirect: "manual" },
    );
    assert.equal(altered.status, 400);
  });
});

test("DCR does not follow a registered JWKS redirect to a private address", async () => {
  const requests: string[] = [];
  const metadataFetch: typeof fetch = async (input) => {
    requests.push(String(input));
    return new Response(null, {
      headers: { location: "https://127.0.0.1/jwks" },
      status: 302,
    });
  };
  await runningProvider(async (baseUrl) => {
    const registered = await fetch(`${baseUrl}/oauth/register`, {
      body: JSON.stringify({
        grant_types: ["authorization_code"],
        jwks_uri: "https://metadata.example/jwks",
        redirect_uris: [`${baseUrl}/dcr-callback`],
        response_types: ["code"],
        token_endpoint_auth_method: "private_key_jwt",
      }),
      headers: { "content-type": "application/json" },
      method: "POST",
    });
    assert.equal(registered.status, 201);
    const client = (await registered.json()) as { client_id: string };
    const encode = (value: object) => Buffer.from(JSON.stringify(value)).toString("base64url");
    const assertion = [
      encode({ alg: "RS256", typ: "JWT" }),
      encode({
        aud: `${baseUrl}/oauth/token`,
        exp: Math.floor(Date.now() / 1_000) + 60,
        iss: client.client_id,
        jti: "dcr-private-jwks-test",
        sub: client.client_id,
      }),
      "signature-is-never-verified-after-the-fetch-is-rejected",
    ].join(".");
    const response = await fetch(`${baseUrl}/oauth/token`, {
      body: new URLSearchParams({
        client_assertion: assertion,
        client_assertion_type: "urn:ietf:params:oauth:client-assertion-type:jwt-bearer",
        grant_type: "authorization_code",
      }),
      headers: { "content-type": "application/x-www-form-urlencoded" },
      method: "POST",
    });
    assert.equal(response.status, 401);
  }, metadataFetch);
  assert.deepEqual(requests, ["https://metadata.example/jwks"]);
});

test("rejects unsafe profile endpoints and weak secrets", () => {
  const valid = {
    issuer: "https://cookops.example/oauth",
    resource: "https://cookops.example/mcp",
    redirectUri: "http://127.0.0.1:9876/callback",
    cookieKeys: COOKIE_KEYS,
    resourceServerSecret: RESOURCE_SERVER_SECRET,
    jwks: JWKS,
    adapter: createTestAdapter(),
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
