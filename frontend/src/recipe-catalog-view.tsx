import { liveQuery } from "dexie";
import { useCallback, useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";

import {
  readRecipeCatalog,
  type RecipeCatalogProjection,
} from "./recipe-catalog";
import { queueRecipeCreate, type RecipeCreateInput } from "./recipe-create";
import {
  queueRecipeVersionPublish,
  type RecipeVersionInput,
} from "./recipe-publish";
import { queueRecipeLifecycle } from "./recipe-lifecycle";
import { pullOrganization, SyncRequestError } from "./sync-bootstrap";

type CatalogState =
  | { status: "loading" }
  | { status: "ready" | "offline"; catalog: RecipeCatalogProjection }
  | { status: "error" };

const initialInput: RecipeCreateInput = {
  name: "",
  description: "",
  scalingUnitId: "",
  baseScalingAmount: "1",
};
const errors = new Set([
  "name",
  "description",
  "scalingUnit",
  "baseScalingAmount",
]);

function RecipeCreateForm({
  catalog,
  organizationId,
  userId,
}: {
  catalog: RecipeCatalogProjection;
  organizationId: string;
  userId: string;
}) {
  const { t } = useTranslation();
  const [input, setInput] = useState(initialInput);
  const [error, setError] = useState<string>();
  const [saved, setSaved] = useState(false);
  const submitting = useRef(false);

  useEffect(() => {
    setInput((current) => ({
      ...current,
      scalingUnitId: catalog.scalingUnits.some(
        (unit) => unit.id === current.scalingUnitId,
      )
        ? current.scalingUnitId
        : (catalog.scalingUnits[0]?.id ?? ""),
    }));
  }, [catalog.scalingUnits]);

  function change(field: keyof RecipeCreateInput, value: string) {
    setInput((current) => ({ ...current, [field]: value }));
    setError(undefined);
    setSaved(false);
  }

  async function submit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (submitting.current) return;
    submitting.current = true;
    try {
      await queueRecipeCreate(userId, organizationId, input);
      setInput({
        ...initialInput,
        scalingUnitId: catalog.scalingUnits[0]?.id ?? "",
      });
      setSaved(true);
    } catch (reason) {
      setSaved(false);
      setError(
        reason instanceof Error && errors.has(reason.message)
          ? reason.message
          : "unavailable",
      );
    } finally {
      submitting.current = false;
    }
  }

  return (
    <form className="recipe-create" onSubmit={(event) => void submit(event)}>
      <h3>{t("recipesCatalog.createHeading")}</h3>
      <div className="recipe-create__fields">
        <label>
          {t("recipesCatalog.name")}
          <input
            autoComplete="off"
            maxLength={200}
            onChange={(event) => change("name", event.target.value)}
            required
            value={input.name}
          />
        </label>
        <label>
          {t("recipesCatalog.scalingUnit")}
          <select
            disabled={!catalog.scalingUnits.length}
            onChange={(event) => change("scalingUnitId", event.target.value)}
            required
            value={input.scalingUnitId}
          >
            {catalog.scalingUnits.map((unit) => (
              <option key={unit.id} value={unit.id}>
                {unit.name}
              </option>
            ))}
          </select>
        </label>
        <label>
          {t("recipesCatalog.baseScalingAmount")}
          <input
            inputMode="decimal"
            onChange={(event) =>
              change("baseScalingAmount", event.target.value)
            }
            pattern="(?:0|[1-9][0-9]*)(?:\.[0-9]+)?"
            required
            value={input.baseScalingAmount}
          />
        </label>
        <label className="recipe-create__description">
          {t("recipesCatalog.description")}
          <textarea
            onChange={(event) => change("description", event.target.value)}
            value={input.description}
          />
        </label>
      </div>
      {!catalog.scalingUnits.length ? (
        <p role="status">{t("recipesCatalog.noScalingUnits")}</p>
      ) : null}
      {error ? <p role="alert">{t(`recipesCatalog.errors.${error}`)}</p> : null}
      {saved ? <p role="status">{t("recipesCatalog.saved")}</p> : null}
      <button disabled={!catalog.scalingUnits.length} type="submit">
        {t("recipesCatalog.create")}
      </button>
    </form>
  );
}

