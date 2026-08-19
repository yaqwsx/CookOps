import type { IncomingMessage, ServerResponse } from "node:http";

import type { Interaction, Provider } from "oidc-provider";

import {
  type InteractionApprovalRequest,
  InteractionApprovalStore,
  interactionBinding,
  privateCredentialMatches,
} from "./interaction-approvals.js";
import type { AuthorizedGrant, GrantManagementAdapter } from "./postgres-adapter.js";

const MAX_BODY_BYTES = 1_024;
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
const HANDLE = /^[0-9a-f]{64}$/;

export interface PrivateInteractionDetails {
  interactionUid: string;
  clientId: string;
  clientName: string;
  resource: string;
  scopes: string[];
  prompt: "login" | "consent";
}

function send(response: ServerResponse, status: number): void {
  response.writeHead(status).end();
}

function grantsAdapter(provider: Provider): GrantManagementAdapter {
  const adapter = (provider as unknown as { Grant?: { adapter?: unknown } }).Grant?.adapter;
  if (!adapter || typeof adapter !== "object" || !("listAuthorizedGrants" in adapter) || !("revokeGrant" in adapter)) throw new TypeError("grant management is unavailable");
  return adapter as GrantManagementAdapter;
}

function subject(value: unknown): string | undefined {
  return typeof value === "string" && UUID.test(value) ? value : undefined;
}

function grantJson(grant: AuthorizedGrant): object {
  return { handle: grant.handle, clientId: grant.clientId, ...(grant.issuedAt === undefined ? {} : { issuedAt: grant.issuedAt }), ...(grant.expiresAt === undefined ? {} : { expiresAt: grant.expiresAt }) };
}

async function body(request: IncomingMessage): Promise<InteractionApprovalRequest | undefined> {
  if (request.headers["content-type"]?.split(";", 1)[0] !== "application/json") {
    return undefined;
  }
  let size = 0;
  const chunks: Buffer[] = [];
  for await (const chunk of request) {
    const bytes = Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk);
    size += bytes.byteLength;
    if (size > MAX_BODY_BYTES) return undefined;
    chunks.push(bytes);
  }
  try {
    const parsed: unknown = JSON.parse(Buffer.concat(chunks).toString("utf8"));
    if (
      typeof parsed !== "object" ||
      parsed === null ||
      Array.isArray(parsed) ||
      Object.keys(parsed).length !== 3 ||
      !("interactionUid" in parsed) ||
      !("subject" in parsed) ||
      !("decision" in parsed)
    ) {
      return undefined;
    }
    return parsed as InteractionApprovalRequest;
  } catch {
    return undefined;
  }
}

async function grantForConsent(provider: Provider, interaction: Interaction, subject: string) {
  const existing = interaction.grantId ? await provider.Grant.find(interaction.grantId) : undefined;
  const grant =
    existing ??
    new provider.Grant({ accountId: subject, clientId: String(interaction.params.client_id) });
  const missingOidcScope = interaction.prompt.details.missingOIDCScope as string[] | undefined;
  if (missingOidcScope) grant.addOIDCScope(missingOidcScope.join(" "));
  const missingOidcClaims = interaction.prompt.details.missingOIDCClaims as string[] | undefined;
  if (missingOidcClaims) grant.addOIDCClaims(missingOidcClaims);
  const missingResourceScopes = interaction.prompt.details.missingResourceScopes as
    | Record<string, string[]>
    | undefined;
  for (const [resource, scopes] of Object.entries(missingResourceScopes ?? {})) {
    grant.addResourceScope(resource, scopes.join(" "));
  }
  return grant;
}

async function detailsFor(provider: Provider, interactionUid: string): Promise<PrivateInteractionDetails | undefined> {
  const interaction = await provider.Interaction.find(interactionUid);
  if (!interaction) return undefined;
  const binding = interactionBinding(interaction);
  const client = await provider.Client.find(binding.clientId);
  if (!client) throw new TypeError("interaction client is unavailable");
  return {
    interactionUid: binding.interactionUid,
    clientId: binding.clientId,
    clientName: client.clientName ?? binding.clientId,
    resource: binding.resource,
    scopes: binding.scope.split(" "),
    prompt: binding.prompt,
  };
}

/**
 * Handles only the private record endpoint and the browser completion endpoint.
 * Callers route all other paths to oidc-provider. Neither endpoint logs an
 * approval, credential, user UUID, or interaction UID.
 */
