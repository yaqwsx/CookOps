import { useCallback, useMemo, useRef, useState } from "react";
import { useTranslation } from "react-i18next";

import {
  copyIngredient,
  IngredientCopyRequestError,
  getIngredientCopyPreview,
  type IngredientCopyMapping,
  type IngredientCopyMappingRequirement,
  type IngredientCopyPreview,
} from "./api/ingredient-copy";
import {
  getAvailableOrganizations,
  OrganizationRequestError,
  type AvailableOrganization,
} from "./api/organizations";
import { pullOrganization, SyncRequestError } from "./sync-bootstrap";
import {
  readIngredientCopyCatalog,
  type IngredientCopyCatalog,
} from "./ingredient-copy-catalog";
import type { IngredientCatalogProjection } from "./ingredient-catalog";
import { readIngredientCatalog } from "./ingredient-catalog";

type PreviewState = {
  organizations: AvailableOrganization[];
  destinationId: string;
  preview: IngredientCopyPreview;
  sourceIngredient: Ingredient;
  sourceCatalog: IngredientCopyCatalog;
  destinationCatalog: IngredientCopyCatalog;
  mappings: Record<string, string | null>;
  mutationId: string;
  clientWallTime: string;
  copyError?: boolean;
};

type PanelState =
  | { status: "closed" }
  | { status: "organizations"; organizations: AvailableOrganization[] }
  | { status: "loading"; organizations: AvailableOrganization[]; destinationId: string }
  | ({ status: "ready" | "copying" } & PreviewState)
  | {
      status: "success";
      organizations: AvailableOrganization[];
      destinationId: string;
    }
  | { status: "error"; organizations: AvailableOrganization[]; destinationId?: string };

type Ingredient = IngredientCatalogProjection["ingredients"][number];

function requirementKey(requirement: IngredientCopyMappingRequirement) {
  return `${requirement.kind}:${requirement.sourceId}`;
}

function candidateIds(
  requirement: IngredientCopyMappingRequirement,
  ingredient: Ingredient,
  sourceCatalog: IngredientCopyCatalog,
  destinationCatalog: IngredientCopyCatalog,
): string[] {
  if (requirement.kind === "canonical_unit") {
    const source = sourceCatalog.units.find((item) => item.id === requirement.sourceId);
    if (!source) return [];
    const versions = ingredient.versions ?? [
      {
        id: ingredient.versionId,
        name: ingredient.name,
        canonicalUnitName: ingredient.canonicalUnitName,
        mass: ingredient.massPerCanonicalQuantity,
        canonicalUnitId: ingredient.canonicalUnitId ?? "",
        dietaryTagIds: ingredient.dietaryTagIds ?? [],
        defaultStoreSectionId: ingredient.defaultStoreSectionId ?? null,
      },
    ];
    const masses = new Set(
      versions
        .filter((version) => version.canonicalUnitId === requirement.sourceId)
        .map((version) => version.mass),
    );
    return destinationCatalog.units
      .filter(
        (item) =>
          item.dimension === source.dimension &&
          (source.dimension !== "mass" || masses.has(item.baseUnitFactor ?? "")),
      )
      .map((item) => item.id);
  }
  if (requirement.kind === "default_store_section") {
    return destinationCatalog.sections.map((section) => section.id);
  }
  return destinationCatalog.dietaryTags
    .filter((tag) => requirement.seedKey === null || tag.seedKey === requirement.seedKey)
    .map((tag) => tag.id);
}

function mappingGroupKey(
  requirement: IngredientCopyMappingRequirement,
  sourceCatalog: IngredientCopyCatalog,
): string | null {
  if (requirement.kind !== "canonical_unit") return null;
  const dimension = sourceCatalog.units.find((item) => item.id === requirement.sourceId)?.dimension;
  return dimension === "count" || dimension === "custom" ? `${requirement.kind}:${dimension}` : null;
}

function candidateIdsForRequirement(
  requirement: IngredientCopyMappingRequirement,
  requirements: IngredientCopyMappingRequirement[],
  ingredient: Ingredient,
  sourceCatalog: IngredientCopyCatalog,
  destinationCatalog: IngredientCopyCatalog,
): string[] {
  const group = mappingGroupKey(requirement, sourceCatalog);
  if (!group) return candidateIds(requirement, ingredient, sourceCatalog, destinationCatalog);
  const grouped = requirements.filter((item) => mappingGroupKey(item, sourceCatalog) === group);
  const candidates = grouped.map((item) =>
    candidateIds(item, ingredient, sourceCatalog, destinationCatalog),
  );
  return candidates[0]?.filter((id) => candidates.every((items) => items.includes(id))) ?? [];
}

