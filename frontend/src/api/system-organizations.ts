import { readOrCreateBrowserInstallationId } from "../local-db";

export class SystemOrganizationRequestError extends Error {
  constructor(readonly status: number) {
    super("System organization request failed.");
  }
}

export async function getSystemAdministrationAccess(): Promise<boolean> {
  const response = await fetch("/api/v1/system/organizations/access", {
    credentials: "same-origin",
  });
  if (response.status === 403 || response.status === 401) return false;
  if (!response.ok) throw new SystemOrganizationRequestError(response.status);
  return true;
}

export async function createSystemOrganization(
  userId: string,
  input: { name: string; description: string | null; defaultCurrency: string },
): Promise<{ id: string; name: string }> {
  const response = await fetch("/api/v1/system/organizations", {
    method: "POST",
    credentials: "same-origin",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({
      mutation_id: crypto.randomUUID(),
      organization_id: crypto.randomUUID(),
      client_installation_id: await readOrCreateBrowserInstallationId(userId),
      client_wall_time: new Date().toISOString(),
      name: input.name,
      description: input.description,
      default_currency: input.defaultCurrency,
    }),
  });
  if (!response.ok) {
    throw new SystemOrganizationRequestError(response.status);
  }
  return (await response.json()) as { id: string; name: string };
}

export type SystemOrganization = {
  id: string;
  name: string;
  description: string | null;
  default_currency: string;
  retired_at: string | null;
  retired_by_user_id: string | null;
};

export async function getSystemOrganizations(): Promise<SystemOrganization[]> {
  const response = await fetch("/api/v1/system/organizations", { credentials: "same-origin" });
  if (!response.ok) throw new SystemOrganizationRequestError(response.status);
  return (await response.json()) as SystemOrganization[];
}

export async function changeSystemOrganizationLifecycle(
  userId: string, id: string, operation: "retire" | "restore",
): Promise<SystemOrganization> {
  const response = await fetch(`/api/v1/system/organizations/${id}/lifecycle`, {
    method: "POST", credentials: "same-origin", headers: { "content-type": "application/json" },
    body: JSON.stringify({
      operation,
      mutation_id: crypto.randomUUID(),
      client_installation_id: await readOrCreateBrowserInstallationId(userId),
      client_wall_time: new Date().toISOString(),
    }),
  });
  if (!response.ok) throw new SystemOrganizationRequestError(response.status);
  return (await response.json()) as SystemOrganization;
}

export async function editSystemOrganization(
  userId: string,
  id: string,
  input: { name: string; description: string | null; defaultCurrency: string },
): Promise<SystemOrganization> {
  const response = await fetch(`/api/v1/system/organizations/${id}`, {
    method: "PATCH", credentials: "same-origin", headers: { "content-type": "application/json" },
    body: JSON.stringify({
      mutation_id: crypto.randomUUID(),
      client_installation_id: await readOrCreateBrowserInstallationId(userId),
      client_wall_time: new Date().toISOString(),
      name: input.name,
      description: input.description,
      default_currency: input.defaultCurrency,
    }),
  });
  if (!response.ok) throw new SystemOrganizationRequestError(response.status);
  return (await response.json()) as SystemOrganization;
}
