import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it } from "vitest";
import { OrganizationMetadataSettings } from "./organization-metadata-settings";
import { localDb } from "./local-db";
import i18n, { defaultLocale } from "./i18n";

const userId = "11111111-1111-4111-8111-111111111111";
const organizationId = "22222222-2222-4222-8222-222222222222";

describe("OrganizationMetadataSettings", () => {
  beforeEach(async () => {
    await i18n.changeLanguage(defaultLocale);
    await Promise.all([
      localDb.canonicalRecords.clear(),
      localDb.optimisticOverlays.clear(),
      localDb.outbox.clear(),
      localDb.syncMetadata.clear(),
    ]);
    Object.defineProperty(navigator, "onLine", {
      configurable: true,
      value: true,
    });
    await localDb.canonicalRecords.put({
      userId,
      organizationId,
      entityType: "organization",
      entityId: organizationId,
      recordSchemaVersion: 1,
      lifecycle: "active",
      fields: { name: "Old", description: null, default_currency: "CZK" },
      fieldClocks: {},
      immutable: false,
      updatedAt: "2026-08-22T12:00:00.000Z",
    });
  });
  it("lets an ordinary member submit without system API", async () => {
    const user = userEvent.setup();
    render(
      <OrganizationMetadataSettings
        userId={userId}
        organizationId={organizationId}
      />,
    );
    const name = await screen.findByRole("textbox", { name: "Název" });
    await user.clear(name);
    await user.type(name, "New");
    await user.click(
      screen.getByRole("button", { name: "Zařadit změnu k synchronizaci" }),
    );
    expect(await localDb.outbox.toCollection().first()).toMatchObject({
      commandType: "organization.update",
    });
  });
  it("keeps the form writable offline under a valid authorization lease", async () => {
    const user = userEvent.setup();
    Object.defineProperty(navigator, "onLine", {
      configurable: true,
      value: false,
    });
    await localDb.syncMetadata.put({
      userId,
      organizationId,
      activity: "caughtUp",
      lastAuthorizedAt: new Date().toISOString(),
    });
    render(
      <OrganizationMetadataSettings
        userId={userId}
        organizationId={organizationId}
      />,
    );
    const name = await screen.findByRole("textbox", { name: "Název" });
    await waitFor(() => expect(name).not.toBeDisabled());
    await user.clear(name);
    await user.type(name, "Offline");
    await user.click(
      screen.getByRole("button", { name: "Zařadit změnu k synchronizaci" }),
    );
    expect(await localDb.outbox.toCollection().first()).toMatchObject({
      commandType: "organization.update",
    });
  });
});