function RecipeEditor({
  catalog,
  recipe,
  organizationId,
  userId,
}: {
  catalog: RecipeCatalogProjection;
  recipe: RecipeCatalogProjection["recipes"][number];
  organizationId: string;
  userId: string;
}) {
  const { t } = useTranslation();
  const activeIngredients = catalog.ingredients.filter(
    (ingredient) =>
      ingredient.retired !== true && ingredient.historical !== true,
  );
  const [open, setOpen] = useState(false);
  const [input, setInput] = useState<RecipeVersionInput>(() => ({
    recipeId: recipe.id,
    basedOnVersionId: recipe.versionId,
    name: recipe.name,
    description: recipe.description ?? "",
    scalingUnitId: recipe.scalingUnitId,
    baseScalingAmount: recipe.baseScalingAmount,
    ingredientLines: recipe.ingredientLines,
    recipeTagIds: recipe.recipeTagIds,
  }));
  const [error, setError] = useState<string>();
  const [saved, setSaved] = useState(false);
  useEffect(
    () =>
      setInput((current) => ({
        ...current,
        recipeId: recipe.id,
        basedOnVersionId: recipe.versionId,
      })),
    [recipe.id, recipe.versionId],
  );
  if (!open)
    return (
      <button onClick={() => setOpen(true)} type="button">
        {t("recipesCatalog.edit")}
      </button>
    );
  const change = (field: keyof RecipeVersionInput, value: string) =>
    setInput((current) => ({ ...current, [field]: value }));
  async function submit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setError(undefined);
    setSaved(false);
    try {
      await queueRecipeVersionPublish(userId, organizationId, input);
      setSaved(true);
      setOpen(false);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "unavailable");
    }
  }
  return (
    <form className="recipe-create" onSubmit={(event) => void submit(event)}>
      <h4>{t("recipesCatalog.editHeading")}</h4>
      <label>
        {t("recipesCatalog.name")}
        <input
          maxLength={200}
          onChange={(event) => change("name", event.target.value)}
          required
          value={input.name}
        />
      </label>
      <label>
        {t("recipesCatalog.scalingUnit")}
        <select
          onChange={(event) => change("scalingUnitId", event.target.value)}
          value={input.scalingUnitId}
        >
          {catalog.scalingUnits.map((unit) => (
            <option key={unit.id} value={unit.id}>
              {unit.name}
            </option>
          ))}
        </select>
      </label>
      <label>
        {t("recipesCatalog.baseScalingAmount")}
        <input
          inputMode="decimal"
          onChange={(event) => change("baseScalingAmount", event.target.value)}
          required
          value={input.baseScalingAmount}
        />
      </label>
      <label className="recipe-create__description">
        {t("recipesCatalog.description")}
        <textarea
          onChange={(event) => change("description", event.target.value)}
          value={input.description}
        />
      </label>
      <fieldset>
        <legend>{t("recipesCatalog.ingredients")}</legend>
        {input.ingredientLines.map((line, index) => (
          <div key={line.id}>
            <select
              aria-label={t("recipesCatalog.ingredient")}
              onChange={(event) =>
                setInput((current) => ({
                  ...current,
                  ingredientLines: current.ingredientLines.map(
                    (item, itemIndex) =>
                      itemIndex === index
                        ? { ...item, ingredientVersionId: event.target.value }
                        : item,
                  ),
                }))
              }
              value={line.ingredientVersionId}
            >
              {catalog.ingredients
                .filter(
                  (ingredient) =>
                    ingredient.retired !== true ||
                    ingredient.versionId === line.ingredientVersionId,
                )
                .map((ingredient) => (
                  <option
                    key={ingredient.versionId}
                    value={ingredient.versionId}
                  >
                    {ingredient.name}
                  </option>
                ))}
            </select>
            <input
              aria-label={t("recipesCatalog.quantity")}
              inputMode="decimal"
              onChange={(event) =>
                setInput((current) => ({
                  ...current,
                  ingredientLines: current.ingredientLines.map(
                    (item, itemIndex) =>
                      itemIndex === index
                        ? { ...item, baseQuantity: event.target.value }
                        : item,
                  ),
                }))
              }
              value={line.baseQuantity}
            />
            <button
              onClick={() =>
                setInput((current) => ({
                  ...current,
                  ingredientLines: current.ingredientLines.filter(
                    (_, itemIndex) => itemIndex !== index,
                  ),
                }))
              }
              type="button"
            >
              {t("recipesCatalog.removeLine")}
            </button>
          </div>
        ))}
      </fieldset>
      <button
        disabled={!activeIngredients.length}
        onClick={() =>
          setInput((current) => ({
            ...current,
            ingredientLines: [
              ...current.ingredientLines,
              {
                id: crypto.randomUUID(),
                ingredientVersionId: activeIngredients[0]?.versionId ?? "",
                baseQuantity: "0",
                scalingBehavior: "proportional",
                includeInPortionWeight: true,
                note: "",
              },
            ],
          }))
        }
        type="button"
      >
        {t("recipesCatalog.addLine")}
      </button>
      <fieldset>
        <legend>{t("recipesCatalog.tags")}</legend>
        {catalog.tags.map((tag) => (
          <label key={tag.id}>
            <input
              checked={input.recipeTagIds.includes(tag.id)}
              onChange={(event) =>
                setInput((current) => ({
                  ...current,
                  recipeTagIds: event.target.checked
                    ? [...current.recipeTagIds, tag.id]
                    : current.recipeTagIds.filter((id) => id !== tag.id),
                }))
              }
              type="checkbox"
            />
            {tag.name}
          </label>
        ))}
      </fieldset>
      {error ? <p role="alert">{t(`recipesCatalog.errors.${error}`)}</p> : null}
      {saved ? <p role="status">{t("recipesCatalog.saved")}</p> : null}
      <button type="submit">{t("recipesCatalog.publish")}</button>
      <button onClick={() => setOpen(false)} type="button">
        {t("recipesCatalog.cancel")}
      </button>
    </form>
  );
}

