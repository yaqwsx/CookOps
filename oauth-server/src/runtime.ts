import { once } from "node:events";
import { createServer, type Server } from "node:http";

import type { Configuration, Provider } from "oidc-provider";
import { Pool } from "pg";

import { createPostgresAdapter, type PgQueryable } from "./postgres-adapter.js";
import { InteractionApprovalStore } from "./interaction-approvals.js";
import { OAUTH_SCHEMA_VERSION, OAUTH_SCHEMA_VERSIONS } from "./migrations.js";
import { handleInteractionBridgeRequest } from "./interaction-bridge.js";
import {
  createProvider,
  providerHttpHandler,
  serverPort,
} from "./provider-profile.js";

export interface OAuthRuntimeConfiguration {
  issuer: string;
  resource: string;
  interactionUrl: string;
  cookieKeys: string[];
  resourceServerSecret: string;
  jwks: NonNullable<Configuration["jwks"]>;
  databaseUrl: string;
  adapterSecret: Uint8Array;
  interactionApprovalSecret: Uint8Array;
  approvalApiCredential: Uint8Array;
  interactionDetailsApiCredential: Uint8Array;
  host: string;
  port: number;
}

interface OAuthRuntime {
  provider: Provider;
  approvals: InteractionApprovalStore;
  isReady(): Promise<boolean>;
  close(): Promise<void>;
}

export interface RunningOAuthServer extends OAuthRuntime {
  server: Server;
}

function required(environment: NodeJS.ProcessEnv, name: string): string {
  const value = environment[name];
  if (!value) throw new Error(`${name} is required`);
  return value;
}

export function decodeAdapterSecret(encoded: string): Uint8Array {
  if (!/^[A-Za-z0-9_-]+$/.test(encoded)) {
    throw new TypeError("OAUTH_ADAPTER_SECRET_BASE64URL must be unpadded base64url");
  }
  const decoded = Buffer.from(encoded, "base64url");
  if (decoded.toString("base64url") !== encoded || decoded.byteLength < 32) {
    throw new TypeError(
      "OAUTH_ADAPTER_SECRET_BASE64URL must canonically encode at least 32 bytes",
    );
  }
  return decoded;
}

export function decodeInteractionApprovalSecret(encoded: string): Uint8Array {
  try {
    return decodeAdapterSecret(encoded);
  } catch {
    throw new TypeError(
      "OAUTH_INTERACTION_APPROVAL_SECRET_BASE64URL must canonically encode at least 32 bytes",
    );
  }
}

export function decodeApprovalApiCredential(encoded: string): Uint8Array {
  try {
    return decodeAdapterSecret(encoded);
  } catch {
    throw new TypeError(
      "OAUTH_APPROVAL_API_CREDENTIAL_BASE64URL must canonically encode at least 32 bytes",
    );
  }
}

export function decodeInteractionDetailsApiCredential(encoded: string): Uint8Array {
  try {
    return decodeAdapterSecret(encoded);
  } catch {
    throw new TypeError(
      "OAUTH_INTERACTION_DETAILS_API_CREDENTIAL_BASE64URL must canonically encode at least 32 bytes",
    );
  }
}

function databaseUrl(value: string): string {
  const parsed = URL.parse(value);
  if (!parsed || !["postgres:", "postgresql:"].includes(parsed.protocol)) {
    throw new TypeError("OAUTH_DATABASE_URL must be a PostgreSQL URL");
  }
  if (parsed.hash) {
    throw new TypeError("OAUTH_DATABASE_URL must not contain a fragment");
  }
  return value;
}

function publicHttpsUrl(value: string, name: string): string {
  const parsed = URL.parse(value);
  if (
    !parsed ||
    parsed.protocol !== "https:" ||
    !parsed.hostname ||
    parsed.username ||
    parsed.password ||
    parsed.search ||
    parsed.hash
  ) {
    throw new TypeError(`${name} must be a credential-free public HTTPS URL without query or fragment`);
  }
  return parsed.href.replace(/\/$/, "");
}

function jsonJwks(value: string): NonNullable<Configuration["jwks"]> {
  let parsed: unknown;
  try {
    parsed = JSON.parse(value);
  } catch {
    throw new TypeError("OAUTH_JWKS must be valid JSON");
  }
  if (
    typeof parsed !== "object" ||
    parsed === null ||
    !Array.isArray((parsed as { keys?: unknown }).keys) ||
    (parsed as { keys: unknown[] }).keys.length === 0
  ) {
    throw new TypeError("OAUTH_JWKS must contain at least one key");
  }
  return parsed as NonNullable<Configuration["jwks"]>;
}

function bindHost(value: string): string {
  if (!["0.0.0.0", "::"].includes(value)) {
    throw new TypeError("OAUTH_BIND_HOST must be an all-interface container address");
  }
  return value;
}

export async function schemaIsCurrent(database: PgQueryable): Promise<boolean> {
  const current = await database.query<{ versions: number[] | null }>(
    "SELECT array_agg(version ORDER BY version)::integer[] AS versions FROM oauth_schema_migrations",
  );
  const versions = current.rows[0]?.versions;
  return (
    versions !== null &&
    versions !== undefined &&
    versions.length === OAUTH_SCHEMA_VERSIONS.length &&
    versions.every((version, index) => version === OAUTH_SCHEMA_VERSIONS[index])
  );
}

