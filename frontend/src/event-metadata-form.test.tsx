import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import i18n, { defaultLocale } from "./i18n";
import { EventMetadata } from "./event-metadata-form";

const { queueEventMetadataUpdate } = vi.hoisted(() => ({
  queueEventMetadataUpdate: vi.fn(),
}));
vi.mock("./event-metadata", () => ({ queueEventMetadataUpdate }));

describe("EventMetadata", () => {
  it("submits labelled native controls", async () => {
    await i18n.changeLanguage(defaultLocale);
    queueEventMetadataUpdate.mockResolvedValue(undefined);
    const user = userEvent.setup();
    render(
      <EventMetadata
        eventId="3d8b2b21-c378-4574-9e46-9338c81305ef"
        organizationId="5ce17d2f-8365-4b1f-a80b-34d10425d51c"
        userId="a6a58bd6-214e-49af-8fae-e5f974bf8e08"
        name="Summer"
        startDate="2026-07-01"
        endDate="2026-07-02"
        location="Prague"
        budgetAmount="10"
        generalNote=""
      />,
    );
    await user.clear(screen.getByLabelText("Název akce"));
    await user.type(screen.getByLabelText("Název akce"), "Summer 2");
    await user.click(screen.getByRole("button", { name: "Uložit údaje akce" }));
    expect(queueEventMetadataUpdate).toHaveBeenCalledWith(
      expect.any(String),
      expect.any(String),
      expect.objectContaining({
        eventId: expect.any(String),
        name: "Summer 2",
        startDate: "2026-07-01",
        endDate: "2026-07-02",
        location: "Prague",
        budgetAmount: "10",
        generalNote: "",
      }),
    );
  });
});
