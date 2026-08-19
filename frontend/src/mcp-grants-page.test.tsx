import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { McpGrantsPage } from "./mcp-grants-page";
import i18n from "./i18n";

describe("MCP grants page", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("lists opaque grants and removes a confirmed grant after revocation", async () => {
    const handle = "a".repeat(64);
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify([{ handle, clientId: "trusted-client" }])))
      .mockResolvedValueOnce(new Response(null, { status: 204 }));
    vi.stubGlobal("fetch", fetchMock);
    vi.stubGlobal("confirm", vi.fn(() => true));
    await i18n.changeLanguage("en");
    render(<McpGrantsPage onUnauthenticated={vi.fn()} />);

    expect(await screen.findByText("trusted-client")).toBeInTheDocument();
    expect(screen.queryByText(handle)).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Revoke" }));
    await waitFor(() => expect(screen.queryByText("trusted-client")).not.toBeInTheDocument());
    expect(fetchMock).toHaveBeenLastCalledWith(`/auth/mcp-grants/${handle}`, expect.objectContaining({ method: "DELETE" }));
  });

  it("reports list and revoke failures accessibly", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(null, { status: 503 })));
    await i18n.changeLanguage("en");
    render(<McpGrantsPage onUnauthenticated={vi.fn()} />);
    expect(await screen.findByRole("alert")).toHaveTextContent("Unable to load connections.");
  });

  it("returns to authentication when the grants request is unauthorized", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(null, { status: 401 })));
    const onUnauthenticated = vi.fn();
    render(<McpGrantsPage onUnauthenticated={onUnauthenticated} />);
    await waitFor(() => expect(onUnauthenticated).toHaveBeenCalledOnce());
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });
});