export function runtimeConfigurationFromEnvironment(
  environment: NodeJS.ProcessEnv,
): OAuthRuntimeConfiguration {
  const issuer = publicHttpsUrl(required(environment, "OAUTH_ISSUER"), "OAUTH_ISSUER");
  if (new URL(issuer).pathname !== "/oauth") {
    throw new TypeError("OAUTH_ISSUER must use the canonical /oauth path");
  }
  const resource = publicHttpsUrl(required(environment, "MCP_RESOURCE"), "MCP_RESOURCE");
  const interactionUrl = publicHttpsUrl(
    required(environment, "OAUTH_INTERACTION_URL"),
    "OAUTH_INTERACTION_URL",
  );
  if (
    new URL(issuer).origin !== new URL(resource).origin ||
    new URL(issuer).origin !== new URL(interactionUrl).origin
  ) {
    throw new TypeError(
      "OAUTH_ISSUER, MCP_RESOURCE, and OAUTH_INTERACTION_URL must share a public origin",
    );
  }
  const cookieKeys = required(environment, "OAUTH_COOKIE_KEYS").split(",");
  if (
    cookieKeys.length < 2 ||
    cookieKeys.some((key) => key.length < 32 || key !== key.trim())
  ) {
    throw new TypeError(
      "OAUTH_COOKIE_KEYS must contain two trimmed secrets of at least 32 characters",
    );
  }
  const resourceServerSecret = required(environment, "OAUTH_RESOURCE_SERVER_SECRET");
  if (resourceServerSecret.length < 32 || resourceServerSecret !== resourceServerSecret.trim()) {
    throw new TypeError(
      "OAUTH_RESOURCE_SERVER_SECRET must be a trimmed secret of at least 32 characters",
    );
  }
  const approvalApiCredential = decodeApprovalApiCredential(
    required(environment, "OAUTH_APPROVAL_API_CREDENTIAL_BASE64URL"),
  );
  const interactionDetailsApiCredential = decodeInteractionDetailsApiCredential(
    required(environment, "OAUTH_INTERACTION_DETAILS_API_CREDENTIAL_BASE64URL"),
  );
  if (Buffer.compare(approvalApiCredential, interactionDetailsApiCredential) === 0) {
    throw new TypeError("OAuth private API credentials must be distinct");
  }
  return {
    issuer,
    resource,
    interactionUrl,
    cookieKeys,
    resourceServerSecret,
    jwks: jsonJwks(required(environment, "OAUTH_JWKS")),
    databaseUrl: databaseUrl(required(environment, "OAUTH_DATABASE_URL")),
    adapterSecret: decodeAdapterSecret(
      required(environment, "OAUTH_ADAPTER_SECRET_BASE64URL"),
    ),
    interactionApprovalSecret: decodeInteractionApprovalSecret(
      required(environment, "OAUTH_INTERACTION_APPROVAL_SECRET_BASE64URL"),
    ),
    approvalApiCredential,
    interactionDetailsApiCredential,
    host: bindHost(environment.OAUTH_BIND_HOST ?? "0.0.0.0"),
    port: serverPort(issuer, environment.PORT),
  };
}

async function createOAuthRuntime(
  configuration: OAuthRuntimeConfiguration,
): Promise<OAuthRuntime> {
  const pool = new Pool({
    connectionString: configuration.databaseUrl,
    connectionTimeoutMillis: 5_000,
  });
  try {
    if (!(await schemaIsCurrent(pool))) {
      throw new Error("OAuth database migrations have not run");
    }
    const provider = createProvider({
      issuer: configuration.issuer,
      resource: configuration.resource,
      interactionUrl: configuration.interactionUrl,
      cookieKeys: configuration.cookieKeys,
      resourceServerSecret: configuration.resourceServerSecret,
      jwks: configuration.jwks,
      adapter: createPostgresAdapter(pool, {
        secret: configuration.adapterSecret,
      }),
    });
    provider.proxy = true;

    let closed = false;
    return {
      provider,
      approvals: new InteractionApprovalStore(pool, configuration.interactionApprovalSecret),
      async isReady() {
        try {
          await pool.query("SELECT 1");
          return true;
        } catch {
          return false;
        }
      },
      async close() {
        if (closed) return;
        closed = true;
        await pool.end();
      },
    };
  } catch (error) {
    await pool.end();
    throw error;
  }
}

export async function startOAuthServer(
  configuration: OAuthRuntimeConfiguration,
): Promise<RunningOAuthServer> {
  const host = bindHost(configuration.host);
  const runtime = await createOAuthRuntime(configuration);
  const providerHandler = providerHttpHandler(runtime.provider, configuration.issuer);
  const basePath = new URL(configuration.issuer).pathname.replace(/\/$/, "");
  const server = createServer((request, response) => {
    if (request.method === "GET" && request.url === "/health/live") {
      response.writeHead(200, { "content-type": "application/json" }).end('{"status":"ok"}');
      return;
    }
    if (request.method === "GET" && request.url === "/health/ready") {
      void runtime.isReady().then((ready) => {
        response
          .writeHead(ready ? 200 : 503, { "content-type": "application/json" })
          .end(ready ? '{"status":"ok"}' : '{"status":"unavailable"}');
      });
      return;
    }
    void handleInteractionBridgeRequest(
      runtime.provider,
      runtime.approvals,
      configuration.approvalApiCredential,
      configuration.interactionDetailsApiCredential,
      basePath,
      request,
      response,
    )
      .then((handled) => {
        if (!handled) providerHandler(request, response);
      })
      .catch(() => response.writeHead(500).end());
  });
  try {
    server.listen(configuration.port, host);
    await once(server, "listening");
  } catch (error) {
    await runtime.close();
    throw error;
  }

  let closed = false;
  return {
    ...runtime,
    server,
    async close() {
      if (closed) return;
      closed = true;
      try {
        await server[Symbol.asyncDispose]();
      } finally {
        await runtime.close();
      }
    },
  };
}
