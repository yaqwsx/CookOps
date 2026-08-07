import { liveQuery } from "dexie";
import { useCallback, useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";

import {
  readRecipeCatalog,
  type RecipeCatalogProjection,
} from "./recipe-catalog";
import { queueRecipeCreate, type RecipeCreateInput } from "./recipe-create";
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

  useEffect(() => {
    const subscription = liveQuery(() =>
      readRecipeCatalog(userId, organizationId),
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
      {!state.catalog.recipes.length ? (
        <p role="status">{t("recipesCatalog.empty")}</p>
      ) : (
        <ul className="recipe-list">
          {state.catalog.recipes.map((recipe) => (
            <li key={recipe.id}>
              <h3>{recipe.name}</h3>
              <p>
                {t("recipesCatalog.scaling", {
                  amount: recipe.baseScalingAmount,
                })}
              </p>
              {recipe.description ? <p>{recipe.description}</p> : null}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
