import { describe, expect, it, vi } from "vitest";

import { editSystemOrganization } from "./system-organizations";

describe("editSystemOrganization", () => {
  it("sends an online PATCH with the canonical edit payload", async () => {
    const response = {
      id: "5ce17d2f-8365-4b1f-a80b-34d10425d51c",
      name: "Edited kitchen",
      description: "Updated",
      default_currency: "EUR",
      retired_at: null,
      retired_by_user_id: null,
    };
    const fetchMock = vi.fn<typeof fetch>(
      async (_input, _init) =>
        new Response(JSON.stringify(response), { status: 200 }),
    );
    vi.stubGlobal("fetch", fetchMock);
    vi.stubGlobal("crypto", { randomUUID: () => "mutation-id" });

    await expect(
      editSystemOrganization("user-id", response.id, {
        name: response.name,
        description: response.description,
        defaultCurrency: response.default_currency,
      }),
    ).resolves.toEqual(response);
    expect(fetchMock).toHaveBeenCalledWith(
      `/api/v1/system/organizations/${response.id}`,
      expect.objectContaining({
        method: "PATCH",
        credentials: "same-origin",
      }),
    );
    const body = fetchMock.mock.calls[0][1]?.body;
    expect(typeof body).toBe("string");
    if (typeof body !== "string")
      throw new Error("Expected a JSON request body");
    expect(JSON.parse(body)).toMatchObject({
      mutation_id: "mutation-id",
      name: "Edited kitchen",
      default_currency: "EUR",
    });
  });
});
