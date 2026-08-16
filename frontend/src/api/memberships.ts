import { readOrCreateBrowserInstallationId } from "../local-db";

export type OrganizationMembership = {
  id: string;
  invitedEmail: string;
  role: "member" | "organization_admin";
  state: "invited" | "active";
};

export class MembershipRequestError extends Error {
  constructor(readonly status: number) {
    super("Membership request failed.");
  }
}

const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

function record(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function membership(value: unknown): OrganizationMembership {
  if (
    !record(value) ||
    typeof value.id !== "string" ||
    !uuid.test(value.id) ||
    typeof value.invited_email !== "string" ||
    typeof value.role !== "string" ||
    (value.role !== "member" && value.role !== "organization_admin") ||
    typeof value.state !== "string" ||
    (value.state !== "invited" && value.state !== "active")
  )
    throw new Error("Invalid membership response.");
  return {
    id: value.id,
    invitedEmail: value.invited_email,
    role: value.role,
    state: value.state,
  };
}

function path(organizationId: string) {
  return `/api/v1/organizations/${organizationId}/members`;
}

export async function getMemberships(
  organizationId: string,
): Promise<OrganizationMembership[]> {
  const response = await fetch(path(organizationId), {
    credentials: "same-origin",
  });
  if (!response.ok) throw new MembershipRequestError(response.status);
  const value = await response.json();
  if (!record(value) || !Array.isArray(value.memberships))
    throw new Error("Invalid membership response.");
  return value.memberships.map(membership);
}

async function mutate(
  organizationId: string,
  userId: string,
  suffix: string,
  values: Record<string, string>,
) {
  const response = await fetch(`${path(organizationId)}${suffix}`, {
    method: "POST",
    credentials: "same-origin",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({
      mutation_id: crypto.randomUUID(),
      client_installation_id: await readOrCreateBrowserInstallationId(userId),
      client_wall_time: new Date().toISOString(),
      ...values,
    }),
  });
  if (!response.ok) throw new MembershipRequestError(response.status);
}

export function inviteMember(
  organizationId: string,
  userId: string,
  email: string,
) {
  return mutate(organizationId, userId, "/invitations", {
    invited_email: email,
  });
}

export function removeMember(
  organizationId: string,
  userId: string,
  membershipId: string,
) {
  return mutate(organizationId, userId, `/${membershipId}/remove`, {});
}

export function assignOrganizationAdmin(
  organizationId: string,
  userId: string,
  membershipId: string,
) {
  return mutate(organizationId, userId, `/${membershipId}/assign-organization-admin`, {});
}

export function revokeOrganizationAdmin(
  organizationId: string,
  userId: string,
  membershipId: string,
) {
  return mutate(organizationId, userId, `/${membershipId}/revoke-organization-admin`, {});
}
