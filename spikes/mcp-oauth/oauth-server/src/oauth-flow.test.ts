import assert from "node:assert/strict";
import { createHash, generateKeyPairSync, randomUUID } from "node:crypto";
import { once } from "node:events";
import {
  createServer,
  request as httpRequest,
  type IncomingMessage,
  type Server,
  type ServerResponse,
} from "node:http";
import test from "node:test";

import type { Interaction, Provider } from "oidc-provider";
import { Pool } from "pg";

import {
  MCP_SCOPE,
  PUBLIC_CLIENT_ID,
  RESOURCE_SERVER_CLIENT_ID,
} from "./provider-profile.js";
import {
  startOAuthServer,
  type OAuthRuntimeConfiguration,
  type RunningOAuthServer,
} from "./runtime.js";

const COOKIE_KEYS = ["a".repeat(32), "b".repeat(32)];
const RESOURCE_SERVER_SECRET = "c".repeat(32);
const ADAPTER_SECRET = Buffer.alloc(32, 0x5a);
const INTERNAL_USER_ID = "018f7cc9-4a90-7fa0-b7e4-77f6c42d5731";
const CODE_VERIFIER = "cookops-spike-verifier-that-is-long-enough-for-pkce";
const CODE_CHALLENGE = createHash("sha256")
  .update(CODE_VERIFIER)
  .digest("base64url");
const privateJwk = generateKeyPairSync("rsa", {
  modulusLength: 2048,
}).privateKey.export({ format: "jwk" });
const JWKS = {
  keys: [{ ...privateJwk, alg: "RS256", kid: "oauth-flow-test", use: "sig" }],
};

interface TokenResponse {
  access_token: string;
  expires_in: number;
  refresh_token: string;
  scope: string;
  token_type: string;
}

interface IntrospectionResponse {
  active: boolean;
  aud?: string;
  client_id?: string;
  exp?: number;
  iss?: string;
  scope?: string;
  sub?: string;
}

class CookieJar {
  readonly #cookies = new Map<string, string>();

