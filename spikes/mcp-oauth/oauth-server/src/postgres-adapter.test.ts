import assert from "node:assert/strict";
import { randomUUID } from "node:crypto";
import test from "node:test";

import { Pool, type QueryResult, type QueryResultRow } from "pg";

import {
  createPostgresAdapter,
  deleteExpiredOidcRecords,
  initializePostgresAdapter,
  type PgClient,
  type PgPool as AdapterPgPool,
} from "./postgres-adapter.js";

const TEST_SECRET = Buffer.alloc(32, 0x5a);

function result<Row extends QueryResultRow>(rows: Row[]): QueryResult<Row> {
  return { command: "", rowCount: rows.length, oid: 0, fields: [], rows };
}

class RecordingDatabase implements AdapterPgPool, PgClient {
  readonly queries: Array<{ text: string; values: unknown[] }> = [];

  async query<Row extends QueryResultRow = QueryResultRow>(
    text: string,
    values: unknown[] = [],
  ): Promise<QueryResult<Row>> {
    this.queries.push({ text, values });
    return result([]) as QueryResult<Row>;
  }

  async connect(): Promise<PgClient> {
    return this;
  }

  release(): void {}
}

test("adapter fails closed on weak secrets and HMACs lookup values", async () => {
  const database = new RecordingDatabase();
  assert.throws(
    () => createPostgresAdapter(database, { secret: Buffer.alloc(31) }),
    /at least 32 bytes/,
  );
  assert.throws(
    () =>
      createPostgresAdapter(database, {
        secret: "not-bytes" as unknown as Uint8Array,
      }),
    /at least 32 bytes/,
  );

  const factory = createPostgresAdapter(database, {
    secret: TEST_SECRET,
    clockToleranceSeconds: 15,
  });
  await factory("AccessToken").upsert(
    "raw-primary-id",
    {
      jti: "raw-primary-id",
      grantId: "raw-grant-id",
      userCode: "raw-user-code",
    },
    60,
  );
  const values =
    database.queries.find(({ text }) => text.includes("oidc:upsert"))?.values ??
    [];
  assert.equal(values[3], 75);
  for (const raw of ["raw-primary-id", "raw-grant-id", "raw-user-code"]) {
    assert.equal(values.includes(raw), false);
  }
  const digests = [values[1], values[4], values[5]];
  for (const digest of digests) {
    assert.match(String(digest), /^[0-9a-f]{64}$/);
  }
  assert.equal(new Set(digests).size, digests.length);
  assert.equal(String(values[2]).includes("raw-primary-id"), false);
  assert.equal(String(values[2]).includes("raw-grant-id"), false);
  assert.equal(String(values[2]).includes("raw-user-code"), false);

  await factory("AccessToken").find("raw-primary-id");
  assert.equal(database.queries.at(-1)?.values[1], values[1]);
});

const integrationDatabaseUrl = process.env["TEST_DATABASE_URL"];