function mappingsAreComplete(
  preview: IngredientCopyPreview,
  ingredient: Ingredient,
  sourceCatalog: IngredientCopyCatalog,
  destinationCatalog: IngredientCopyCatalog,
  mappings: Record<string, string | null>,
): boolean {
  const groups = new Map<string, IngredientCopyMappingRequirement[]>();
  for (const requirement of preview.mappingRequirements) {
    const group = mappingGroupKey(requirement, sourceCatalog);
    if (group) groups.set(group, [...(groups.get(group) ?? []), requirement]);
    const value = mappings[requirementKey(requirement)] ?? null;
    const required = requirement.kind !== "dietary_tag" || requirement.seedKey !== null;
    if (required && value === null) return false;
    if (value !== null && !candidateIdsForRequirement(requirement, preview.mappingRequirements, ingredient, sourceCatalog, destinationCatalog).includes(value)) return false;
  }
  for (const requirements of groups.values()) {
    const values = requirements.map((requirement) => mappings[requirementKey(requirement)] ?? null);
    if (values.some((value) => value === null) || new Set(values).size !== 1) return false;
  }
  return true;
}

function sourceLabel(
  requirement: IngredientCopyMappingRequirement,
  sourceCatalog: IngredientCopyCatalog,
  t: (key: string) => string,
) {
  if (requirement.kind === "canonical_unit")
    return sourceCatalog.units.find((item) => item.id === requirement.sourceId)?.name ?? t("ingredientsCatalog.copyUnknownDependency");
  if (requirement.kind === "default_store_section")
    return sourceCatalog.sections.find((item) => item.id === requirement.sourceId)?.name ?? t("ingredientsCatalog.copyUnknownDependency");
  return sourceCatalog.dietaryTags.find((item) => item.id === requirement.sourceId)?.name ?? t("ingredientsCatalog.copyUnknownDependency");
}

function optionLabel(
  requirement: IngredientCopyMappingRequirement,
  id: string,
  destinationCatalog: IngredientCopyCatalog,
  fallback: string,
) {
  if (requirement.kind === "canonical_unit") return destinationCatalog.units.find((item) => item.id === id)?.name ?? fallback;
  if (requirement.kind === "default_store_section") return destinationCatalog.sections.find((item) => item.id === id)?.name ?? fallback;
  return destinationCatalog.dietaryTags.find((item) => item.id === id)?.name ?? fallback;
}

function isUnauthorized(reason: unknown): boolean {
  return (
    (reason instanceof SyncRequestError ||
      reason instanceof IngredientCopyRequestError ||
      reason instanceof OrganizationRequestError) &&
    reason.status === 401
  );
}

function isDefiniteRejection(reason: unknown): boolean {
  return reason instanceof IngredientCopyRequestError && reason.status >= 400 && reason.status < 500 && reason.status !== 401;
}

