import type { IncomingMessage, ServerResponse } from "node:http";

import type { Interaction, Provider } from "oidc-provider";

import {
  type InteractionApprovalRequest,
  InteractionApprovalStore,
  interactionBinding,
  privateCredentialMatches,
} from "./interaction-approvals.js";

const MAX_BODY_BYTES = 1_024;

function send(response: ServerResponse, status: number): void {
  response.writeHead(status).end();
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
  const missingResourceScopes = interaction.prompt.details.missingResourceScopes as
    | Record<string, string[]>
    | undefined;
  for (const [resource, scopes] of Object.entries(missingResourceScopes ?? {})) {
    grant.addResourceScope(resource, scopes);
  }
  return grant;
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
  basePath: string,
  request: IncomingMessage,
  response: ServerResponse,
): Promise<boolean> {
  const url = new URL(request.url ?? "/", "http://localhost");
  const privatePath = `${basePath}/private/interactions/approval`;
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
