import { liveQuery } from "dexie";
import { useCallback, useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";

import {
  readIngredientCatalog,
  type IngredientCatalogProjection,
} from "./ingredient-catalog";
import {
  defaultMassForUnit,
  queueIngredientCreate,
  type IngredientCreateInput,
} from "./ingredient-create";
import { pullOrganization, SyncRequestError } from "./sync-bootstrap";

type CatalogState =
  | { status: "loading" }
  | { status: "ready" | "offline"; catalog: IngredientCatalogProjection }
  | { status: "error" };

const initialInput: IngredientCreateInput = {
  name: "",
  canonicalUnitId: "",
  massPerCanonicalQuantity: "1",
  dietaryTagIds: [],
};
const errors = new Set(["name", "unit", "mass", "tag"]);

function IngredientCreateForm({
  catalog,
  organizationId,
  userId,
}: {
  catalog: IngredientCatalogProjection;
  organizationId: string;
  userId: string;
}) {
  const { t } = useTranslation();
  const [input, setInput] = useState(initialInput);
  const [error, setError] = useState<string>();
  const [saved, setSaved] = useState(false);
  const submitting = useRef(false);
  const selected = catalog.units.find(
    (unit) => unit.id === input.canonicalUnitId,
  );

  useEffect(() => {
    const unit =
      catalog.units.find((item) => item.id === input.canonicalUnitId) ??
      catalog.units[0];
    setInput((current) => ({
      ...current,
      canonicalUnitId: unit?.id ?? "",
      massPerCanonicalQuantity:
        current.canonicalUnitId === unit?.id
          ? current.massPerCanonicalQuantity
          : defaultMassForUnit(unit),
    }));
  }, [catalog.units, input.canonicalUnitId]);

  function change(field: keyof IngredientCreateInput, value: string) {
    setInput((current) => ({ ...current, [field]: value }));
    setError(undefined);
    setSaved(false);
  }

  async function submit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (submitting.current) return;
    submitting.current = true;
    try {
      await queueIngredientCreate(userId, organizationId, input);
      setInput({
        ...initialInput,
        canonicalUnitId: catalog.units[0]?.id ?? "",
        massPerCanonicalQuantity: defaultMassForUnit(catalog.units[0]),
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
    <form
      className="ingredient-create"
      onSubmit={(event) => void submit(event)}
    >
      <h3>{t("ingredientsCatalog.createHeading")}</h3>
      <div className="ingredient-create__fields">
        <label>
          {t("ingredientsCatalog.name")}
          <input
            autoComplete="off"
            maxLength={200}
            onChange={(event) => change("name", event.target.value)}
            required
            value={input.name}
          />
        </label>
        <label>
          {t("ingredientsCatalog.canonicalUnit")}
          <select
            disabled={!catalog.units.length}
            onChange={(event) => {
              const unit = catalog.units.find(
                (item) => item.id === event.target.value,
              );
              change("canonicalUnitId", event.target.value);
              change("massPerCanonicalQuantity", defaultMassForUnit(unit));
            }}
            required
            value={input.canonicalUnitId}
          >
            {catalog.units.map((unit) => (
              <option key={unit.id} value={unit.id}>
                {unit.name}
              </option>
            ))}
          </select>
        </label>
        <label>
          {t("ingredientsCatalog.mass")}
          <input
            inputMode="decimal"
            onChange={(event) =>
              change("massPerCanonicalQuantity", event.target.value)
            }
            pattern="(?:0|[1-9][0-9]*)(?:\.[0-9]+)?"
            readOnly={selected?.dimension === "mass"}
            required
            value={input.massPerCanonicalQuantity}
          />
        </label>
      </div>
      {catalog.dietaryTags.length ? (
        <fieldset>
          <legend>{t("ingredientsCatalog.dietaryTags")}</legend>
          {catalog.dietaryTags.map((tag) => (
            <label key={tag.id}>
              <input
                checked={input.dietaryTagIds.includes(tag.id)}
                onChange={(event) =>
                  setInput((current) => ({
                    ...current,
                    dietaryTagIds: event.target.checked
                      ? [...current.dietaryTagIds, tag.id]
                      : current.dietaryTagIds.filter((id) => id !== tag.id),
                  }))
                }
                type="checkbox"
              />
              {tag.name}
            </label>
          ))}
        </fieldset>
      ) : null}
      {selected?.dimension !== "mass" ? (
        <p>{t("ingredientsCatalog.massHint")}</p>
      ) : null}
      {!catalog.units.length ? (
        <p role="status">{t("ingredientsCatalog.noUnits")}</p>
      ) : null}
      {error ? (
        <p role="alert">{t(`ingredientsCatalog.errors.${error}`)}</p>
      ) : null}
      {saved ? <p role="status">{t("ingredientsCatalog.saved")}</p> : null}
      <button disabled={!catalog.units.length} type="submit">
        {t("ingredientsCatalog.create")}
      </button>
    </form>
  );
}

export function IngredientCatalog({
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
      readIngredientCatalog(userId, organizationId),
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
      if (error instanceof SyncRequestError && error.status === 401)
        return onUnauthenticated();
      setState((current) =>
        current.status === "ready" || current.status === "offline"
          ? { ...current, status: "offline" }
          : { status: "error" },
      );
    }
  }, [onUnauthenticated, organizationId, userId]);
  useEffect(() => void refresh(), [refresh]);
  if (state.status === "loading")
    return (
      <p aria-live="polite" role="status">
        {t("ingredientsCatalog.loading")}
      </p>
    );
  if (state.status === "error")
    return (
      <div role="alert">
        <p>{t("ingredientsCatalog.error")}</p>
        <button onClick={() => void refresh()} type="button">
          {t("ingredientsCatalog.retry")}
        </button>
      </div>
    );
  return (
    <div className="ingredient-catalog">
      <p className="ingredient-catalog__scope">
        {t("ingredientsCatalog.scope")}
      </p>
      {state.status === "offline" ? (
        <p role="status">{t("ingredientsCatalog.offline")}</p>
      ) : null}
      <IngredientCreateForm
        catalog={state.catalog}
        organizationId={organizationId}
        userId={userId}
      />
      {!state.catalog.ingredients.length ? (
        <p role="status">{t("ingredientsCatalog.empty")}</p>
      ) : (
        <ul className="ingredient-list">
          {state.catalog.ingredients.map((ingredient) => (
            <li key={ingredient.id}>
              <h3>{ingredient.name}</h3>
              <p>
                {t("ingredientsCatalog.canonical", {
                  unit: ingredient.canonicalUnitName,
                  mass: ingredient.massPerCanonicalQuantity,
                })}
              </p>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
