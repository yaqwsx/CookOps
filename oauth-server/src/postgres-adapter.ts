import {
  createCipheriv,
  createDecipheriv,
  createHmac,
  randomBytes,
} from "node:crypto";

import type {
  Adapter,
  AdapterFactory,
  AdapterPayload,
} from "oidc-provider";
import type { QueryResult, QueryResultRow } from "pg";

const TABLE_NAME = "oidc_provider_records";

// Keep this aligned with oidc-provider's built-in adapter semantics. Only these
// model families participate in grant-wide revocation.
const GRANTABLE_MODELS = new Set([
  "AccessToken",
  "AuthorizationCode",
  "RefreshToken",
  "DeviceCode",
  "BackchannelAuthenticationRequest",
  "PreAuthorizedCode",
]);

export interface PgQueryable {
  query<Row extends QueryResultRow = QueryResultRow>(
    text: string,
    values?: unknown[],
  ): Promise<QueryResult<Row>>;
}

export interface PgClient extends PgQueryable {
  release(): void;
}

export interface PgPool extends PgQueryable {
  connect(): Promise<PgClient>;
}

export interface PostgresAdapterOptions {
  /** A deployment secret containing at least 256 bits (32 bytes). */
  secret: Uint8Array;
  clockToleranceSeconds?: number;
}

interface NormalizedPostgresAdapterOptions {
  secret: Uint8Array;
  clockToleranceSeconds: number;
}

interface PayloadRow extends QueryResultRow {
  id: string;
  payload: EncryptedPayload;
  consumed_at: string | number | null;
}

interface EncryptedPayload {
  v: 1;
  iv: string;
  ciphertext: string;
  tag: string;
}

interface DeletedCountRow extends QueryResultRow {
  deleted: number;
}

function optionalString(value: unknown): string | null {
  return typeof value === "string" ? value : null;
}

function validateOptions(
  options: PostgresAdapterOptions,
): NormalizedPostgresAdapterOptions {
  if (typeof options !== "object" || options === null) {
    throw new TypeError("adapter options are required");
  }
  if (!(options.secret instanceof Uint8Array) || options.secret.byteLength < 32) {
    throw new TypeError("adapter secret must contain at least 32 bytes");
  }
  const clockToleranceSeconds = options.clockToleranceSeconds ?? 0;
  if (!Number.isFinite(clockToleranceSeconds) || clockToleranceSeconds < 0) {
    throw new TypeError("clock tolerance must be a non-negative finite number");
  }
  return {
    secret: Uint8Array.from(options.secret),
    clockToleranceSeconds,
  };
}

function lookupDigest(
  secret: Uint8Array,
  domain: "grant" | "id" | "uid" | "user-code",
  model: string,
  value: string,
): string {
  return createHmac("sha256", secret)
    .update("cookops:oidc-adapter:v1\0", "utf8")
    .update(domain, "utf8")
    .update("\0", "utf8")
    .update(model, "utf8")
    .update("\0", "utf8")
    .update(value, "utf8")
    .digest("hex");
}

function payloadKey(secret: Uint8Array): Buffer {
  return createHmac("sha256", secret)
    .update("cookops:oidc-adapter:payload-key:v1", "utf8")
    .digest();
}

function encryptPayload(
  key: Buffer,
  model: string,
  id: string,
  payload: AdapterPayload,
): EncryptedPayload {
  const iv = randomBytes(12);
  const cipher = createCipheriv("aes-256-gcm", key, iv);
  cipher.setAAD(Buffer.from(`${model}\0${id}`, "utf8"));
  const ciphertext = Buffer.concat([
    cipher.update(JSON.stringify(payload), "utf8"),
    cipher.final(),
  ]);
  return {
    v: 1,
    iv: iv.toString("base64url"),
    ciphertext: ciphertext.toString("base64url"),
    tag: cipher.getAuthTag().toString("base64url"),
  };
}