export async function handleInteractionBridgeRequest(
  provider: Provider,
  approvals: InteractionApprovalStore,
  credential: Uint8Array,
  detailsCredential: Uint8Array,
  basePath: string,
  request: IncomingMessage,
  response: ServerResponse,
  grantsCredential?: Uint8Array,
): Promise<boolean> {
  const url = new URL(request.url ?? "/", "http://localhost");
  const privatePath = `${basePath}/private/interactions/approval`;
  const grantsPath = `${basePath}/private/grants`;
  const grantMatch = new RegExp(`^${basePath.replace(/[.*+?^${}()|[\\]\\]/g, "\\$&")}/private/grants/([0-9a-f]{64})$`).exec(url.pathname);
  if (url.pathname === grantsPath || grantMatch) {
    if (!grantsCredential || !privateCredentialMatches(grantsCredential, request.headers.authorization)) { send(response, 401); return true; }
    try {
      const adapter = grantsAdapter(provider);
      const parsed = request.method === "GET" ? undefined : await bodyObject(request);
      const requestedSubject = request.method === "GET"
        ? (url.searchParams.size === 1 && url.searchParams.has("subject") ? subject(url.searchParams.get("subject")) : undefined)
        : subject(parsed?.subject);
      if (!requestedSubject || (grantMatch && (request.method !== "DELETE" || !HANDLE.test(grantMatch[1] ?? ""))) || (!grantMatch && request.method !== "GET")) { send(response, 400); return true; }
      if (grantMatch) {
        const revoked = await adapter.revokeGrant(requestedSubject, grantMatch[1]!);
        send(response, revoked ? 204 : 404);
      } else {
        const grants = await adapter.listAuthorizedGrants(requestedSubject);
        response.writeHead(200, { "content-type": "application/json", "cache-control": "no-store" }).end(JSON.stringify(grants.map(grantJson)));
      }
    } catch { send(response, 400); }
    return true;
  }
  const details = new RegExp(`^${basePath.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}/private/interactions/([A-Za-z0-9_-]{16,255})$`);
  const detailsMatch = details.exec(url.pathname);
  if (detailsMatch) {
    if (request.method !== "GET" || !privateCredentialMatches(detailsCredential, request.headers.authorization)) {
      send(response, 401);
      return true;
    }
    try {
      const interactionUid = detailsMatch[1];
      if (!interactionUid) throw new TypeError("interaction UID is unavailable");
      const detail = await detailsFor(provider, interactionUid);
      if (!detail) send(response, 404);
      else response.writeHead(200, { "content-type": "application/json", "cache-control": "no-store" }).end(JSON.stringify(detail));
    } catch {
      send(response, 400);
    }
    return true;
  }
  if (url.pathname === privatePath) {
    if (request.method !== "POST" || !privateCredentialMatches(credential, request.headers.authorization)) {
      send(response, 401);
      return true;
    }
    const submitted = await body(request);
    if (!submitted) {
      send(response, 400);
      return true;
    }
    try {
      const interaction = await provider.Interaction.find(submitted.interactionUid);
      if (!interaction) {
        send(response, 404);
        return true;
      }
      send(response, (await approvals.record(interaction, submitted)) ? 204 : 409);
    } catch {
      // Deliberately non-enumerating. Invalid protocol input is not a reason to
      // disclose interaction state to a private client that has made an error.
      send(response, 400);
    }
    return true;
  }

  const complete = new RegExp(`^${basePath.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}/interaction/([A-Za-z0-9_-]{16,255})/complete$`);
  const matched = complete.exec(url.pathname);
  if (!matched) return false;
  if (request.method !== "GET") {
    send(response, 405);
    return true;
  }
  try {
    const interaction = await provider.interactionDetails(request, response);
    const binding = interactionBinding(interaction);
    if (binding.interactionUid !== matched[1]) {
      send(response, 400);
      return true;
    }
    const expectedSubject =
      interaction.prompt.name === "consent" ? interaction.session?.accountId : undefined;
    if (interaction.prompt.name === "consent" && !expectedSubject) {
      send(response, 403);
      return true;
    }
    const approval = await approvals.consume(binding, expectedSubject);
    if (!approval) {
      send(response, 403);
      return true;
    }
    if (approval.decision === "deny") {
      await provider.interactionFinished(
        request,
        response,
        { error: "access_denied" },
        { mergeWithLastSubmission: false },
      );
      return true;
    }
    if (interaction.prompt.name === "login") {
      await provider.interactionFinished(
        request,
        response,
        { login: { accountId: approval.subject } },
        { mergeWithLastSubmission: false },
      );
      return true;
    }
    const grant = await grantForConsent(provider, interaction, approval.subject);
    await provider.interactionFinished(
      request,
      response,
      { consent: { grantId: await grant.save() } },
      { mergeWithLastSubmission: true },
    );
  } catch {
    send(response, 400);
  }
  return true;
}

async function bodyObject(request: IncomingMessage): Promise<Record<string, unknown> | undefined> {
  if (request.headers["content-type"]?.split(";", 1)[0] !== "application/json") return undefined;
  let size = 0; const chunks: Buffer[] = [];
  for await (const chunk of request) { const bytes = Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk); size += bytes.length; if (size > MAX_BODY_BYTES) return undefined; chunks.push(bytes); }
  try { const parsed: unknown = JSON.parse(Buffer.concat(chunks).toString("utf8")); return typeof parsed === "object" && parsed !== null && !Array.isArray(parsed) && Object.keys(parsed).length === 1 ? parsed as Record<string, unknown> : undefined; } catch { return undefined; }
}
