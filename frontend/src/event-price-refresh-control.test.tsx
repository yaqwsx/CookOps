import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import "./i18n";
import { EventPriceRefreshControl } from "./event-price-refresh-control";

const { queueEventPriceRefresh } = vi.hoisted(() => ({
  queueEventPriceRefresh: vi.fn(),
}));
vi.mock("./event-price-refresh", () => ({
  eventPriceRefreshPending: vi.fn().mockResolvedValue(false),
  queueEventPriceRefresh,
}));

describe("EventPriceRefreshControl", () => {
  beforeEach(() => queueEventPriceRefresh.mockReset());

  it("queues the current event", async () => {
    queueEventPriceRefresh.mockResolvedValue(true);
    const user = userEvent.setup();
    render(
      <EventPriceRefreshControl
        eventId="6ce17d2f-8365-4b1f-a80b-34d10425d51c"
        organizationId="5ce17d2f-8365-4b1f-a80b-34d10425d51c"
        userId="a6a58bd6-214e-49af-8fae-e5f974bf8e08"
      />,
    );
    await user.click(
      screen.getByRole("button", { name: "Aktualizovat odhady cen" }),
    );
    expect(queueEventPriceRefresh).toHaveBeenCalledOnce();
  });
});