function decryptPayload(
  key: Buffer,
  model: string,
  id: string,
  envelope: EncryptedPayload,
): AdapterPayload {
  if (
    envelope.v !== 1 ||
    typeof envelope.iv !== "string" ||
    typeof envelope.ciphertext !== "string" ||
    typeof envelope.tag !== "string"
  ) {
    throw new TypeError("invalid encrypted adapter payload");
  }
  const decipher = createDecipheriv(
    "aes-256-gcm",
    key,
    Buffer.from(envelope.iv, "base64url"),
  );
  decipher.setAAD(Buffer.from(`${model}\0${id}`, "utf8"));
  decipher.setAuthTag(Buffer.from(envelope.tag, "base64url"));
  const plaintext = Buffer.concat([
    decipher.update(Buffer.from(envelope.ciphertext, "base64url")),
    decipher.final(),
  ]).toString("utf8");
  return JSON.parse(plaintext) as AdapterPayload;
}

/**
 * Creates the deliberately small persistence surface needed by oidc-provider.
 * A production migration can run the same statements before the service starts;
 * this initializer keeps the disposable spike self-contained and idempotent.
 */
export async function initializePostgresAdapter(
  database: PgQueryable,
): Promise<void> {
  await database.query(`
    /* oidc:init-table */
    CREATE TABLE IF NOT EXISTS ${TABLE_NAME} (
      model text NOT NULL,
      id text NOT NULL,
      payload jsonb NOT NULL,
      consumed_at bigint,
      expires_at timestamptz,
      grant_id text,
      user_code text,
      uid text,
      PRIMARY KEY (model, id)
    );

    CREATE TABLE IF NOT EXISTS oidc_provider_lookups (
      model text NOT NULL,
      kind text NOT NULL CHECK (kind IN ('user_code', 'uid')),
      lookup text NOT NULL,
      id text NOT NULL,
      PRIMARY KEY (model, kind, lookup),
      FOREIGN KEY (model, id)
        REFERENCES ${TABLE_NAME} (model, id)
        ON DELETE CASCADE
    );

    /* oidc:init-revoked-grants-table */
    CREATE TABLE IF NOT EXISTS oidc_provider_revoked_grants (
      grant_id text PRIMARY KEY
    );

    /* oidc:init-grant-index */
    CREATE INDEX IF NOT EXISTS oidc_provider_records_grant_id_idx
      ON ${TABLE_NAME} (grant_id)
      WHERE grant_id IS NOT NULL;

    /* oidc:init-expiry-index */
    CREATE INDEX IF NOT EXISTS oidc_provider_records_expires_at_idx
      ON ${TABLE_NAME} (expires_at)
      WHERE expires_at IS NOT NULL;
  `);
}

/**
 * Deletes a bounded batch of expired records. Reads exclude expired rows, so
 * correctness does not depend on scheduling this storage reclamation.
 */
export async function deleteExpiredOidcRecords(
  database: PgQueryable,
  limit = 1_000,
): Promise<number> {
  if (!Number.isSafeInteger(limit) || limit <= 0) {
    throw new RangeError("expiry cleanup limit must be a positive safe integer");
  }

  const result = await database.query<DeletedCountRow>(
    `
      /* oidc:cleanup-expired */
      WITH doomed AS (
        SELECT model, id
        FROM ${TABLE_NAME}
        WHERE expires_at IS NOT NULL
          AND expires_at <= CURRENT_TIMESTAMP
        ORDER BY expires_at, model, id
        LIMIT $1
        FOR UPDATE SKIP LOCKED
      ), deleted AS (
        DELETE FROM ${TABLE_NAME} AS records
        USING doomed
        WHERE records.model = doomed.model
          AND records.id = doomed.id
        RETURNING 1
      )
      SELECT count(*)::integer AS deleted FROM deleted
    `,
    [limit],
  );

  return result.rows[0]?.deleted ?? 0;
}

class PostgresAdapter implements Adapter {
  readonly #model: string;
  readonly #database: PgPool;
  readonly #clockToleranceSeconds: number;
  readonly #secret: Uint8Array;
  readonly #payloadKey: Buffer;