  async fetch(url: string, init: RequestInit = {}): Promise<Response> {
    const headers = new Headers(init.headers);
    if (this.#cookies.size) {
      headers.set(
        "cookie",
        [...this.#cookies].map(([name, value]) => `${name}=${value}`).join("; "),
      );
    }
    const response = await fetch(url, { ...init, headers, redirect: "manual" });
    for (const cookie of response.headers.getSetCookie()) {
      const [pair = "", ...attributes] = cookie.split(";");
      const separator = pair.indexOf("=");
      if (separator === -1) continue;
      const name = pair.slice(0, separator);
      const value = pair.slice(separator + 1);
      const expired = attributes.some(
        (attribute) => attribute.trim().toLowerCase() === "max-age=0",
      );
      if (!value || expired) this.#cookies.delete(name);
      else this.#cookies.set(name, value);
    }
    return response;
  }
}

async function completeInteraction(
  provider: Provider,
  request: IncomingMessage,
  response: ServerResponse,
): Promise<void> {
  const interaction = await provider.interactionDetails(request, response);
  if (interaction.prompt.name === "login") {
    await provider.interactionFinished(
      request,
      response,
      { login: { accountId: INTERNAL_USER_ID } },
      { mergeWithLastSubmission: false },
    );
    return;
  }
  assert.equal(interaction.prompt.name, "consent");
  const grant = await consentGrant(provider, interaction);
  await provider.interactionFinished(
    request,
    response,
    { consent: { grantId: await grant.save() } },
    { mergeWithLastSubmission: true },
  );
}

async function consentGrant(provider: Provider, interaction: Interaction) {
  const existing = interaction.grantId
    ? await provider.Grant.find(interaction.grantId)
    : undefined;
  const grant =
    existing ??
    new provider.Grant({
      accountId: interaction.session?.accountId,
      clientId: String(interaction.params.client_id),
    });
  const missingOidcScope = interaction.prompt.details.missingOIDCScope as
    | string[]
    | undefined;
  if (missingOidcScope) grant.addOIDCScope(missingOidcScope);
  const missingOidcClaims = interaction.prompt.details.missingOIDCClaims as
    | string[]
    | undefined;
  if (missingOidcClaims) grant.addOIDCClaims(missingOidcClaims);
  const missingResourceScopes = interaction.prompt.details
    .missingResourceScopes as Record<string, string[]> | undefined;
  for (const [resource, scopes] of Object.entries(missingResourceScopes ?? {})) {
    grant.addResourceScope(resource, scopes);
  }
  return grant;
}

async function startTestProxy(): Promise<{
  origin: string;
  server: Server;
  setRuntime(runtime: RunningOAuthServer): void;
}> {
  let runtime: RunningOAuthServer | undefined;
  const server = createServer(async (request, response) => {
    try {
      if (!runtime) {
        response.writeHead(503).end();
        return;
      }
      if (new URL(request.url ?? "/", "http://localhost").pathname.includes("/interaction/")) {
        await completeInteraction(runtime.provider, request, response);
        return;
      }
      const target = runtime.server.address();
      assert(target && typeof target !== "string");
      const forwarded = httpRequest(
        {
          hostname: "127.0.0.1",
          port: target.port,
          path: request.url,
          method: request.method,
          headers: {
            ...request.headers,
            "x-forwarded-host": request.headers.host,
            "x-forwarded-proto": "http",
          },
        },
        (upstream) => {
          response.writeHead(upstream.statusCode ?? 502, upstream.headers);
          upstream.pipe(response);
        },
      );
      forwarded.on("error", (error) => response.destroy(error));
      request.pipe(forwarded);
    } catch (error) {
      response.destroy(error as Error);
    }
  });
  server.listen(0, "127.0.0.1");
  await once(server, "listening");
  const address = server.address();
  assert(address && typeof address !== "string");
  return {
    origin: `http://127.0.0.1:${address.port}`,
    server,
    setRuntime(value) {
      runtime = value;
    },
  };
}

async function authorizationCode(
  jar: CookieJar,
  origin: string,
  resource: string | undefined,
): Promise<{ code: string | undefined; error: string | undefined }> {
  const state = randomUUID();
  const parameters = new URLSearchParams({
    client_id: PUBLIC_CLIENT_ID,
    code_challenge: CODE_CHALLENGE,
    code_challenge_method: "S256",
    redirect_uri: `${origin}/callback`,
    response_type: "code",
    scope: MCP_SCOPE,
    state,
  });
  if (resource !== undefined) parameters.set("resource", resource);

  let current = `${origin}/oauth/authorize?${parameters}`;
  const redirects: string[] = [];
  for (let redirect = 0; redirect < 10; redirect += 1) {
    const response = await jar.fetch(current);
    assert([302, 303].includes(response.status), `unexpected ${response.status} for ${current}`);
    const location = response.headers.get("location");
    assert(location);
    const redirectUrl = new URL(location, current);
    current = redirectUrl.href;
    redirects.push(current);
    if (redirectUrl.pathname === "/callback") {
      assert.equal(redirectUrl.searchParams.get("state"), state);
      assert.equal(redirectUrl.searchParams.get("iss"), `${origin}/oauth`);
      return {
        code: redirectUrl.searchParams.get("code") ?? undefined,
        error: redirectUrl.searchParams.get("error") ?? undefined,
      };
    }
  }
  throw new Error(`authorization flow exceeded redirect limit: ${redirects.join(" -> ")}`);
}

function assertActiveIntrospection(
  inspected: IntrospectionResponse,
  origin: string,
  resource: string,
): void {
  const now = Math.floor(Date.now() / 1_000);
  assert.deepEqual(
    {
      active: inspected.active,
      aud: inspected.aud,
      client_id: inspected.client_id,
      iss: inspected.iss,
      scope: inspected.scope,
      sub: inspected.sub,
    },
    {
      active: true,
      aud: resource,
      client_id: PUBLIC_CLIENT_ID,
      iss: `${origin}/oauth`,
      scope: MCP_SCOPE,
      sub: INTERNAL_USER_ID,
    },
  );
  assert(inspected.exp && inspected.exp > now && inspected.exp <= now + 15 * 60);
}

async function assertSecureForwardedCookies(
  configuration: OAuthRuntimeConfiguration,
): Promise<RunningOAuthServer> {
  const issuer = "https://cookops.example/oauth";
  const resource = "https://cookops.example/mcp";
  const redirectUri = "https://client.example/callback";
  const runtime = await startOAuthServer({
    ...configuration,
    issuer,
    resource,
    redirectUri,
    port: 0,
  });
  const address = runtime.server.address();
  assert(address && typeof address !== "string");
  const parameters = new URLSearchParams({
    client_id: PUBLIC_CLIENT_ID,
    code_challenge: CODE_CHALLENGE,
    code_challenge_method: "S256",
    redirect_uri: redirectUri,
    resource,
    response_type: "code",
    scope: MCP_SCOPE,
    state: randomUUID(),
  });
  const response = await fetch(
    `http://127.0.0.1:${address.port}/oauth/authorize?${parameters}`,
    {
      headers: {
        host: "cookops.example",
        "x-forwarded-host": "cookops.example",
        "x-forwarded-proto": "https",
      },
      redirect: "manual",
    },
  );
  assert.equal(response.status, 303);
  assert.match(response.headers.get("location") ?? "", /^https:\/\/cookops\.example\/oauth\/interaction\//);
  const cookies = response.headers.getSetCookie();
  assert(cookies.length > 0);
  for (const cookie of cookies) assert.match(cookie, /;\s*Secure(?:;|$)/i);
  return runtime;
}

async function tokenRequest(
  origin: string,
  parameters: Record<string, string>,
): Promise<Response> {
  return fetch(`${origin}/oauth/token`, {
    method: "POST",
    headers: { "content-type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams({ client_id: PUBLIC_CLIENT_ID, ...parameters }),
  });
}

async function exchangeCode(
  origin: string,
  code: string,
  resource?: string,
): Promise<Response> {
  const parameters: Record<string, string> = {
    code,
    code_verifier: CODE_VERIFIER,
    grant_type: "authorization_code",
    redirect_uri: `${origin}/callback`,
  };
  if (resource !== undefined) parameters.resource = resource;
  return tokenRequest(origin, parameters);
}

async function introspect(origin: string, token: string): Promise<IntrospectionResponse> {
  const response = await fetch(`${origin}/oauth/introspect`, {
    method: "POST",
    headers: {
      authorization: `Basic ${Buffer.from(
        `${RESOURCE_SERVER_CLIENT_ID}:${RESOURCE_SERVER_SECRET}`,
      ).toString("base64")}`,
      "content-type": "application/x-www-form-urlencoded",
    },
    body: new URLSearchParams({ token }),
  });
  assert.equal(response.status, 200);
  return (await response.json()) as IntrospectionResponse;
}

const integrationDatabaseUrl = process.env.TEST_DATABASE_URL;

test(
  "public client completes resource-bound PKCE, refresh, introspection, and revocation",
  { skip: integrationDatabaseUrl ? false : "TEST_DATABASE_URL is not configured" },
  async () => {
    assert.ok(integrationDatabaseUrl);
    const schema = `oauth_flow_${randomUUID().replaceAll("-", "")}`;
    const administrator = new Pool({ connectionString: integrationDatabaseUrl });
    const proxy = await startTestProxy();
    let runtime: RunningOAuthServer | undefined;
    try {
      await administrator.query(`CREATE SCHEMA ${schema}`);
      const databaseUrl = new URL(integrationDatabaseUrl);
      databaseUrl.searchParams.set("options", `-c search_path=${schema}`);
      const resource = `${proxy.origin}/mcp`;
      const configuration: OAuthRuntimeConfiguration = {
        issuer: `${proxy.origin}/oauth`,
        resource,
        redirectUri: `${proxy.origin}/callback`,
        cookieKeys: COOKIE_KEYS,
        resourceServerSecret: RESOURCE_SERVER_SECRET,
        jwks: JWKS,
        databaseUrl: databaseUrl.href,
        adapterSecret: ADAPTER_SECRET,
        host: "127.0.0.1",
        port: 0,
      };
      runtime = await startOAuthServer(configuration);
      proxy.setRuntime(runtime);
      const jar = new CookieJar();

      assert.deepEqual(await authorizationCode(jar, proxy.origin, undefined), {
        code: undefined,
        error: "invalid_target",
      });

      const missingResourceCode = await authorizationCode(jar, proxy.origin, resource);
      assert.ok(missingResourceCode.code);
      const missingResourceResponse = await exchangeCode(
        proxy.origin,
        missingResourceCode.code,
      );
      assert.equal(missingResourceResponse.status, 400);
      assert.equal((await missingResourceResponse.json()).error, "invalid_target");

      const wrongResourceCode = await authorizationCode(jar, proxy.origin, resource);
      assert.ok(wrongResourceCode.code);
      const wrongResourceResponse = await exchangeCode(
        proxy.origin,
        wrongResourceCode.code,
        `${proxy.origin}/wrong-resource`,
      );
      assert.equal(wrongResourceResponse.status, 400);
      assert.equal((await wrongResourceResponse.json()).error, "invalid_target");

      const wrongVerifierCode = await authorizationCode(jar, proxy.origin, resource);
      assert.ok(wrongVerifierCode.code);
      const wrongVerifierResponse = await tokenRequest(proxy.origin, {
        code: wrongVerifierCode.code,
        code_verifier: "wrong-verifier-that-is-also-long-enough-for-pkce",
        grant_type: "authorization_code",
        redirect_uri: `${proxy.origin}/callback`,
        resource,
      });
      assert.equal(wrongVerifierResponse.status, 400);
      assert.equal((await wrongVerifierResponse.json()).error, "invalid_grant");

      const validCode = await authorizationCode(jar, proxy.origin, resource);
      assert.ok(validCode.code);
      const tokenResponse = await exchangeCode(proxy.origin, validCode.code, resource);
      assert.equal(tokenResponse.status, 200);
      const tokens = (await tokenResponse.json()) as TokenResponse;
      assert.equal(tokens.token_type, "Bearer");
      assert.equal(tokens.scope, MCP_SCOPE);
      assert.equal(tokens.expires_in, 15 * 60);
      assert.equal(tokens.access_token.includes("."), false);
      assert.ok(tokens.refresh_token);

      const persistedPayloads = await administrator.query<{ payload: string }>(
        `SELECT payload::text AS payload FROM ${schema}.oidc_provider_records`,
      );
      const databaseContents = persistedPayloads.rows.map(({ payload }) => payload).join("\n");
      for (const credential of [
        validCode.code,
        tokens.access_token,
        tokens.refresh_token,
      ]) {
        assert.equal(databaseContents.includes(credential), false);
      }

      assertActiveIntrospection(
        await introspect(proxy.origin, tokens.access_token),
        proxy.origin,
        resource,
      );

      const refreshResponse = await tokenRequest(proxy.origin, {
        grant_type: "refresh_token",
        refresh_token: tokens.refresh_token,
        resource,
      });
      assert.equal(refreshResponse.status, 200);
      const rotated = (await refreshResponse.json()) as TokenResponse;
      assert.equal(rotated.expires_in, 15 * 60);
      assert.notEqual(rotated.refresh_token, tokens.refresh_token);
      assert.notEqual(rotated.access_token, tokens.access_token);
      assertActiveIntrospection(
        await introspect(proxy.origin, rotated.access_token),
        proxy.origin,
        resource,
      );

      const replayResponse = await tokenRequest(proxy.origin, {
        grant_type: "refresh_token",
        refresh_token: tokens.refresh_token,
        resource,
      });
      assert.equal(replayResponse.status, 400);
      assert.equal((await replayResponse.json()).error, "invalid_grant");
      assert.deepEqual(await introspect(proxy.origin, rotated.access_token), {
        active: false,
      });
      const rotatedReplayResponse = await tokenRequest(proxy.origin, {
        grant_type: "refresh_token",
        refresh_token: rotated.refresh_token,
        resource,
      });
      assert.equal(rotatedReplayResponse.status, 400);
      assert.equal((await rotatedReplayResponse.json()).error, "invalid_grant");

      const revocableCode = await authorizationCode(jar, proxy.origin, resource);
      assert.ok(revocableCode.code);
      const revocableResponse = await exchangeCode(
        proxy.origin,
        revocableCode.code,
        resource,
      );
      assert.equal(revocableResponse.status, 200);
      const revocable = (await revocableResponse.json()) as TokenResponse;
      const revocationResponse = await fetch(`${proxy.origin}/oauth/revoke`, {
        method: "POST",
        headers: { "content-type": "application/x-www-form-urlencoded" },
        body: new URLSearchParams({
          client_id: PUBLIC_CLIENT_ID,
          token: revocable.access_token,
          token_type_hint: "access_token",
        }),
      });
      assert.equal(revocationResponse.status, 200);
      assert.deepEqual(await introspect(proxy.origin, revocable.access_token), {
        active: false,
      });
      const revokedFamilyRefresh = await tokenRequest(proxy.origin, {
        grant_type: "refresh_token",
        refresh_token: revocable.refresh_token,
        resource,
      });
      assert.equal(revokedFamilyRefresh.status, 400);
      assert.equal((await revokedFamilyRefresh.json()).error, "invalid_grant");

      await runtime.close();
      runtime = await assertSecureForwardedCookies(configuration);
    } finally {
      await runtime?.close();
      await proxy.server[Symbol.asyncDispose]();
      await administrator.query(`DROP SCHEMA IF EXISTS ${schema} CASCADE`);
      await administrator.end();
    }
  },
);
