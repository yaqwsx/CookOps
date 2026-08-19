export type CurrentIdentity = {
  id: string;
  display_name: string;
  verified_email: string;
  preferred_locale: "cs" | "en";
};

export type DevelopmentIdentity = {
  subject: string;
  display_name: string;
};
export type AuthorizedGrant = { handle: string; clientId: string; issuedAt?: number; expiresAt?: number };

export class AuthenticationRequestError extends Error {
  constructor(readonly status: number) {
    super("Authentication request failed.");
  }
}

async function request(path: string, init?: RequestInit): Promise<Response> {
  return fetch(path, { credentials: "same-origin", ...init });
}

async function requireSuccess(response: Response): Promise<Response> {
  if (!response.ok) {
    throw new AuthenticationRequestError(response.status);
  }
  return response;
}

export async function getCurrentIdentity(): Promise<CurrentIdentity | null> {
  const response = await request("/auth/session");
  if (response.status === 401) {
    return null;
  }
  return (await requireSuccess(response).then((result) =>
    result.json(),
  )) as CurrentIdentity;
}

export async function setCurrentIdentityLocale(
  preferred_locale: "cs" | "en",
): Promise<CurrentIdentity> {
  const response = await requireSuccess(await request("/auth/session/locale", {
    method: "PATCH",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ preferred_locale }),
  }));
  return (await response.json()) as CurrentIdentity;
}

export async function getDevelopmentIdentities(): Promise<
  DevelopmentIdentity[]
> {
  const response = await request("/auth/dummy/identities");
  const payload = (await requireSuccess(response).then((result) =>
    result.json(),
  )) as {
    identities: DevelopmentIdentity[];
  };
  return payload.identities;
}

export async function createDevelopmentSession(subject: string): Promise<void> {
  await requireSuccess(
    await request("/auth/dummy/session", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ subject }),
    }),
  );
}

export async function createGoogleSession(idToken: string): Promise<void> {
  await requireSuccess(
    await request("/auth/google/session", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ id_token: idToken }),
    }),
  );
}

export async function logout(): Promise<void> {
  await requireSuccess(
    await request("/auth/session/logout", { method: "POST" }),
  );
}

export async function getAuthorizedGrants(): Promise<AuthorizedGrant[]> {
  const response = await requireSuccess(await request("/auth/mcp-grants"));
  return (await response.json()) as AuthorizedGrant[];
}

export async function revokeAuthorizedGrant(handle: string): Promise<void> {
  await requireSuccess(await request(`/auth/mcp-grants/${handle}`, { method: "DELETE" }));
}
