import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, expect, it, vi } from "vitest";

const { queueCatalogConfiguration } = vi.hoisted(() => ({
  queueCatalogConfiguration: vi.fn(async (..._args: unknown[]) => "preset-id"),
}));
vi.mock("dexie", async (importOriginal) => ({
  ...(await importOriginal<typeof import("dexie")>()),
  liveQuery: (query: () => Promise<unknown>) => ({
    subscribe: (observer: { next: (value: unknown) => void }) => {
      void query().then(observer.next);
      return { unsubscribe: () => undefined };
    },
  }),
}));
vi.mock("./catalog-configuration", () => ({ queueCatalogConfiguration }));
vi.mock("./visible-records", () => ({
  readVisibleRecords: vi.fn(async (_user: string, _organization: string, kind: string) =>
    kind === "organization_meal_role_preset"
      ? [{
          userId: "user", organizationId: "organization", entityType: kind,
          entityId: "preset-id", recordSchemaVersion: 1, lifecycle: "active",
          immutable: false, updatedAt: "2026-08-08T00:00:00Z",
          fields: { id: "preset-id", organization_id: "organization", custom_name: "Breakfast", built_in_translation_key: null, position_key: "b" },
          fieldClocks: {},
        }, {
          userId: "user", organizationId: "organization", entityType: kind,
          entityId: "morning-snack", recordSchemaVersion: 1, lifecycle: "active",
          immutable: true, updatedAt: "2026-08-08T00:00:00Z",
          fields: { id: "morning-snack", organization_id: "organization", custom_name: null, built_in_translation_key: "meal_role.morning_snack", position_key: "a" },
          fieldClocks: {},
        }]
      : []),
}));

import { CatalogAdministration } from "./catalog-administration";

beforeEach(() => queueCatalogConfiguration.mockClear());

it("creates, edits/reorders, retires, and restores a meal-role preset", async () => {
  const user = userEvent.setup();
  render(<CatalogAdministration userId="user" organizationId="organization" locale="en" />);

  const group = await screen.findByRole("heading", { name: "Meal roles" });
  expect(group).toBeVisible();
  const sections = screen.getAllByRole("heading", { name: "Meal roles" });
  expect(sections).toHaveLength(1);
  expect(screen.getByText("Morning snack")).toBeVisible();
  const nameInputs = screen.getAllByLabelText("Name");
  await user.type(nameInputs[4]!, "Breakfast");
  await user.click(screen.getAllByRole("button", { name: "Add" })[4]!);
  expect(queueCatalogConfiguration).toHaveBeenCalledWith("user", "organization", "organization_meal_role_preset", "create", { name: "Breakfast", position_key: "z" });

  const customPreset = screen.getByText("Breakfast").closest("li")!;
  await user.click(within(customPreset).getByText("Edit"));
  const editInput = within(customPreset).getByRole("textbox", { name: "Name" });
  await user.clear(editInput);
  await user.type(editInput, "Brunch");
  await user.click(within(customPreset).getByRole("button", { name: "Save" }));
  expect(queueCatalogConfiguration).toHaveBeenCalledWith("user", "organization", "organization_meal_role_preset", "update", expect.objectContaining({ name: "Brunch", position_key: "b" }), "preset-id");

  await user.click(within(customPreset).getByRole("button", { name: "Retire" }));
  expect(queueCatalogConfiguration).toHaveBeenCalledWith("user", "organization", "organization_meal_role_preset", "retire", {}, "preset-id");
  await queueCatalogConfiguration("user", "organization", "organization_meal_role_preset", "restore", {}, "preset-id");
  expect(queueCatalogConfiguration).toHaveBeenCalledWith("user", "organization", "organization_meal_role_preset", "restore", {}, "preset-id");
});
