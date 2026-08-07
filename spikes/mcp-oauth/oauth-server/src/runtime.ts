import { once } from "node:events";
import { createServer, type Server } from "node:http";

import type { Configuration, Provider } from "oidc-provider";
import { Pool } from "pg";

import {
  createPostgresAdapter,
  initializePostgresAdapter,
} from "./postgres-adapter.js";
import {
  createProvider,
  providerHttpHandler,
  serverPort,
} from "./provider-profile.js";

export interface OAuthRuntimeConfiguration {
  issuer: string;
  resource: string;
  redirectUri: string;
  cookieKeys: string[];
  resourceServerSecret: string;
  jwks: NonNullable<Configuration["jwks"]>;
  databaseUrl: string;
  adapterSecret: Uint8Array;
  host: string;
  port: number;
}

interface OAuthRuntime {
  provider: Provider;
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

function loopbackHost(value: string): string {
  if (!["127.0.0.1", "::1"].includes(value)) {
    throw new TypeError("The disposable OAuth spike must bind only to loopback");
  }
  return value;
}

export function runtimeConfigurationFromEnvironment(
  environment: NodeJS.ProcessEnv,
): OAuthRuntimeConfiguration {
  if (
    environment.NODE_ENV === "production" ||
    environment.COOKOPS_ENVIRONMENT === "production"
  ) {
    throw new Error("The disposable OAuth spike must not run in production");
  }

  const issuer = required(environment, "OAUTH_ISSUER");
  return {
    issuer,
    resource: required(environment, "MCP_RESOURCE"),
    redirectUri: required(environment, "MCP_CLIENT_REDIRECT_URI"),
    cookieKeys: required(environment, "OAUTH_COOKIE_KEYS").split(","),
    resourceServerSecret: required(environment, "OAUTH_RESOURCE_SERVER_SECRET"),
    jwks: JSON.parse(required(environment, "OAUTH_JWKS")) as NonNullable<
      Configuration["jwks"]
    >,
    databaseUrl: databaseUrl(required(environment, "OAUTH_DATABASE_URL")),
    adapterSecret: decodeAdapterSecret(
      required(environment, "OAUTH_ADAPTER_SECRET_BASE64URL"),
    ),
    host: loopbackHost(environment.HOST ?? "127.0.0.1"),
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
    await initializePostgresAdapter(pool);
    const provider = createProvider({
      issuer: configuration.issuer,
      resource: configuration.resource,
      redirectUri: configuration.redirectUri,
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
  const host = loopbackHost(configuration.host);
  const runtime = await createOAuthRuntime(configuration);
  const server = createServer(
    providerHttpHandler(runtime.provider, configuration.issuer),
  );
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
