import { describe, expect, it, vi } from "vitest";

import { getMemberships } from "./memberships";

function response(body: object, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

describe("getMemberships", () => {
  it("accepts only typed member records from the online-only endpoint", async () => {
    const fetchMock = vi.fn(async () =>
      response({
        memberships: [
          {
            id: "5ce17d2f-8365-4b1f-a80b-34d10425d51c",
            invited_email: "cook@example.test",
            role: "member",
            state: "invited",
          },
        ],
      }),
    );
    vi.stubGlobal("fetch", fetchMock);

    await expect(
      getMemberships("8c4c9065-0fb3-490f-8c31-bef5103c1b1b"),
    ).resolves.toEqual([
      {
        id: "5ce17d2f-8365-4b1f-a80b-34d10425d51c",
        invitedEmail: "cook@example.test",
        role: "member",
        state: "invited",
      },
    ]);
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/v1/organizations/8c4c9065-0fb3-490f-8c31-bef5103c1b1b/members",
      { credentials: "same-origin" },
    );
  });

  it("rejects untrusted response shapes", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => response({ memberships: [{ id: "not-a-uuid" }] })),
    );
    await expect(
      getMemberships("8c4c9065-0fb3-490f-8c31-bef5103c1b1b"),
    ).rejects.toThrow("Invalid membership response.");
  });
});
