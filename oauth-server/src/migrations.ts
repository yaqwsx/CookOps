import { initializeInteractionApprovals } from "./interaction-approvals.js";
import { initializePostgresAdapter, type PgQueryable } from "./postgres-adapter.js";

interface Migration {
  version: number;
  apply(database: PgQueryable): Promise<void>;
}

const migrations: readonly Migration[] = [
  {
    version: 1,
    async apply(database) {
      await initializePostgresAdapter(database);
      await initializeInteractionApprovals(database);
    },
  },
];

export const OAUTH_SCHEMA_VERSION = migrations.at(-1)?.version ?? 0;
export const OAUTH_SCHEMA_VERSIONS = migrations.map(({ version }) => version);

export async function applyMigrations(database: PgQueryable): Promise<void> {
  await database.query(`
    CREATE TABLE IF NOT EXISTS oauth_schema_migrations (
      version integer PRIMARY KEY,
      applied_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP
    )
  `);
  const applied = await database.query<{ version: number }>(
    "SELECT version FROM oauth_schema_migrations",
  );
  const versions = new Set(applied.rows.map(({ version }) => version));
  for (const migration of migrations) {
    if (versions.has(migration.version)) continue;
    await migration.apply(database);
    await database.query("INSERT INTO oauth_schema_migrations (version) VALUES ($1)", [
      migration.version,
    ]);
  }
}
