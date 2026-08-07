import { createHmac, timingSafeEqual } from "node:crypto";

import type { Interaction } from "oidc-provider";

import type { PgPool, PgQueryable } from "./postgres-adapter.js";

const APPROVAL_TTL_SECONDS = 5 * 60;
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
const INTERACTION_UID = /^[A-Za-z0-9_-]{16,255}$/;

export type ApprovalDecision = "approve" | "deny";

export interface InteractionApprovalRequest {
  interactionUid: string;
  subject: string;
  decision: ApprovalDecision;
}

export interface InteractionBinding {
  interactionUid: string;
  clientId: string;
  resource: string;
  scope: string;
  prompt: "login" | "consent";
  expiresAt: Date;
}

export interface ConsumedInteractionApproval extends InteractionBinding {
  subject: string;
  decision: ApprovalDecision;
}

interface ApprovalRow {
  client_id: string;
  resource: string;
  scope: string;
  prompt: "login" | "consent";
  subject: string;
  decision: ApprovalDecision;
  expires_at: Date;
}

function text(value: unknown, name: string): string {
  if (typeof value !== "string" || !value || value !== value.trim() || value.length > 4_096) {
    throw new TypeError(`${name} must be a nonblank trimmed string`);
  }
  return value;
}

export function interactionBinding(interaction: Interaction): InteractionBinding {
  const params = interaction.params;
  const prompt = interaction.prompt.name;
  if (prompt !== "login" && prompt !== "consent") {
    throw new TypeError("interaction prompt is not supported");
  }
  if (!Number.isSafeInteger(interaction.exp) || interaction.exp * 1_000 <= Date.now()) {
    throw new TypeError("interaction is expired");
  }
  return {
    interactionUid: validateInteractionUid(interaction.uid),
    clientId: text(params.client_id, "interaction client ID"),
    resource: text(params.resource, "interaction resource"),
    scope: text(params.scope, "interaction scope"),
    prompt,
    expiresAt: new Date(interaction.exp * 1_000),
  };
}

function digest(secret: Uint8Array, interactionUid: string): string {
  return createHmac("sha256", secret)
    .update("cookops:oauth-interaction-approval:v1\0", "utf8")
    .update(interactionUid, "utf8")
    .digest("hex");
}

function validSecret(value: Uint8Array): Uint8Array {
  if (!(value instanceof Uint8Array) || value.byteLength < 32) {
    throw new TypeError("interaction approval secret must contain at least 32 bytes");
  }
  return Uint8Array.from(value);
}

function request(request: InteractionApprovalRequest): InteractionApprovalRequest {
  if (typeof request !== "object" || request === null) {
    throw new TypeError("interaction approval is required");
  }
  const interactionUid = validateInteractionUid(request.interactionUid);
  const subject = text(request.subject, "approval subject");
  if (!UUID.test(subject)) throw new TypeError("approval subject must be a UUID");
  if (request.decision !== "approve" && request.decision !== "deny") {
    throw new TypeError("approval decision must be approve or deny");
  }
  return { interactionUid, subject, decision: request.decision };
}

function validateInteractionUid(value: unknown): string {
  const normalized = text(value, "interaction UID");
  if (!INTERACTION_UID.test(normalized)) {
    throw new TypeError("interaction UID must be an opaque base64url value");
  }
  return normalized;
}

export function privateCredentialMatches(expected: Uint8Array, presented: string | undefined): boolean {
  if (!presented?.startsWith("Bearer ")) return false;
  const encoded = presented.slice("Bearer ".length);
  if (!/^[A-Za-z0-9_-]+$/.test(encoded)) return false;
  const actual = Buffer.from(encoded, "base64url");
  const wanted = Buffer.from(expected);
  return (
    actual.toString("base64url") === encoded &&
    actual.byteLength === wanted.byteLength &&
    timingSafeEqual(actual, wanted)
  );
}

