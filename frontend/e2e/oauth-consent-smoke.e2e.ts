import { createHash, randomBytes } from "node:crypto";

import { expect, test } from "@playwright/test";

test("a seeded CookOps browser session completes a real provider consent interaction", async ({
  context,
  page,
  baseURL,
  request,
}) => {
  if (!baseURL) throw new Error("browser smoke base URL is unavailable");
  const origin = baseURL;
  await page.goto(`${origin}/auth/dummy/identities`);
  const registerClient = async (name: string) =>
    page.evaluate(async (clientName) => {
      const response = await fetch("/oauth/register", {
        body: JSON.stringify({
          application_type: "web",
          client_name: clientName,
          grant_types: ["authorization_code", "refresh_token"],
          id_token_signed_response_alg: "ES256",
          redirect_uris: [`${location.origin}/callback`],
          response_types: ["code"],
          token_endpoint_auth_method: "none",
        }),
        headers: { "content-type": "application/json" },
        method: "POST",
      });
      return { body: await response.json(), status: response.status };
    }, name);
  const registration = await registerClient("CookOps consent smoke");
  expect(registration.status, JSON.stringify(registration.body)).toBe(201);
  const client = registration.body as { client_id: string };
  const sessionStatus = await page.evaluate(async () => {
    const response = await fetch("/auth/dummy/session", {
      body: JSON.stringify({ subject: "dummy-member" }),
      headers: { "content-type": "application/json" },
      method: "POST",
    });
    return response.status;
  });
  expect(sessionStatus).toBe(204);

  const authorizeAndConsent = async (
    clientId: string,
    clientName: string,
    state: string,
  ) => {
    await context.clearCookies();
    await page.goto(`${origin}/auth/dummy/identities`);
    expect(
      await page.evaluate(
        async () =>
          (
            await fetch("/auth/dummy/session", {
              body: JSON.stringify({ subject: "dummy-member" }),
              headers: { "content-type": "application/json" },
              method: "POST",
            })
          ).status,
      ),
    ).toBe(204);
    const verifier = randomBytes(48).toString("base64url");
    const authorize = new URL(`${origin}/oauth/authorize`);
    authorize.search = new URLSearchParams({
      client_id: clientId,
      code_challenge: createHash("sha256").update(verifier).digest("base64url"),
      code_challenge_method: "S256",
      redirect_uri: `${origin}/callback`,
      resource: `${origin}/mcp`,
      response_type: "code",
      scope: "cookops:mcp",
      state,
    }).toString();
    await page.goto(authorize.href);
    await page.waitForURL(
      (url) =>
        url.pathname.startsWith("/auth/mcp-interactions/") ||
        url.pathname === "/callback",
    );
    if (new URL(page.url()).pathname !== "/callback") {
      await expect(
        page.getByRole("heading", { name: `Allow ${clientName}?` }),
      ).toBeVisible();
      const loginCompletion = page.waitForResponse((response) =>
        response.url().includes("/oauth/interaction/"),
      );
      await page.getByRole("button", { name: "Allow" }).click();
      expect((await loginCompletion).status()).toBe(303);
      await page.waitForURL(
        (url) =>
          url.pathname.startsWith("/auth/mcp-interactions/") ||
          url.pathname === "/callback",
      );
    }
    if (new URL(page.url()).pathname.startsWith("/auth/mcp-interactions/")) {
      await expect(
        page.getByRole("heading", { name: `Allow ${clientName}?` }),
      ).toBeVisible();
      const consentCompletion = page.waitForResponse((response) =>
        response.url().includes("/oauth/interaction/"),
      );
      await page.getByRole("button", { name: "Allow" }).click();
      expect((await consentCompletion).status()).toBe(303);
    }
    await page.waitForURL(`${origin}/callback?*`);
    const completed = new URL(page.url());
    expect(completed.searchParams.get("state")).toBe(state);
    const code = completed.searchParams.get("code");
    expect(code).toBeTruthy();
    return { code: code as string, verifier };
  };
  const initial = await authorizeAndConsent(
    client.client_id,
    "CookOps consent smoke",
    "smoke-state",
  );

  const tokenEndpoint = `${origin}/oauth/token`;
  const introspectionEndpoint = `${origin}/oauth/introspect`;
  const resourceSecret = process.env.COOKOPS_OAUTH_SMOKE_RESOURCE_SECRET;
  if (!resourceSecret)
    throw new Error("OAuth smoke resource secret is unavailable");
  const form = (values: Record<string, string | undefined>) =>
    Object.fromEntries(
      Object.entries(values).filter(
        (entry): entry is [string, string] => entry[1] !== undefined,
      ),
    );
  const tokenRequest = (values: Record<string, string | undefined>) =>
    request.post(tokenEndpoint, {
      form: form(values),
      failOnStatusCode: false,
      maxRedirects: 0,
    });
  const introspect = (token: string) =>
    request.post(introspectionEndpoint, {
      form: form({ token, resource: `${origin}/mcp` }),
      failOnStatusCode: false,
      headers: {
        authorization: `Basic ${Buffer.from(`cookops-resource-server:${resourceSecret}`).toString("base64")}`,
      },
      maxRedirects: 0,
    });
  const exchange = {
    client_id: client.client_id,
    code: initial.code,
    code_verifier: initial.verifier,
    grant_type: "authorization_code",
    redirect_uri: `${origin}/callback`,
    resource: `${origin}/mcp`,
  };

  const tokenResponse = await tokenRequest(exchange);
  expect(tokenResponse.status()).toBe(200);
  const tokenBody = (await tokenResponse.json()) as Record<string, unknown>;
  expect(typeof tokenBody.access_token).toBe("string");
  expect(tokenBody.token_type).toBe("Bearer");
  expect(typeof tokenBody.expires_in).toBe("number");
  expect(typeof tokenBody.refresh_token).toBe("string");
  expect(tokenBody.scope).toBe("cookops:mcp");
  const accessToken = tokenBody.access_token as string;
  const refreshToken = tokenBody.refresh_token as string;

  const activeResponse = await introspect(accessToken);
  const activeBody = (await activeResponse.json()) as Record<string, unknown>;
  expect(
    activeResponse.status(),
    JSON.stringify({
      error: activeBody.error,
      error_description: activeBody.error_description,
    }),
  ).toBe(200);
  expect(activeBody).toMatchObject({
    active: true,
    client_id: client.client_id,
    aud: `${origin}/mcp`,
    scope: "cookops:mcp",
    token_type: "Bearer",
  });
  expect(typeof activeBody.sub).toBe("string");

  const refreshResponse = await tokenRequest({
    client_id: client.client_id,
    grant_type: "refresh_token",
    refresh_token: refreshToken,
    resource: `${origin}/mcp`,
  });
  const refreshedBody = (await refreshResponse.json()) as Record<
    string,
    unknown
  >;
  expect(
    refreshResponse.status(),
    JSON.stringify({
      error: refreshedBody.error,
      error_description: refreshedBody.error_description,
    }),
  ).toBe(200);
  expect(typeof refreshedBody.access_token).toBe("string");
  expect(typeof refreshedBody.refresh_token).toBe("string");
  const rotatedAccessToken = refreshedBody.access_token as string;

  const replayResponse = await tokenRequest({
    client_id: client.client_id,
    grant_type: "refresh_token",
    refresh_token: refreshToken,
    resource: `${origin}/mcp`,
  });
  expect(replayResponse.status()).toBe(400);
  const revokedResponse = await introspect(rotatedAccessToken);
  expect(revokedResponse.status()).toBe(200);
  expect((await revokedResponse.json()).active).toBe(false);

  const replayRegistration = await registerClient("CookOps code replay smoke");
  expect(replayRegistration.status).toBe(201);
  const replayClient = replayRegistration.body as { client_id: string };
  const replayable = await authorizeAndConsent(
    replayClient.client_id,
    "CookOps code replay smoke",
    "smoke-code-replay",
  );
  const replayableExchange = await tokenRequest({
    ...exchange,
    client_id: replayClient.client_id,
    code: replayable.code,
    code_verifier: replayable.verifier,
  });
  expect(replayableExchange.status()).toBe(200);
  const reusedCode = await tokenRequest({
    ...exchange,
    client_id: replayClient.client_id,
    code: replayable.code,
    code_verifier: replayable.verifier,
  });
  expect(reusedCode.status()).toBe(400);

  for (const invalid of [
    "wrong-resource",
    "missing-verifier",
    "wrong-verifier",
  ]) {
    const invalidRegistration = await registerClient(
      `CookOps ${invalid} smoke`,
    );
    expect(invalidRegistration.status).toBe(201);
    const invalidClient = invalidRegistration.body as { client_id: string };
    const fresh = await authorizeAndConsent(
      invalidClient.client_id,
      `CookOps ${invalid} smoke`,
      `smoke-${invalid}`,
    );
    const invalidExchange = {
      ...exchange,
      client_id: invalidClient.client_id,
      code: fresh.code,
      code_verifier: fresh.verifier,
    };
    if (invalid === "wrong-resource")
      invalidExchange.resource = `${origin}/wrong-resource`;
    if (invalid === "missing-verifier")
      invalidExchange.code_verifier = undefined;
    if (invalid === "wrong-verifier")
      invalidExchange.code_verifier = `${fresh.verifier}wrong`;
    expect((await tokenRequest(invalidExchange)).status()).toBe(400);
  }
});
