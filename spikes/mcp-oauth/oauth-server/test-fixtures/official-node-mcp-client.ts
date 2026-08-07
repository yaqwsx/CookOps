import assert from "node:assert/strict";

import {
  auth,
  type OAuthClientProvider,
  type OAuthDiscoveryState,
} from "@modelcontextprotocol/sdk/client/auth.js";
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StreamableHTTPClientTransport } from "@modelcontextprotocol/sdk/client/streamableHttp.js";
import type {
  OAuthClientInformationMixed,
  OAuthClientMetadata,
  OAuthTokens,
} from "@modelcontextprotocol/sdk/shared/auth.js";

const CLIENT_ID = "cookops-spike-client";
const SCOPE = "cookops:mcp";

function required(name: string): string {
  const value = process.env[name];
  if (!value) throw new Error(`${name} is required`);
  return value;
}

class CookieJar {
  readonly #cookies = new Map<string, string>();

  async fetch(url: string): Promise<Response> {
    const headers = new Headers();
    if (this.#cookies.size) {
      headers.set(
        "cookie",
        [...this.#cookies].map(([name, value]) => `${name}=${value}`).join("; "),
      );
    }
    const response = await fetch(url, { headers, redirect: "manual" });
    for (const cookie of response.headers.getSetCookie()) {
      const [pair = ""] = cookie.split(";", 1);
      const separator = pair.indexOf("=");
      if (separator !== -1) {
        this.#cookies.set(pair.slice(0, separator), pair.slice(separator + 1));
      }
    }
    return response;
  }
}

class OfficialSdkOAuthClient implements OAuthClientProvider {
  #authorizationUrl: URL | undefined;
  #codeVerifier: string | undefined;
  #discoveryState: OAuthDiscoveryState | undefined;
  #tokens: OAuthTokens | undefined;

  constructor(
    readonly redirectUrl: URL,
    private readonly expectedIssuer: string,
  ) {}

  get clientMetadata(): OAuthClientMetadata {
    return {
      redirect_uris: [this.redirectUrl.toString()],
      token_endpoint_auth_method: "none",
    };
  }

  clientInformation(): OAuthClientInformationMixed {
    return { client_id: CLIENT_ID, token_endpoint_auth_method: "none" };
  }

  tokens(): OAuthTokens | undefined {
    return this.#tokens;
  }

  saveTokens(tokens: OAuthTokens): void {
    this.#tokens = tokens;
  }

  redirectToAuthorization(authorizationUrl: URL): void {
    this.#authorizationUrl = authorizationUrl;
  }

  saveCodeVerifier(codeVerifier: string): void {
    this.#codeVerifier = codeVerifier;
  }

  codeVerifier(): string {
    assert.ok(this.#codeVerifier);
    return this.#codeVerifier;
  }

  state(): string {
    return "official-node-sdk-state";
  }

  saveDiscoveryState(state: OAuthDiscoveryState): void {
    this.#discoveryState = state;
  }

  discoveryState(): OAuthDiscoveryState | undefined {
    return this.#discoveryState;
  }

  async completeInteractiveAuthorization(): Promise<string> {
    assert.ok(this.#authorizationUrl);
    const jar = new CookieJar();
    let current = this.#authorizationUrl.href;
    for (let redirects = 0; redirects < 10; redirects += 1) {
      const response = await jar.fetch(current);
      assert([302, 303].includes(response.status), `unexpected ${response.status}`);
      const location = response.headers.get("location");
      assert.ok(location);
      const redirect = new URL(location, current);
      if (redirect.href.startsWith(this.redirectUrl.href)) {
        assert.equal(redirect.searchParams.get("state"), this.state());
        assert.equal(redirect.searchParams.get("iss"), this.expectedIssuer);
        const code = redirect.searchParams.get("code");
        assert.ok(code);
        return code;
      }
      current = redirect.href;
    }
    throw new Error("official SDK authorization exceeded redirect limit");
  }
}

async function main(): Promise<void> {
  const resource = new URL(required("MCP_E2E_RESOURCE"));
  const issuer = required("MCP_E2E_ISSUER");
  const subject = required("MCP_E2E_SUBJECT");
  const provider = new OfficialSdkOAuthClient(
    new URL("/callback", resource),
    issuer,
  );

  assert.equal(await auth(provider, { serverUrl: resource }), "REDIRECT");
  const code = await provider.completeInteractiveAuthorization();
  assert.equal(
    await auth(provider, { authorizationCode: code, serverUrl: resource }),
    "AUTHORIZED",
  );
  assert.ok(provider.tokens()?.access_token);
  assert.equal(provider.tokens()?.access_token.includes("."), false);

  const transport = new StreamableHTTPClientTransport(resource, { authProvider: provider });
  const client = new Client({ name: "cookops-official-node-sdk-spike", version: "0.0.0" });
  // The SDK's own transport declaration is not exact-optional compatible with
  // its generic Transport declaration. Runtime values are the same SDK type.
  await client.connect(transport as never);
  try {
    const result = await client.callTool({ name: "authenticated_identity" });
    assert.equal(result.isError, false);
    assert.deepEqual(result.structuredContent, {
      client_id: CLIENT_ID,
      resource: resource.href,
      subject,
    });
  } finally {
    await transport.close();
  }
}

await main();
