import { Pool } from "pg";

import { applyMigrations } from "./migrations.js";
import { runtimeConfigurationFromEnvironment } from "./runtime.js";

const configuration = runtimeConfigurationFromEnvironment(process.env);
const pool = new Pool({ connectionString: configuration.databaseUrl, connectionTimeoutMillis: 5_000 });
try {
  const client = await pool.connect();
  try {
    await client.query("BEGIN");
    await client.query("SELECT pg_advisory_xact_lock(hashtext('cookops_oauth_schema_migrations'))");
    await applyMigrations(client);
    await client.query("COMMIT");
  } catch (error) {
    await client.query("ROLLBACK");
    throw error;
  } finally {
    client.release();
  }
} finally {
  await pool.end();
}
