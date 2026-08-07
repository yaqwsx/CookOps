export type AvailableOrganization = {
  id: string;
  name: string;
};

export class OrganizationRequestError extends Error {
  constructor(readonly status: number) {
    super("Organization request failed.");
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

function isUuid(value: unknown): value is string {
  return (
    typeof value === "string" &&
    /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(
      value,
    )
  );
}

function parseOrganizations(value: unknown): AvailableOrganization[] {
  if (!isRecord(value) || !Array.isArray(value.organizations)) {
    throw new Error("Invalid organization response.");
  }
  const organizations = value.organizations.map((organization) => {
    if (
      !isRecord(organization) ||
      !isUuid(organization.id) ||
      typeof organization.name !== "string" ||
      !organization.name.trim() ||
      organization.name.length > 200
    ) {
      throw new Error("Invalid organization response.");
    }
    return { id: organization.id, name: organization.name };
  });
  if (
    new Set(organizations.map(({ id }) => id)).size !== organizations.length
  ) {
    throw new Error("Invalid organization response.");
  }
  return organizations;
}

export async function getAvailableOrganizations(): Promise<
  AvailableOrganization[]
> {
  const response = await fetch("/api/v1/organizations", {
    credentials: "same-origin",
  });
  if (!response.ok) throw new OrganizationRequestError(response.status);
  return parseOrganizations(await response.json());
}