  constructor(
    model: string,
    database: PgPool,
    options: NormalizedPostgresAdapterOptions,
  ) {
    this.#model = model;
    this.#database = database;
    this.#clockToleranceSeconds = options.clockToleranceSeconds;
    this.#secret = options.secret;
    this.#payloadKey = payloadKey(options.secret);
  }

  async upsert(
    id: string,
    payload: AdapterPayload,
    expiresIn?: number,
  ): Promise<void> {
    const grantId = GRANTABLE_MODELS.has(this.#model)
      ? optionalString(payload.grantId)
      : null;
    const grantLookup =
      grantId === null ? null : this.#digest("grant", "", grantId);
    const primaryLookup = this.#digest("id", this.#model, id);
    const values = [
      this.#model,
      primaryLookup,
      JSON.stringify(
        encryptPayload(this.#payloadKey, this.#model, primaryLookup, payload),
      ),
      expiresIn === undefined
        ? null
        : expiresIn + this.#clockToleranceSeconds,
      grantLookup,
      this.#optionalDigest("user-code", this.#model, payload.userCode),
      this.#model === "Session"
        ? this.#optionalDigest("uid", this.#model, payload.uid)
        : null,
    ];

    if (grantLookup === null) {
      await this.#persist(this.#database, values);
      return;
    }

    const client = await this.#database.connect();
    try {
      await client.query("BEGIN");
      await client.query(
        "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
        [grantLookup],
      );
      const revoked = await client.query(
        "SELECT 1 FROM oidc_provider_revoked_grants WHERE grant_id = $1",
        [grantLookup],
      );
      if (revoked.rowCount !== 0) {
        throw new Error("cannot persist member of a revoked grant");
      }
      await this.#persist(client, values);
      await client.query("COMMIT");
    } catch (error) {
      await client.query("ROLLBACK").catch(() => undefined);
      throw error;
    } finally {
      client.release();
    }
  }

  async #persist(database: PgQueryable, values: unknown[]): Promise<void> {
    await database.query(
      `
        /* oidc:upsert */
        WITH saved AS (
          INSERT INTO ${TABLE_NAME} (
            model, id, payload, expires_at, grant_id, user_code, uid
          ) SELECT
            $1,
            $2,
            $3::jsonb,
            CASE
              WHEN $4::double precision IS NULL THEN NULL
              ELSE clock_timestamp() + make_interval(secs => $4::double precision)
            END,
            $5,
            $6,
            $7
          ON CONFLICT (model, id) DO UPDATE SET
            payload = EXCLUDED.payload,
            consumed_at = NULL,
            expires_at = EXCLUDED.expires_at,
            grant_id = EXCLUDED.grant_id,
            user_code = EXCLUDED.user_code,
            uid = EXCLUDED.uid
          RETURNING model, id
        ), user_lookup AS (
          INSERT INTO oidc_provider_lookups (model, kind, lookup, id)
          SELECT $1, 'user_code', $6, $2 FROM saved WHERE $6 IS NOT NULL
          ON CONFLICT (model, kind, lookup) DO UPDATE SET id = EXCLUDED.id
          RETURNING 1
        ), uid_lookup AS (
          INSERT INTO oidc_provider_lookups (model, kind, lookup, id)
          SELECT $1, 'uid', $7, $2 FROM saved WHERE $7 IS NOT NULL
          ON CONFLICT (model, kind, lookup) DO UPDATE SET id = EXCLUDED.id
          RETURNING 1
        )
        SELECT count(*) FROM saved
      `,
      values,
    );
  }

  async find(id: string): Promise<AdapterPayload | undefined> {
    return this.#findBy(
      "id",
      this.#digest("id", this.#model, id),
    );
  }

