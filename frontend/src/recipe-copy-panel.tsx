import { useCallback, useEffect, useId, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { copyRecipe, RecipeCopyRequestError, type RecipeCopyInput } from "./api/recipe-copy";
import { getAvailableOrganizations, OrganizationRequestError, type AvailableOrganization } from "./api/organizations";
import { pullOrganization, SyncRequestError } from "./sync-bootstrap";
import { compatibleUnit, readRecipeCopyCatalog, readRecipeCopyDestinationCatalog, matchingIds, normalized, type RecipeCopyCatalog } from "./recipe-copy-catalog";
import type { CatalogRecipe } from "./recipe-catalog";

type Dependency = { kind: "ingredient" | "tag" | "scaling" | "display"; sourceId: string; label: string; candidates: string[] };
type State =
  | { status: "closed" }
  | { status: "organizations" | "loading" | "ready" | "copying" | "success" | "error"; organizations: AvailableOrganization[]; destinationId?: string; source?: RecipeCopyCatalog; destination?: RecipeCopyCatalog; dependencies?: Dependency[]; values?: Record<string, string>; input?: RecipeCopyInput; message?: string };

function uuid() { return crypto.randomUUID(); }
function unauthorized(value: unknown) { return (value instanceof SyncRequestError || value instanceof OrganizationRequestError || value instanceof RecipeCopyRequestError) && value.status === 401; }

function dependencies(source: RecipeCopyCatalog, destination: RecipeCopyCatalog): Dependency[] {
  const recipe = source.recipe;
  const sourceIngredient = new Map(source.ingredients.map((item) => [item.versionId, item]));
  const destinationIngredients = destination.ingredients.filter((item) => !item.historical && !item.retired);
  const bySourceUnit = new Map((source.sourceUnits ?? source.units).map((unit) => [unit.id, unit]));
  const byDestinationUnit = new Map(destination.units.map((unit) => [unit.id, unit]));
  const result: Dependency[] = [];
  for (const line of recipe.ingredientLines) {
    const ingredient = sourceIngredient.get(line.ingredientVersionId);
    if (!ingredient) { result.push({ kind: "ingredient", sourceId: line.ingredientVersionId, label: line.ingredientVersionId, candidates: [] }); continue; }
    const sourceUnit = bySourceUnit.get(ingredient.canonicalUnitId ?? "");
    const candidates = destinationIngredients.filter((item) => {
      const unit = byDestinationUnit.get(item.canonicalUnitId ?? "");
      return normalized(item.name) === normalized(ingredient.name) && compatibleUnit(sourceUnit, unit);
    }).map((item) => item.versionId);
    result.push({ kind: "ingredient", sourceId: line.ingredientVersionId, label: ingredient.name, candidates });
    if (line.preferredDisplayUnitId) {
      const unit = bySourceUnit.get(line.preferredDisplayUnitId);
      const displayCandidates = unit ? destination.units.filter((item) => compatibleUnit(unit, item)).map((item) => item.id) : [];
      result.push({ kind: "display", sourceId: line.preferredDisplayUnitId, label: unit?.name ?? line.preferredDisplayUnitId, candidates: displayCandidates });
    }
  }
  const scaling = source.scalingUnits.find((item) => item.id === recipe.scalingUnitId);
  result.push({ kind: "scaling", sourceId: recipe.scalingUnitId, label: scaling?.name ?? recipe.scalingUnitId, candidates: scaling ? destination.scalingUnits.filter((item) => normalized(item.name) === normalized(scaling.name) && compatibleUnit(scaling, item)).map((item) => item.id) : [] });
  for (const tagId of recipe.recipeTagIds) {
    const tag = source.tags.find((item) => item.id === tagId);
    result.push({ kind: "tag", sourceId: tagId, label: tag?.name ?? tagId, candidates: tag ? matchingIds(destination.tags.filter((item) => !item.retired), tag.name) : [] });
  }
  const dedup = new Map(result.map((item) => [`${item.kind}:${item.sourceId}`, item]));
  return [...dedup.values()];
}

export function RecipeCopyPanel({ recipe, organizationId, userId, onUnauthenticated }: { recipe: CatalogRecipe; organizationId: string; userId: string; onUnauthenticated: () => void }) {
  const { t } = useTranslation();
  const [state, setState] = useState<State>({ status: "closed" });
  const request = useRef(0);
  const mounted = useRef(true);
  const submitting = useRef(false);
  const headingId = useId();
  useEffect(() => () => { mounted.current = false; request.current += 1; }, []);
  const update = useCallback((next: Parameters<typeof setState>[0]) => { if (mounted.current) setState(next); }, []);
  const openPanel = useCallback(async () => {
    if (navigator.onLine === false) { update({ status: "error", organizations: [], message: t("recipesCatalog.copyOnlineOnly") }); return; }
    const current = ++request.current;
    update({ status: "organizations", organizations: [] });
    try { const organizations = (await getAvailableOrganizations()).filter((item) => item.id !== organizationId); if (current === request.current && mounted.current) update({ status: "organizations", organizations }); }
    catch (reason) { if (unauthorized(reason) && mounted.current) onUnauthenticated(); if (current === request.current && mounted.current) update({ status: "error", organizations: [] }); }
  }, [organizationId, onUnauthenticated, t, update]);
  const selectDestination = useCallback(async (destinationId: string) => {
    if (!destinationId) { update((current) => current.status === "closed" ? current : { status: "organizations", organizations: current.organizations }); return; }
    const current = ++request.current;
    update((old) => ({ status: "loading", organizations: old.status === "closed" ? [] : old.organizations, destinationId }));
    try {
      await pullOrganization(userId, organizationId); await pullOrganization(userId, destinationId);
      const [source, destinationProjection] = await Promise.all([readRecipeCopyCatalog(userId, organizationId, recipe.id, true), readRecipeCopyDestinationCatalog(userId, destinationId)]);
      const destination = { ...destinationProjection, recipe: source.recipe };
      if (current !== request.current || source.recipe.versionId !== recipe.versionId) throw new Error("Recipe copy snapshot is stale.");
      const deps = dependencies(source, destination);
      const values: Record<string, string> = {};
      for (const dep of deps) if (dep.candidates.length === 1) values[`${dep.kind}:${dep.sourceId}`] = dep.candidates[0];
      if (current === request.current && mounted.current) update((old) => ({ status: "ready", organizations: old.status === "closed" ? [] : old.organizations, destinationId, source, destination, dependencies: deps, values }));
    } catch (reason) { if (unauthorized(reason) && mounted.current) onUnauthenticated(); if (current === request.current && mounted.current) update((old) => ({ status: "error", organizations: old.status === "closed" ? [] : old.organizations, destinationId })); }
  }, [organizationId, onUnauthenticated, recipe.id, recipe.versionId, update, userId]);
  async function confirm() {
    if (submitting.current || state.status !== "ready" || !state.source || !state.destination || !state.dependencies || !state.values || state.dependencies.some((dep) => !state.values?.[`${dep.kind}:${dep.sourceId}`])) return;
    submitting.current = true;
    const destinationId = state.destinationId ?? "", values = state.values;
    const ingredientVersionMappings: Record<string, string> = {}, recipeTagMappings: Record<string, string> = {}, scalingUnitMappings: Record<string, string> = {}, preferredDisplayUnitMappings: Record<string, string> = {};
    for (const dep of state.dependencies) { const value = values[`${dep.kind}:${dep.sourceId}`]; if (dep.kind === "ingredient") ingredientVersionMappings[dep.sourceId] = value; else if (dep.kind === "tag") recipeTagMappings[dep.sourceId] = value; else if (dep.kind === "scaling") scalingUnitMappings[dep.sourceId] = value; else preferredDisplayUnitMappings[dep.sourceId] = value; }
    const prior = state.input;
    const sameMappings = prior && JSON.stringify([prior.ingredientVersionMappings, prior.recipeTagMappings, prior.scalingUnitMappings, prior.preferredDisplayUnitMappings]) === JSON.stringify([ingredientVersionMappings, recipeTagMappings, scalingUnitMappings, preferredDisplayUnitMappings]);
    const input: RecipeCopyInput = sameMappings ? prior : { sourceOrganizationId: organizationId, sourceRecipeId: recipe.id, sourceCurrentRecipeVersionId: recipe.versionId, destinationRecipeId: uuid(), destinationRecipeVersionId: uuid(), ingredientVersionMappings, recipeTagMappings, scalingUnitMappings, preferredDisplayUnitMappings, mutationId: uuid(), clientWallTime: new Date().toISOString() };
    update((old) => old.status === "ready" ? { ...old, status: "copying", input } : old);
    try { await copyRecipe(userId, destinationId, input); await pullOrganization(userId, destinationId); if (mounted.current) update((old) => ({ status: "success", organizations: old.status === "closed" ? [] : old.organizations, destinationId })); }
    catch (reason) { if (unauthorized(reason) && mounted.current) onUnauthenticated(); if (mounted.current) update((old) => isDefinite(reason) ? { status: "organizations", organizations: old.status === "closed" ? [] : old.organizations } : old.status === "copying" && old.source && old.destination && old.dependencies && old.values ? { ...old, status: "ready" } : old); }
    finally { submitting.current = false; }
  }
  function isDefinite(reason: unknown) { return reason instanceof RecipeCopyRequestError && reason.status >= 400 && reason.status < 500 && reason.status !== 401; }
  if (state.status === "closed") return <button onClick={() => void openPanel()} type="button">{t("recipesCatalog.copy")}</button>;
  return <section aria-labelledby={headingId}><h4 id={headingId}>{t("recipesCatalog.copy")}</h4><label>{t("recipesCatalog.copyDestination")}<select aria-label={t("recipesCatalog.copyDestination")} disabled={state.status === "loading" || state.status === "copying"} onChange={(event) => void selectDestination(event.target.value)} value={state.destinationId ?? ""}><option value="">{t("recipesCatalog.copyChooseDestination")}</option>{state.organizations.map((item) => <option key={item.id} value={item.id}>{item.name}</option>)}</select></label>{state.status === "loading" ? <p role="status">{t("recipesCatalog.copyLoading")}</p> : null}{state.status === "error" ? <p role="alert">{state.message ?? t("recipesCatalog.errors.unavailable")}</p> : null}{state.status === "success" ? <p role="status">{t("recipesCatalog.copySaved")}</p> : null}{state.dependencies?.map((dep) => <label key={`${dep.kind}:${dep.sourceId}`}>{dep.label}<select aria-label={dep.label} disabled={state.status === "copying"} onChange={(event) => update((old) => old.status === "ready" ? { ...old, values: { ...old.values, [`${dep.kind}:${dep.sourceId}`]: event.target.value } } : old)} value={state.values?.[`${dep.kind}:${dep.sourceId}`] ?? ""}><option value="">{t("recipesCatalog.copyChooseMapping")}</option>{dep.candidates.map((id) => <option key={id} value={id}>{id}</option>)}</select></label>)}{state.status === "ready" ? <button disabled={state.dependencies?.some((dep) => !state.values?.[`${dep.kind}:${dep.sourceId}`])} onClick={() => void confirm()} type="button">{t("recipesCatalog.copyConfirm")}</button> : null}</section>;
}
