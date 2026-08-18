export class UpgradeRequiredError extends Error {
  constructor(
    readonly reason:
      | "sync_schema_version"
      | "record_schema_version"
      | "entity_kind",
    readonly value: unknown,
  ) {
    super(`Synchronization upgrade required: unsupported ${reason}.`);
    this.name = "UpgradeRequiredError";
  }
}

export function isUpgradeRequiredError(
  error: unknown,
): error is UpgradeRequiredError {
  return error instanceof UpgradeRequiredError;
}
