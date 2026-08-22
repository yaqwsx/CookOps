import { describe, expect, it, vi } from "vitest";

import { getAvailableOrganizations } from "./organizations";

function response(body: object, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

describe("getAvailableOrganizations", () => {
  it("accepts only unique named UUID organizations from the trusted response", async () => {
    const fetchMock = vi.fn(async (_input: RequestInfo | URL) =>
      response({
        organizations: [
          { id: "5ce17d2f-8365-4b1f-a80b-34d10425d51c", name: "Kitchen" },
        ],
      }),
    );
    vi.stubGlobal("fetch", fetchMock);

    await expect(getAvailableOrganizations()).resolves.toEqual([
      { id: "5ce17d2f-8365-4b1f-a80b-34d10425d51c", name: "Kitchen" },
    ]);
    const request = fetchMock.mock.calls[0]?.[0];
    expect(request).toBeInstanceOf(Request);
    const requestUrl = new URL((request as Request).url);
    expect(requestUrl.pathname + requestUrl.search).toBe(
      "/api/v1/organizations",
    );
    expect((request as Request).method).toBe("GET");
    expect((request as Request).credentials).toBe("same-origin");
  });

  it.each([
    {},
    { organizations: [{ id: "not-a-uuid", name: "Kitchen" }] },
    {
      organizations: [
        { id: "5ce17d2f-8365-4b1f-a80b-34d10425d51c", name: " " },
      ],
    },
    {
      organizations: [
        { id: "5ce17d2f-8365-4b1f-a80b-34d10425d51c", name: "Kitchen" },
        { id: "5ce17d2f-8365-4b1f-a80b-34d10425d51c", name: "Duplicate" },
      ],
    },
  ])("rejects malformed organization responses", async (payload) => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => response(payload)),
    );
    await expect(getAvailableOrganizations()).rejects.toThrow(
      "Invalid organization response.",
    );
  });
});