  async findByUserCode(
    userCode: string,
  ): Promise<AdapterPayload | undefined> {
    return this.#findBy(
      "user_code",
      this.#digest("user-code", this.#model, userCode),
    );
  }

  async findByUid(uid: string): Promise<AdapterPayload | undefined> {
    return this.#findBy(
      "uid",
      this.#digest("uid", this.#model, uid),
    );
  }

  async consume(id: string): Promise<void> {
    const result = await this.#database.query(
      `
        /* oidc:consume */
        UPDATE ${TABLE_NAME}
        SET consumed_at = floor(extract(epoch FROM clock_timestamp()))::bigint
        WHERE model = $1
          AND id = $2
          AND (expires_at IS NULL OR expires_at > CURRENT_TIMESTAMP)
          AND consumed_at IS NULL
        RETURNING id
      `,
      [this.#model, this.#digest("id", this.#model, id)],
    );
    if (result.rowCount !== 1) {
      throw new Error(`cannot consume missing or expired ${this.#model} record`);
    }
  }

  async destroy(id: string): Promise<void> {
    await this.#database.query(
      `
        /* oidc:destroy */
        DELETE FROM ${TABLE_NAME}
        WHERE model = $1 AND id = $2
      `,
      [this.#model, this.#digest("id", this.#model, id)],
    );
  }

  async revokeByGrantId(grantId: string): Promise<void> {
    const grantLookup = this.#digest("grant", "", grantId);
    const client = await this.#database.connect();
    try {
      await client.query("BEGIN");
      await client.query(
        "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
        [grantLookup],
      );
      await client.query(
        `
          /* oidc:revoke-grant */
          INSERT INTO oidc_provider_revoked_grants (grant_id)
          VALUES ($1)
          ON CONFLICT (grant_id) DO NOTHING
        `,
        [grantLookup],
      );
      await client.query(
        `DELETE FROM ${TABLE_NAME} WHERE grant_id = $1`,
        [grantLookup],
      );
      await client.query("COMMIT");
    } catch (error) {
      await client.query("ROLLBACK").catch(() => undefined);
      throw error;
    } finally {
      client.release();
    }
  }

  async #findBy(
    field: "id" | "user_code" | "uid",
    value: string,
  ): Promise<AdapterPayload | undefined> {
    const lookupJoin =
      field === "id"
        ? ""
        : `JOIN oidc_provider_lookups AS lookup
             ON lookup.model = records.model
            AND lookup.id = records.id
            AND lookup.kind = '${field}'`;
    const lookupPredicate =
      field === "id" ? "records.id = $2" : "lookup.lookup = $2";
    const currentLookupPredicate =
      field === "id" ? "TRUE" : `records.${field} = lookup.lookup`;
    const result = await this.#database.query<PayloadRow>(
      `
        /* oidc:find:${field} */
        SELECT records.id, records.payload, records.consumed_at
        FROM ${TABLE_NAME} AS records
        ${lookupJoin}
        WHERE records.model = $1
          AND ${lookupPredicate}
          AND ${currentLookupPredicate}
          AND (
            records.expires_at IS NULL
            OR records.expires_at > CURRENT_TIMESTAMP
          )
        LIMIT 1
      `,
      [this.#model, value],
    );

    const row = result.rows[0];
    if (!row) return undefined;
    const payload = decryptPayload(
      this.#payloadKey,
      this.#model,
      row.id,
      row.payload,
    );
    if (row.consumed_at !== null) {
      payload.consumed = Number(row.consumed_at);
    }
    return payload;
  }

  #digest(
    domain: "grant" | "id" | "uid" | "user-code",
    model: string,
    value: string,
  ): string {
    return lookupDigest(this.#secret, domain, model, value);
  }

  #optionalDigest(
    domain: "uid" | "user-code",
    model: string,
    value: unknown,
  ): string | null {
    const normalized = optionalString(value);
    return normalized === null ? null : this.#digest(domain, model, normalized);
  }
}

export function createPostgresAdapter(
  database: PgPool,
  options: PostgresAdapterOptions,
): AdapterFactory {
  const validated = validateOptions(options);
  return (model: string): Adapter =>
    new PostgresAdapter(model, database, validated);
}