test(
  "adapter contract holds against PostgreSQL",
  { skip: integrationDatabaseUrl ? false : "TEST_DATABASE_URL is not configured" },
  async () => {
    assert.ok(integrationDatabaseUrl);
    const schema = `oidc_adapter_${randomUUID().replaceAll("-", "")}`;
    const administrator = new Pool({ connectionString: integrationDatabaseUrl });
    await administrator.query(`CREATE SCHEMA ${schema}`);
    const poolOptions = {
      connectionString: integrationDatabaseUrl,
      options: `-c search_path=${schema}`,
    };
    let database = new Pool(poolOptions);

    try {
      await initializePostgresAdapter(database);
      let factory = createPostgresAdapter(database, {
        secret: TEST_SECRET,
        clockToleranceSeconds: 2,
      });
      const codes = factory("AuthorizationCode");

      await codes.upsert(
        "expired-code",
        { userCode: "SAME-CODE", extra: { nested: true } },
        -3,
      );
      await codes.upsert(
        "current-code",
        {
          jti: "current-code",
          userCode: "SAME-CODE",
          extra: { nested: true },
        },
        30,
      );
      assert.deepEqual(await codes.findByUserCode("SAME-CODE"), {
        jti: "current-code",
        userCode: "SAME-CODE",
        extra: { nested: true },
      });

      await codes.upsert("replacement-code", { userCode: "SAME-CODE" }, 30);
      await codes.destroy("replacement-code");
      assert.equal(await codes.findByUserCode("SAME-CODE"), undefined);

      await codes.upsert("concurrent-code", {}, 30);
      const consumeResults = await Promise.allSettled([
        codes.consume("concurrent-code"),
        codes.consume("concurrent-code"),
      ]);
      assert.equal(
        consumeResults.filter(({ status }) => status === "fulfilled").length,
        1,
      );

      const sessions = factory("Session");
      await sessions.upsert(
        "persistent-session",
        { uid: "persistent-uid", accountId: "user-1" },
        30,
      );
      await database.end();
      database = new Pool(poolOptions);
      factory = createPostgresAdapter(database, {
        secret: TEST_SECRET,
        clockToleranceSeconds: 2,
      });
      assert.deepEqual(await factory("Session").findByUid("persistent-uid"), {
        uid: "persistent-uid",
        accountId: "user-1",
      });

      const tokens = factory("AccessToken");
      await database.query(`
        CREATE FUNCTION delay_grant_revocation() RETURNS trigger AS $$
        BEGIN
          PERFORM pg_sleep(0.3);
          RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        CREATE TRIGGER delay_grant_revocation
          BEFORE INSERT ON oidc_provider_revoked_grants
          FOR EACH ROW EXECUTE FUNCTION delay_grant_revocation();
      `);
      await tokens.upsert(
        "existing-access",
        { grantId: "grant-1" },
        30,
      );
      const grantRow = await database.query<{ grant_id: string }>(
        "SELECT grant_id FROM oidc_provider_records WHERE grant_id IS NOT NULL LIMIT 1",
      );
      const grantLookup = grantRow.rows[0]?.grant_id;
      assert.ok(grantLookup);

      const lockObserver = await database.connect();
      const revokePromise = tokens.revokeByGrantId("grant-1");
      let revocationHoldsLock = false;
      try {
        for (let attempt = 0; attempt < 100; attempt += 1) {
          const lockResult = await lockObserver.query<{ acquired: boolean }>(
            "SELECT pg_try_advisory_lock(hashtextextended($1, 0)) AS acquired",
            [grantLookup],
          );
          if (!lockResult.rows[0]?.acquired) {
            revocationHoldsLock = true;
            break;
          }
          await lockObserver.query(
            "SELECT pg_advisory_unlock(hashtextextended($1, 0))",
            [grantLookup],
          );
          await new Promise((resolve) => setTimeout(resolve, 5));
        }
      } finally {
        lockObserver.release();
      }
      assert.equal(revocationHoldsLock, true);
      const upsertPromise = tokens.upsert(
        "racing-access",
        { grantId: "grant-1" },
        30,
      );
      const [revokeResult, upsertResult] = await Promise.allSettled([
        revokePromise,
        upsertPromise,
      ]);
      assert.equal(revokeResult.status, "fulfilled");
      assert.equal(upsertResult.status, "rejected");
      assert.match(
        String(upsertResult.status === "rejected" && upsertResult.reason),
        /revoked grant/,
      );
      assert.equal(await tokens.find("racing-access"), undefined);
      await assert.rejects(
        tokens.upsert("late-access", { grantId: "grant-1" }, 30),
        /cannot persist member of a revoked grant/,
      );

      const records = await database.query<{
        id: string;
        grant_id: string | null;
        payload: string;
      }>("SELECT id, grant_id, payload::text AS payload FROM oidc_provider_records");
      const lookups = await database.query<{ id: string; lookup: string }>(
        "SELECT id, lookup FROM oidc_provider_lookups",
      );
      const revoked = await database.query<{ grant_id: string }>(
        "SELECT grant_id FROM oidc_provider_revoked_grants",
      );
      const lookupValues = [
        ...records.rows.flatMap((row) => [row.id, row.grant_id]),
        ...lookups.rows.flatMap((row) => [row.id, row.lookup]),
        ...revoked.rows.map(({ grant_id }) => grant_id),
      ].filter((value): value is string => value !== null);
      for (const raw of [
        "current-code",
        "SAME-CODE",
        "concurrent-code",
        "persistent-session",
        "persistent-uid",
        "racing-access",
        "grant-1",
      ]) {
        assert.equal(lookupValues.includes(raw), false);
      }
      assert.equal(
        lookupValues.every((value) => /^[0-9a-f]{64}$/.test(value)),
        true,
      );
      const storedPayloads = records.rows.map(({ payload }) => payload).join("\n");
      for (const raw of [
        "SAME-CODE",
        "current-code",
        "persistent-uid",
        "persistent-session",
        "grant-1",
      ]) {
        assert.equal(storedPayloads.includes(raw), false);
      }

      const restartedCodes = factory("AuthorizationCode");
      await restartedCodes.upsert("cleanup-code", {}, -3);
      assert.equal(await deleteExpiredOidcRecords(database), 2);
      await factory("Session").destroy("persistent-session");
      assert.equal(await factory("Session").find("persistent-session"), undefined);
    } finally {
      await database.end();
      await administrator.query(`DROP SCHEMA ${schema} CASCADE`);
      await administrator.end();
    }
  },
);
