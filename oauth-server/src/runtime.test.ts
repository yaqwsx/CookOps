import assert from "node:assert/strict";
import test from "node:test";

import { runtimeConfigurationFromEnvironment } from "./runtime.js";

const secret = Buffer.alloc(32, 7).toString("base64url");
const environment: NodeJS.ProcessEnv = {
  OAUTH_ISSUER: "https://cookops.example/oauth",
  MCP_RESOURCE: "https://cookops.example/mcp",
  OAUTH_INTERACTION_URL: "https://cookops.example/auth/mcp-interactions",
  OAUTH_COOKIE_KEYS: `${"a".repeat(32)},${"b".repeat(32)}`,
  OAUTH_RESOURCE_SERVER_SECRET: "c".repeat(32),
  OAUTH_JWKS: JSON.stringify({ keys: [{}] }),
  OAUTH_DATABASE_URL: "postgresql://oauth:secret@postgres/cookops",
  OAUTH_ADAPTER_SECRET_BASE64URL: secret,
  OAUTH_INTERACTION_APPROVAL_SECRET_BASE64URL: secret,
  OAUTH_APPROVAL_API_CREDENTIAL_BASE64URL: secret,
  OAUTH_INTERACTION_DETAILS_API_CREDENTIAL_BASE64URL: Buffer.alloc(32, 8).toString("base64url"),
};

test("production configuration accepts a private-container binding and public URLs", () => {
  const configuration = runtimeConfigurationFromEnvironment(environment);

  assert.equal(configuration.host, "0.0.0.0");
  assert.equal(configuration.port, 3000);
  assert.equal(configuration.resource, "https://cookops.example/mcp");
});

test("production configuration rejects mixed origins, plaintext URLs, and non-container bindings", () => {
  assert.throws(
    () => runtimeConfigurationFromEnvironment({ ...environment, MCP_RESOURCE: "https://other.example/mcp" }),
    /share a public origin/,
  );
  assert.throws(
    () => runtimeConfigurationFromEnvironment({ ...environment, OAUTH_ISSUER: "http://cookops.example/oauth" }),
    /public HTTPS URL/,
  );
  assert.throws(
    () => runtimeConfigurationFromEnvironment({ ...environment, OAUTH_ISSUER: "https://cookops.example/custom" }),
    /canonical \/oauth path/,
  );
  assert.throws(
    () => runtimeConfigurationFromEnvironment({ ...environment, OAUTH_BIND_HOST: "127.0.0.1" }),
    /all-interface container address/,
  );
});
