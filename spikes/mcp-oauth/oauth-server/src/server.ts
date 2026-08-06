import { createServer } from "node:http";

import { createProvider, providerHttpHandler, serverPort } from "./provider-profile.js";

function required(name: string): string {
  const value = process.env[name];
  if (!value) throw new Error(`${name} is required`);
  return value;
}

const issuer = required("OAUTH_ISSUER");
if (process.env.NODE_ENV === "production") {
  throw new Error("The disposable OAuth spike must not run with NODE_ENV=production");
}
const provider = createProvider({
  issuer,
  resource: required("MCP_RESOURCE"),
  redirectUri: required("MCP_CLIENT_REDIRECT_URI"),
  cookieKeys: required("OAUTH_COOKIE_KEYS").split(","),
  resourceServerSecret: required("OAUTH_RESOURCE_SERVER_SECRET"),
  jwks: JSON.parse(required("OAUTH_JWKS")),
});
provider.proxy = true;

const port = serverPort(issuer, process.env.PORT);
createServer(providerHttpHandler(provider, issuer)).listen(
  port,
  process.env.HOST ?? "127.0.0.1",
);