function RecipeLifecycleControl({
  recipe,
  organizationId,
  userId,
}: {
  recipe: RecipeCatalogProjection["recipes"][number];
  organizationId: string;
  userId: string;
}) {
  const { t } = useTranslation();
  const [error, setError] = useState(false);
  const [pending, setPending] = useState(false);
  const operation = recipe.retired ? "restore" : "retire";
  async function submit() {
    if (pending) return;
    setPending(true);
    setError(false);
    try {
      await queueRecipeLifecycle(userId, organizationId, {
        recipeId: recipe.id,
        operation,
      });
    } catch {
      setError(true);
    } finally {
      setPending(false);
    }
  }
  return (
    <>
      <button disabled={pending} onClick={() => void submit()} type="button">
        {t(`recipesCatalog.${operation}`)}
      </button>
      {error ? <p role="alert">{t("recipesCatalog.errors.unavailable")}</p> : null}
    </>
  );
}

export function RecipeCatalog({
  organizationId,
  userId,
  onUnauthenticated,
}: {
  organizationId: string;
  userId: string;
  onUnauthenticated: () => void;
}) {
  const { t } = useTranslation();
  const [state, setState] = useState<CatalogState>({ status: "loading" });
  const [showRetired, setShowRetired] = useState(false);

  useEffect(() => {
    const subscription = liveQuery(() =>
      readRecipeCatalog(userId, organizationId, true),
    ).subscribe({
      next: (catalog) =>
        setState((current) => ({
          status: current.status === "offline" ? "offline" : "ready",
          catalog,
        })),
      error: () => setState({ status: "error" }),
    });
    return () => subscription.unsubscribe();
  }, [organizationId, userId]);

  const refresh = useCallback(async () => {
    try {
      await pullOrganization(userId, organizationId);
    } catch (error) {
      if (error instanceof SyncRequestError && error.status === 401) {
        onUnauthenticated();
        return;
      }
      setState((current) =>
        current.status === "ready" || current.status === "offline"
          ? { ...current, status: "offline" }
          : { status: "error" },
      );
    }
  }, [onUnauthenticated, organizationId, userId]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  if (state.status === "loading")
    return (
      <p aria-live="polite" role="status">
        {t("recipesCatalog.loading")}
      </p>
    );
  if (state.status === "error")
    return (
      <div role="alert">
        <p>{t("recipesCatalog.error")}</p>
        <button onClick={() => void refresh()} type="button">
          {t("recipesCatalog.retry")}
        </button>
      </div>
    );
  const recipes = state.catalog.recipes.filter(
    (recipe) => showRetired || !recipe.retired,
  );
  return (
    <div className="recipe-catalog">
      <p className="recipe-catalog__scope">{t("recipesCatalog.scope")}</p>
      {state.status === "offline" ? (
        <p role="status">{t("recipesCatalog.offline")}</p>
      ) : null}
      <RecipeCreateForm
        catalog={state.catalog}
        organizationId={organizationId}
        userId={userId}
      />
      <label>
        <input
          checked={showRetired}
          onChange={(event) => setShowRetired(event.target.checked)}
          type="checkbox"
        />
        {t("recipesCatalog.showRetired")}
      </label>
      {!recipes.length ? (
        <p role="status">{t("recipesCatalog.empty")}</p>
      ) : (
        <ul className="recipe-list">
          {recipes.map((recipe) => (
            <li key={recipe.id}>
              <h3>{recipe.name}</h3>
              {recipe.retired ? <p>{t("recipesCatalog.retired")}</p> : null}
              {recipe.hasRetiredIngredientReference ? (
                <p role="alert">{t("recipesCatalog.retiredIngredientWarning")}</p>
              ) : null}
              <p>
                {t("recipesCatalog.scaling", {
                  amount: recipe.baseScalingAmount,
                })}
              </p>
              {recipe.description ? <p>{recipe.description}</p> : null}
              <RecipeEditor
                catalog={state.catalog}
                organizationId={organizationId}
                recipe={recipe}
                userId={userId}
              />
              <RecipeLifecycleControl
                organizationId={organizationId}
                recipe={recipe}
                userId={userId}
              />
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
