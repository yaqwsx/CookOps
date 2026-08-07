import assert from "node:assert/strict";
import { generateKeyPairSync, randomUUID } from "node:crypto";
import { once } from "node:events";
import {
  createServer,
  request as httpRequest,
  type IncomingMessage,
  type Server,
  type ServerResponse,
} from "node:http";

import type { Interaction, Provider } from "oidc-provider";
import { Pool } from "pg";

import {
  startOAuthServer,
  type RunningOAuthServer,
} from "../src/runtime.js";

if (process.env.COOKOPS_OAUTH_E2E_FIXTURE !== "authenticated-mcp") {
  throw new Error("authenticated MCP fixture requires explicit test mode");
}

const INTERNAL_USER_ID = "018f7cc9-4a90-7fa0-b7e4-77f6c42d5731";
const RESOURCE_SERVER_SECRET = "c".repeat(32);

function required(name: string): string {
  const value = process.env[name];
  if (!value) throw new Error(`${name} is required`);
  return value;
}

function port(name: string): number {
  const value = Number(required(name));
  if (!Number.isInteger(value) || value < 1 || value > 65_535) {
    throw new TypeError(`${name} must be a valid TCP port`);
  }
  return value;
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

function forward(
  request: IncomingMessage,
  response: ServerResponse,
  targetPort: number,
  forwardedHost?: string,
): void {
  const forwarded = httpRequest(
    {
      hostname: "127.0.0.1",
      port: targetPort,
      path: request.url,
      method: request.method,
      headers: forwardedHost
        ? {
            ...request.headers,
            host: forwardedHost,
            "x-forwarded-host": forwardedHost,
            "x-forwarded-proto": "http",
          }
        : request.headers,
    },
    (upstream) => {
      response.writeHead(upstream.statusCode ?? 502, upstream.headers);
      upstream.pipe(response);
    },
  );
  forwarded.on("error", (error) => response.destroy(error));
  request.pipe(forwarded);
}

async function listen(server: Server, listenPort: number): Promise<number> {
  server.listen(listenPort, "127.0.0.1");
  await once(server, "listening");
  const address = server.address();
  assert(address && typeof address !== "string");
  return address.port;
}

async function close(server: Server | undefined): Promise<void> {
  if (server?.listening) await server[Symbol.asyncDispose]();
}

async function main(): Promise<void> {
  const resourcePort = port("OAUTH_E2E_RESOURCE_PORT");
  const databaseUrl = required("OAUTH_E2E_DATABASE_URL");
  const schema = `authenticated_mcp_${randomUUID().replaceAll("-", "")}`;
  const administrator = new Pool({ connectionString: databaseUrl });
  let runtime: RunningOAuthServer | undefined;
  let publicProxy: Server | undefined;
  let privateProxy: Server | undefined;
  let publicOrigin = "http://127.0.0.1";
  let publicPort = 0;
  let oauthPort = 0;

  try {
    await administrator.query(`CREATE SCHEMA ${schema}`);
    publicProxy = createServer(async (request, response) => {
      try {
        const pathname = new URL(request.url ?? "/", publicOrigin).pathname;
        if (pathname.startsWith("/oauth/interaction/")) {
          if (!runtime) {
            response.writeHead(503).end();
            return;
          }
          await completeInteraction(runtime.provider, request, response);
        } else if (
          pathname.startsWith("/oauth/") ||
          pathname.startsWith("/.well-known/openid-configuration/oauth") ||
          pathname.startsWith("/.well-known/oauth-authorization-server/oauth")
        ) {
          if (oauthPort === 0) {
            response.writeHead(503).end();
            return;
          }
          forward(request, response, oauthPort, `127.0.0.1:${publicPort}`);
        } else {
          forward(request, response, resourcePort);
        }
      } catch (error) {
        response.destroy(error as Error);
      }
    });
    publicPort = await listen(publicProxy, 0);
    publicOrigin = `http://127.0.0.1:${publicPort}`;

    const isolatedDatabaseUrl = new URL(databaseUrl);
    isolatedDatabaseUrl.searchParams.set("options", `-c search_path=${schema}`);
    const privateJwk = generateKeyPairSync("rsa", {
      modulusLength: 2048,
    }).privateKey.export({ format: "jwk" });
    runtime = await startOAuthServer({
      issuer: `${publicOrigin}/oauth`,
      resource: `${publicOrigin}/mcp`,
      redirectUri: `${publicOrigin}/callback`,
      cookieKeys: ["a".repeat(32), "b".repeat(32)],
      resourceServerSecret: RESOURCE_SERVER_SECRET,
      jwks: {
        keys: [
          { ...privateJwk, alg: "RS256", kid: "authenticated-mcp", use: "sig" },
        ],
      },
      databaseUrl: isolatedDatabaseUrl.href,
      adapterSecret: Buffer.alloc(32, 0x5a),
      interactionApprovalSecret: Buffer.alloc(32, 0x6b),
      host: "127.0.0.1",
      port: 0,
    });
    const oauthAddress = runtime.server.address();
    assert(oauthAddress && typeof oauthAddress !== "string");
    oauthPort = oauthAddress.port;

    privateProxy = createServer((request, response) => {
      const pathname = new URL(request.url ?? "/", "http://localhost").pathname;
      if (pathname !== "/oauth/introspect") {
        response.writeHead(404).end();
        return;
      }
      forward(request, response, oauthPort, `127.0.0.1:${publicPort}`);
    });
    const privatePort = await listen(privateProxy, 0);
    process.stdout.write(
      `${JSON.stringify({
        issuer: `${publicOrigin}/oauth`,
        privatePort,
        resource: `${publicOrigin}/mcp`,
        subject: INTERNAL_USER_ID,
      })}\n`,
    );

    await new Promise<void>((resolve) => {
      process.once("SIGTERM", resolve);
      process.once("SIGINT", resolve);
    });
  } finally {
    const cleanupErrors: unknown[] = [];
    for (const result of await Promise.allSettled([
      close(publicProxy),
      close(privateProxy),
    ])) {
      if (result.status === "rejected") cleanupErrors.push(result.reason);
    }
    try {
      await runtime?.close();
    } catch (error) {
      cleanupErrors.push(error);
    }
    try {
      await administrator.query(`DROP SCHEMA IF EXISTS ${schema} CASCADE`);
    } catch (error) {
      cleanupErrors.push(error);
    }
    try {
      await administrator.end();
    } catch (error) {
      cleanupErrors.push(error);
    }
    if (cleanupErrors.length > 0) {
      throw new AggregateError(cleanupErrors, "authenticated MCP fixture cleanup failed");
    }
  }
}

await main();
