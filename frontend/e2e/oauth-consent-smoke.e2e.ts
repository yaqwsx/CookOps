import { createHash, randomBytes } from "node:crypto";

import { expect, test } from "@playwright/test";

test("a seeded CookOps browser session completes a real provider consent interaction", async ({
  context,
  page,
  baseURL,
}) => {
  if (!baseURL) throw new Error("browser smoke base URL is unavailable");
  const origin = baseURL;
  await page.goto(`${origin}/auth/dummy/identities`);
  const registration = await page.evaluate(async () => {
    const response = await fetch("/oauth/register", {
      body: JSON.stringify({
        application_type: "web",
        client_name: "CookOps consent smoke",
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
  });
  expect(registration.status, JSON.stringify(registration.body)).toBe(201);
  const client = registration.body as { client_id: string };
  const verifier = randomBytes(48).toString("base64url");
  const challenge = createHash("sha256").update(verifier).digest("base64url");
  const authorize = new URL(`${origin}/oauth/authorize`);
  authorize.search = new URLSearchParams({
    client_id: client.client_id,
    code_challenge: challenge,
    code_challenge_method: "S256",
    redirect_uri: `${origin}/callback`,
    resource: `${origin}/mcp`,
    response_type: "code",
    scope: "cookops:mcp",
    state: "smoke-state",
  }).toString();

  const sessionStatus = await page.evaluate(async () => {
    const response = await fetch("/auth/dummy/session", {
      body: JSON.stringify({ subject: "dummy-member" }),
      headers: { "content-type": "application/json" },
      method: "POST",
    });
    return response.status;
  });
  expect(sessionStatus).toBe(204);

  await page.goto(authorize.href);
  expect(
    (await context.cookies()).filter(({ name }) =>
      name.includes("interaction"),
    ),
  ).toEqual(
    expect.arrayContaining([
      expect.objectContaining({
        httpOnly: true,
        path: "/oauth",
        sameSite: "Lax",
        secure: true,
      }),
    ]),
  );
  await expect(
    page.getByRole("heading", { name: "Allow CookOps consent smoke?" }),
  ).toBeVisible();
  await expect(page.getByText(`Resource: ${origin}/mcp`)).toBeVisible();
  const loginCompletion = page.waitForResponse((response) =>
    response.url().includes("/oauth/interaction/"),
  );
  await page.getByRole("button", { name: "Allow" }).click();
  expect((await loginCompletion).status()).toBe(303);
  await page.waitForURL(`${origin}/auth/mcp-interactions/**`);
  await expect(
    page.getByRole("heading", { name: "Allow CookOps consent smoke?" }),
  ).toBeVisible();
  const consentCompletion = page.waitForResponse((response) =>
    response.url().includes("/oauth/interaction/"),
  );
  await page.getByRole("button", { name: "Allow" }).click();
  expect((await consentCompletion).status()).toBe(303);
  await page.waitForURL(`${origin}/callback?*`);
  const completed = new URL(page.url());
  expect(completed.searchParams.get("code")).toBeTruthy();
  expect(completed.searchParams.get("state")).toBe("smoke-state");
});
