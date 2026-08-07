import {
  errors,
  Provider,
  type AdapterFactory,
  type Configuration,
} from "oidc-provider";
import type { IncomingMessage, ServerResponse } from "node:http";

export const MCP_SCOPE = "cookops:mcp";
export const PUBLIC_CLIENT_ID = "cookops-spike-client";
export const RESOURCE_SERVER_CLIENT_ID = "cookops-resource-server";

export interface ProviderProfile {
  issuer: string;
  resource: string;
  redirectUri: string;
  cookieKeys: string[];
  resourceServerSecret: string;
  jwks: NonNullable<Configuration["jwks"]>;
  adapter: AdapterFactory;
}

function endpoint(name: string, value: string): URL {
  const url = URL.parse(value);
  if (!url) {
    throw new TypeError(`${name} must be an absolute URL`);
  }

  const loopback = url.hostname === "127.0.0.1" || url.hostname === "localhost";
  if (url.protocol !== "https:" && !(loopback && url.protocol === "http:")) {
    throw new TypeError(`${name} must use HTTPS outside loopback development`);
  }
  if (url.hash || (name !== "redirectUri" && url.search)) {
    throw new TypeError(`${name} must not contain a query or fragment`);
  }
  return url;
}

export function createProvider(profile: ProviderProfile): Provider {
  const issuerUrl = endpoint("issuer", profile.issuer);
  const issuer = issuerUrl.href.replace(/\/$/, "");
  const basePath = issuerUrl.pathname.replace(/\/$/, "");
  const resource = endpoint("resource", profile.resource).href.replace(/\/$/, "");
  const redirectUri = endpoint("redirectUri", profile.redirectUri).href;
  if (profile.cookieKeys.some((key) => key.length < 32) || profile.cookieKeys.length < 2) {
    throw new TypeError("cookieKeys must contain two secrets of at least 32 characters");
  }
  if (profile.resourceServerSecret.length < 32) {
    throw new TypeError("resourceServerSecret must contain at least 32 characters");
  }
  if (!Array.isArray(profile.jwks?.keys) || profile.jwks.keys.length === 0) {
    throw new TypeError("jwks must contain at least one private signing key");
  }

  const configuration: Configuration = {
    adapter: profile.adapter,
    clients: [
      {
        client_id: PUBLIC_CLIENT_ID,
        client_name: "CookOps OAuth spike client",
        application_type: "native",
        redirect_uris: [redirectUri],
        response_types: ["code"],
        grant_types: ["authorization_code", "refresh_token"],
        token_endpoint_auth_method: "none",
      },
      {
        client_id: RESOURCE_SERVER_CLIENT_ID,
        client_secret: profile.resourceServerSecret,
        redirect_uris: [],
        response_types: [],
        grant_types: [],
        token_endpoint_auth_method: "client_secret_basic",
      },
    ],
    cookies: { keys: profile.cookieKeys },
    features: {
      // Spike follow-up: add edge rate limits before testing CIMD or DCR beyond loopback.
      clientIdMetadataDocument: { enabled: true, ack: "draft-02" },
      devInteractions: { enabled: false },
      introspection: {
        enabled: true,
        allowedPolicy: (_ctx, client) => client.clientId === RESOURCE_SERVER_CLIENT_ID,
      },
      registration: { enabled: true },
      resourceIndicators: {
        enabled: true,
        // Spike follow-up: prove token-request presence with a complete code flow.
        defaultResource: () => {
          throw new errors.InvalidTarget("resource is required by MCP");
        },
        useGrantedResource: () => false,
        getResourceServerInfo: (ctx, requestedResource) => {
          if (
            (ctx.oidc.route === "authorization" || ctx.oidc.route === "token") &&
            !ctx.oidc.params?.resource
          ) {
            throw new errors.InvalidTarget("resource is required by MCP");
          }
          if (requestedResource !== resource) {
            throw new errors.InvalidTarget();
          }
          return {
            scope: MCP_SCOPE,
            audience: resource,
            accessTokenFormat: "opaque",
            accessTokenTTL: 15 * 60,
          };
        },
      },
      revocation: { enabled: true },
    },
    interactions: {
      url: (_ctx, interaction) => `${issuer}/interaction/${interaction.uid}`,
    },
    issueRefreshToken: (_ctx, client) => client.grantTypeAllowed("refresh_token"),
    jwks: profile.jwks,
    pkce: { required: () => true },
    responseTypes: ["code"],
    routes: {
      authorization: `${basePath}/authorize`,
      introspection: `${basePath}/introspect`,
      jwks: `${basePath}/jwks`,
      registration: `${basePath}/register`,
      revocation: `${basePath}/revoke`,
      token: `${basePath}/token`,
    },
    scopes: [MCP_SCOPE],
  };

  return new Provider(issuer, configuration);
}

export function providerHttpHandler(
  provider: Provider,
  issuer: string,
): (request: IncomingMessage, response: ServerResponse) => void {
  const callback = provider.callback();
  const basePath = new URL(issuer).pathname.replace(/\/$/, "");
  return (request, response) => {
    const url = new URL(request.url ?? "/", "http://localhost");
    if (basePath && url.pathname === `${basePath}/.well-known/openid-configuration`) {
      url.pathname = "/.well-known/openid-configuration";
      request.url = `${url.pathname}${url.search}`;
    } else if (
      basePath &&
      url.pathname === `/.well-known/openid-configuration${basePath}`
    ) {
      url.pathname = "/.well-known/openid-configuration";
      request.url = `${url.pathname}${url.search}`;
    } else if (
      basePath &&
      url.pathname === `/.well-known/oauth-authorization-server${basePath}`
    ) {
      url.pathname = "/.well-known/oauth-authorization-server";
      request.url = `${url.pathname}${url.search}`;
    }
    callback(request, response);
  };
}

export function serverPort(issuer: string, configuredPort?: string): number {
  const value = configuredPort || new URL(issuer).port || "3000";
  const port = Number(value);
  if (!Number.isInteger(port) || port < 1 || port > 65_535) {
    throw new TypeError("PORT must be an integer between 1 and 65535");
  }
  return port;
}