export function IngredientCopyPanel({
  ingredient,
  organizationId,
  userId,
  onUnauthenticated,
}: {
  ingredient: Ingredient;
  organizationId: string;
  userId: string;
  onUnauthenticated: () => void;
}) {
  const { t } = useTranslation();
  const [state, setState] = useState<PanelState>({ status: "closed" });
  const requestNumber = useRef(0);
  const open = state.status !== "closed";
  const organizations = state.status === "closed" ? [] : state.organizations;

  const openPanel = useCallback(async () => {
    const currentRequest = ++requestNumber.current;
    setState({ status: "organizations", organizations: [] });
    try {
      const available = await getAvailableOrganizations();
      if (currentRequest !== requestNumber.current) return;
      setState({ status: "organizations", organizations: available });
    } catch (reason) {
      if (currentRequest !== requestNumber.current) return;
      if (isUnauthorized(reason)) onUnauthenticated();
      setState({ status: "error", organizations: [] });
    }
  }, [onUnauthenticated]);

  const selectDestination = useCallback(
    async (destinationId: string) => {
      if (!destinationId) {
        setState((current) =>
          current.status === "closed"
            ? current
            : { status: "organizations", organizations: current.organizations },
        );
        return;
      }
      const currentRequest = ++requestNumber.current;
      const organizationList = state.status === "closed" ? [] : state.organizations;
      setState((current) => ({
        status: "loading",
        organizations: current.status === "closed" ? organizationList : current.organizations,
        destinationId,
      }));
      try {
        await pullOrganization(userId, organizationId);
        await pullOrganization(userId, destinationId);
        const [preview, sourceCatalog, sourceProjection, destinationCatalog] = await Promise.all([
          getIngredientCopyPreview(destinationId, organizationId, ingredient.id),
          readIngredientCopyCatalog(userId, organizationId, "source"),
          readIngredientCatalog(userId, organizationId, true),
          readIngredientCopyCatalog(userId, destinationId),
        ]);
        const refreshedIngredient = sourceProjection.ingredients.find(
          (item) => item.id === ingredient.id,
        );
        if (
          currentRequest !== requestNumber.current ||
          preview.sourceOrganizationId !== organizationId ||
          preview.destinationOrganizationId !== destinationId ||
          preview.sourceIngredientId !== ingredient.id ||
          !refreshedIngredient ||
          refreshedIngredient.retired ||
          refreshedIngredient.versionId !== preview.sourceVersionId
        )
          throw new Error("Ingredient copy preview is stale or unavailable.");
        const currentIngredient = {
          ...refreshedIngredient,
          canonicalUnitId: refreshedIngredient.canonicalUnitId ?? preview.canonicalUnitId,
        };
        const mappings: Record<string, string | null> = {};
        for (const requirement of preview.mappingRequirements) {
          const candidates = candidateIdsForRequirement(
            requirement,
            preview.mappingRequirements,
            currentIngredient,
            sourceCatalog,
            destinationCatalog,
          );
          mappings[requirementKey(requirement)] =
            requirement.kind === "dietary_tag" && requirement.seedKey === null
              ? null
              : candidates[0] ?? null;
        }
        for (const requirement of preview.mappingRequirements) {
          const group = mappingGroupKey(requirement, sourceCatalog);
          if (!group) continue;
          const value = mappings[requirementKey(requirement)];
          for (const peer of preview.mappingRequirements) {
            if (mappingGroupKey(peer, sourceCatalog) === group)
              mappings[requirementKey(peer)] = value;
          }
        }
        setState((current) => ({
          status: "ready",
          organizations: current.status === "closed" ? organizationList : current.organizations,
          destinationId,
          preview,
          sourceIngredient: currentIngredient,
          sourceCatalog,
          destinationCatalog,
          mappings,
          mutationId: crypto.randomUUID(),
          clientWallTime: new Date().toISOString(),
        }));
      } catch (reason) {
        if (currentRequest !== requestNumber.current) return;
        if (isUnauthorized(reason)) onUnauthenticated();
        setState((current) => ({
          status: "error",
          organizations: current.status === "closed" ? organizationList : current.organizations,
          destinationId,
        }));
      }
    },
    [ingredient, onUnauthenticated, organizationId, state, userId],
  );

  const close = useCallback(() => {
    requestNumber.current += 1;
    setState({ status: "closed" });
  }, []);

  const submitting = useRef(false);

  async function confirm() {
    if (submitting.current || state.status !== "ready") return;
    const prepared = state;
    const mappings: IngredientCopyMapping[] = state.preview.mappingRequirements.map((requirement) => ({
      kind: requirement.kind,
      sourceId: requirement.sourceId,
      destinationId: state.mappings[requirementKey(requirement)] ?? null,
    }));
    if (!mappingsAreComplete(
      state.preview,
      state.sourceIngredient,
      state.sourceCatalog,
      state.destinationCatalog,
      state.mappings,
    ))
      return;
    submitting.current = true;
    const currentRequest = requestNumber.current;
    setState({
      ...prepared,
      status: "copying",
    });
    try {
      await copyIngredient(userId, state.destinationId, {
        sourceOrganizationId: organizationId,
        ingredientId: ingredient.id,
        mutationId: state.mutationId,
        clientWallTime: state.clientWallTime,
        preconditionFingerprint: state.preview.preconditionFingerprint,
        mappings,
      });
      try {
        await pullOrganization(userId, state.destinationId);
      } catch (reason) {
        if (isUnauthorized(reason)) onUnauthenticated();
        // The copy is committed; a later normal sync will refresh the destination.
      }
      if (currentRequest !== requestNumber.current) return;
      setState({
        status: "success",
        organizations: state.organizations,
        destinationId: state.destinationId,
      });
    } catch (reason) {
      if (currentRequest === requestNumber.current) {
        if (isUnauthorized(reason)) onUnauthenticated();
        if (isDefiniteRejection(reason)) {
          setState((current) =>
            current.status === "copying"
              ? { status: "error", organizations: current.organizations, destinationId: current.destinationId }
              : current,
          );
          return;
        }
        setState((current) =>
          current.status === "copying" ? { ...current, status: "ready", copyError: true } : current,
        );
      }
    } finally {
      submitting.current = false;
    }
  }

  const readyState = state.status === "ready" ? state : undefined;
  const selectedDestination =
    state.status === "loading" || state.status === "ready" || state.status === "copying" || state.status === "success"
      ? state.destinationId
      : "";
  const missingMapping = useMemo(
    () =>
      readyState ? !mappingsAreComplete(
        readyState.preview,
        readyState.sourceIngredient,
        readyState.sourceCatalog,
        readyState.destinationCatalog,
        readyState.mappings,
      ) : false,
    [readyState],
  );

  return (
    <section
      className="ingredient-copy"
      aria-label={!open ? t("ingredientsCatalog.copyAction") : undefined}
      aria-labelledby={open ? "ingredient-copy-heading" : undefined}
    >
      {!open ? (
        <button type="button" onClick={() => void openPanel()}>
          {t("ingredientsCatalog.copyAction")}
        </button>
      ) : (
        <>
          <h3 id="ingredient-copy-heading">{t("ingredientsCatalog.copyHeading")}</h3>
          <p>{t("ingredientsCatalog.copyOnlineOnly")}</p>
          <label>
            {t("ingredientsCatalog.copyDestination")}
            <select
              aria-describedby="ingredient-copy-help"
              disabled={state.status === "loading" || state.status === "copying" || state.status === "success"}
              onChange={(event) => void selectDestination(event.currentTarget.value)}
              value={selectedDestination}
            >
              <option value="">{t("ingredientsCatalog.copyChooseDestination")}</option>
              {organizations
                .filter((organization) => organization.id !== organizationId)
                .map((organization) => (
                  <option key={organization.id} value={organization.id}>
                    {organization.name}
                  </option>
                ))}
            </select>
          </label>
          <p id="ingredient-copy-help" role="status" aria-live="polite">
            {state.status === "organizations" ? t("ingredientsCatalog.copyChooseDestination") : null}
            {state.status === "loading" || state.status === "copying" ? t("ingredientsCatalog.copyLoading") : null}
            {state.status === "ready" && state.copyError ? t("ingredientsCatalog.copyUnavailable") : null}
            {state.status === "error" ? t("ingredientsCatalog.copyUnavailable") : null}
            {state.status === "success" ? t("ingredientsCatalog.copySuccess") : null}
          </p>
          {readyState ? (
            <div className="ingredient-copy__preview">
              <p>
                {t("ingredientsCatalog.copySource", {
                  name: readyState.preview.sourceName,
                })}
              </p>
              <p>
                {t("ingredientsCatalog.copyCurrentVersion")}
              </p>
              {readyState.preview.mappingRequirements.length ? (
                <fieldset>
                  <legend>{t("ingredientsCatalog.copyMappings")}</legend>
                  {readyState.preview.mappingRequirements.map((requirement) => {
                    const key = requirementKey(requirement);
                    const candidates = candidateIdsForRequirement(
                      requirement,
                      readyState.preview.mappingRequirements,
                      readyState.sourceIngredient,
                      readyState.sourceCatalog,
                      readyState.destinationCatalog,
                    );
                    return (
                      <label key={key}>
                        {t(`ingredientsCatalog.copyRequirement.${requirement.kind}`, {
                          source: sourceLabel(requirement, readyState.sourceCatalog, t),
                        })}
                        <select
                          onChange={(event) => {
                            const value = event.currentTarget.value || null;
                            setState((current) =>
                              current.status !== "ready"
                                ? current
                                : {
                                    ...current,
                                    mappings: {
                                      ...current.mappings,
                                      [key]: value,
                                      ...(() => {
                                        const group = mappingGroupKey(requirement, current.sourceCatalog);
                                        if (!group) return {};
                                        return Object.fromEntries(
                                          current.preview.mappingRequirements
                                            .filter((peer) => mappingGroupKey(peer, current.sourceCatalog) === group)
                                            .map((peer) => [requirementKey(peer), value]),
                                        );
                                      })(),
                                    },
                                  },
                            )
                          }}
                          value={readyState.mappings[key] ?? ""}
                        >
                          <option value="">
                            {requirement.kind === "dietary_tag"
                              ? t("ingredientsCatalog.copyCreateTag")
                              : t("ingredientsCatalog.copySelectMapping")}
                          </option>
                          {candidates.map((id) => (
                            <option key={id} value={id}>
                              {optionLabel(
                                requirement,
                                id,
                                readyState.destinationCatalog,
                                t("ingredientsCatalog.copyUnknownDependency"),
                              )}
                            </option>
                          ))}
                        </select>
                      </label>
                    );
                  })}
                </fieldset>
              ) : (
                <p>{t("ingredientsCatalog.copyNoMappings")}</p>
              )}
              <button
                disabled={missingMapping}
                onClick={() => void confirm()}
                type="button"
              >
                {t("ingredientsCatalog.copyConfirm")}
              </button>
            </div>
          ) : null}
          <button disabled={state.status === "loading" || state.status === "copying"} onClick={close} type="button">
            {t("ingredientsCatalog.copyCancel")}
          </button>
        </>
      )}
    </section>
  );
}
