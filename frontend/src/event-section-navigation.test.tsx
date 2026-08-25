import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { EventSectionNavigation } from "./event-section-navigation";

describe("EventSectionNavigation", () => {
  it("links every peer section for the same event and marks the current section", () => {
    render(
      <EventSectionNavigation
        current="costs"
        eventId="event-id"
        organizationId="organization-id"
      />,
    );
    const links = screen.getAllByRole("link");
    expect(links).toHaveLength(5);
    for (const section of [
      "planner",
      "shopping",
      "costs",
      "receipts",
      "settings",
    ]) {
      expect(
        links.find((link) =>
          link.getAttribute("href")?.endsWith(`/${section}`),
        ),
      ).toBeTruthy();
    }
    expect(
      links.find((link) => link.getAttribute("href")?.endsWith("/costs")),
    ).toHaveAttribute("aria-current", "page");
  });
});