export class InteractionApprovalStore {
  readonly #database: PgPool;
  readonly #secret: Uint8Array;

  constructor(database: PgPool, secret: Uint8Array) {
    this.#database = database;
    this.#secret = validSecret(secret);
  }

  async record(
    interaction: Interaction,
    submitted: InteractionApprovalRequest,
  ): Promise<boolean> {
    const binding = interactionBinding(interaction);
    const approval = request(submitted);
    if (binding.interactionUid !== approval.interactionUid) return false;
    const expiry = new Date(
      Math.min(binding.expiresAt.getTime(), Date.now() + APPROVAL_TTL_SECONDS * 1_000),
    );
    const result = await this.#database.query(
      `
        INSERT INTO oidc_interaction_approvals
          (interaction_digest, client_id, resource, scope, prompt, subject, decision, expires_at)
        VALUES ($1, $2, $3, $4, $5, $6::uuid, $7, $8)
        ON CONFLICT (interaction_digest) DO NOTHING
      `,
      [
        digest(this.#secret, binding.interactionUid),
        binding.clientId,
        binding.resource,
        binding.scope,
        binding.prompt,
        approval.subject,
        approval.decision,
        expiry,
      ],
    );
    return result.rowCount === 1;
  }

  async consume(
    binding: InteractionBinding,
    expectedSubject?: string,
  ): Promise<ConsumedInteractionApproval | undefined> {
    const client = await this.#database.connect();
    try {
      await client.query("BEGIN");
      const result = await client.query<ApprovalRow>(
        `
          SELECT client_id, resource, scope, prompt, subject, decision, expires_at
          FROM oidc_interaction_approvals
          WHERE interaction_digest = $1
            AND consumed_at IS NULL
            AND expires_at > CURRENT_TIMESTAMP
            AND ($2::uuid IS NULL OR subject = $2::uuid)
          FOR UPDATE
        `,
        [digest(this.#secret, binding.interactionUid), expectedSubject ?? null],
      );
      const approval = result.rows[0];
      if (
        !approval ||
        approval.client_id !== binding.clientId ||
        approval.resource !== binding.resource ||
        approval.scope !== binding.scope ||
        approval.prompt !== binding.prompt
      ) {
        await client.query("ROLLBACK");
        return undefined;
      }
      const consumed = await client.query(
        `
          UPDATE oidc_interaction_approvals
          SET consumed_at = CURRENT_TIMESTAMP
          WHERE interaction_digest = $1 AND consumed_at IS NULL
        `,
        [digest(this.#secret, binding.interactionUid)],
      );
      if (consumed.rowCount !== 1) {
        await client.query("ROLLBACK");
        return undefined;
      }
      await client.query("COMMIT");
      return { ...binding, subject: approval.subject, decision: approval.decision };
    } catch (error) {
      await client.query("ROLLBACK");
      throw error;
    } finally {
      client.release();
    }
  }
}

/** Initializes the approval table independently of oidc-provider records. */
export async function initializeInteractionApprovals(database: PgQueryable): Promise<void> {
  await database.query(`
    CREATE TABLE IF NOT EXISTS oidc_interaction_approvals (
      interaction_digest text PRIMARY KEY CHECK (interaction_digest ~ '^[0-9a-f]{64}$'),
      client_id text NOT NULL CHECK (client_id <> ''),
      resource text NOT NULL CHECK (resource <> ''),
      scope text NOT NULL CHECK (scope <> ''),
      prompt text NOT NULL CHECK (prompt IN ('login', 'consent')),
      subject uuid NOT NULL,
      decision text NOT NULL CHECK (decision IN ('approve', 'deny')),
      expires_at timestamptz NOT NULL,
      consumed_at timestamptz
    );
    CREATE INDEX IF NOT EXISTS oidc_interaction_approvals_expiry_idx
      ON oidc_interaction_approvals (expires_at) WHERE consumed_at IS NULL;
  `);
}
