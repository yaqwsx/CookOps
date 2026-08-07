import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { EventCreate } from "./event-create-form";
import i18n, { defaultLocale } from "./i18n";

const { queueEventCreate } = vi.hoisted(() => ({
  queueEventCreate: vi.fn(),
}));
vi.mock("./event-create", () => ({ queueEventCreate }));

describe("EventCreate", () => {
  it("does not persist a second event while the first local transaction is pending", async () => {
    await i18n.changeLanguage(defaultLocale);
    let resolve: (() => void) | undefined;
    queueEventCreate.mockReturnValueOnce(
      new Promise<void>((next) => (resolve = next)),
    );
    const user = userEvent.setup();
    render(<EventCreate organizationId="organization-a" userId="user-a" />);

    await user.type(screen.getByLabelText("Název"), "One event");
    await user.type(screen.getByLabelText("Začátek"), "2026-08-10");
    await user.type(screen.getByLabelText("Konec"), "2026-08-10");
    const submit = screen.getByRole("button", { name: "Uložit akci" });
    await user.click(submit);
    await user.click(submit);

    expect(queueEventCreate).toHaveBeenCalledOnce();
    expect(submit).toBeDisabled();
    resolve?.();
  });
});
