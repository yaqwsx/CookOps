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
import { queueIngredientLifecycle } from "./ingredient-lifecycle";
import {
  queueIngredientPricePublish,
  type IngredientPricePublishInput,
} from "./ingredient-price-publish";
import {
  queueIngredientVersionPublish,
  type IngredientVersionPublishInput,
} from "./ingredient-version-publish";
import { pullOrganization, SyncRequestError } from "./sync-bootstrap";
import {
  matchesIngredient,
  normalizeIngredientSearch,
  rankIngredients,
} from "./ingredient-fuzzy";
import { IngredientCopyPanel } from "./ingredient-copy-panel";

type CatalogState =
  | { status: "loading" }
  | { status: "ready" | "offline"; catalog: IngredientCatalogProjection }
  | { status: "error" };

const initialInput: IngredientCreateInput = {
  name: "",
  canonicalUnitId: "",
  massPerCanonicalQuantity: "1",
  dietaryTagIds: [],
  defaultStoreSectionId: "",
};
const errors = new Set(["name", "unit", "mass", "tag", "storeSection"]);

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
  const storeSections = catalog.storeSections ?? [];
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
      defaultStoreSectionId:
        current.defaultStoreSectionId || storeSections[0]?.id || "",
    }));
  }, [catalog.units, input.canonicalUnitId, storeSections[0]?.id]);

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
          {t("ingredientsCatalog.defaultStoreSection")}
          <select
            required
            disabled={!storeSections.length}
            value={input.defaultStoreSectionId}
            onChange={(event) =>
              change("defaultStoreSectionId", event.target.value)
            }
          >
            {storeSections.map((section) => (
              <option key={section.id} value={section.id}>
                {section.name}
              </option>
            ))}
          </select>
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
      {!storeSections.length ? (
        <p role="status">{t("ingredientsCatalog.noStoreSections")}</p>
      ) : null}
      {error ? (
        <p role="alert">{t(`ingredientsCatalog.errors.${error}`)}</p>
      ) : null}
      {saved ? <p role="status">{t("ingredientsCatalog.saved")}</p> : null}
      <button
        disabled={!catalog.units.length || !storeSections.length}
        type="submit"
      >
        {t("ingredientsCatalog.create")}
      </button>
    </form>
  );
}

function IngredientLifecycleControl({
  ingredient,
  organizationId,
  userId,
}: {
  ingredient: IngredientCatalogProjection["ingredients"][number];
  organizationId: string;
  userId: string;
}) {
  const { t } = useTranslation();
  const [error, setError] = useState(false);
  const [pending, setPending] = useState(false);
  const operation = ingredient.retired ? "restore" : "retire";
  async function submit() {
    if (pending) return;
    setPending(true);
    setError(false);
    try {
      await queueIngredientLifecycle(userId, organizationId, {
        ingredientId: ingredient.id,
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
        {t(`ingredientsCatalog.${operation}`)}
      </button>
      {error ? (
        <p role="alert">{t("ingredientsCatalog.errors.unavailable")}</p>
      ) : null}
    </>
  );
}

function IngredientVersionEditor({
  ingredient,
  catalog,
  organizationId,
  userId,
  discardToken = 0,
  onDirtyChange,
}: {
  ingredient: IngredientCatalogProjection["ingredients"][number];
  catalog: IngredientCatalogProjection;
  organizationId: string;
  userId: string;
  discardToken?: number;
  onDirtyChange?: (dirty: boolean) => void;
}) {
  const { t } = useTranslation();
  const storeSections = catalog.storeSections ?? [];
  const current = ingredient.versions?.find(
    (version) => version.id === ingredient.versionId,
  );
  const [input, setInput] = useState<IngredientVersionPublishInput>({
    ingredientId: ingredient.id,
    basedOnVersionId: ingredient.versionId,
    name: ingredient.name,
    canonicalUnitId: ingredient.canonicalUnitId ?? "",
    massPerCanonicalQuantity: ingredient.massPerCanonicalQuantity,
    dietaryTagIds: ingredient.dietaryTagIds ?? [],
    defaultStoreSectionId:
      ingredient.defaultStoreSectionId ?? storeSections[0]?.id ?? null,
  });
  const [message, setMessage] = useState<string>();
  const initialInput = {
    ingredientId: ingredient.id,
    basedOnVersionId: ingredient.versionId,
    name: ingredient.name,
    canonicalUnitId: ingredient.canonicalUnitId ?? "",
    massPerCanonicalQuantity: ingredient.massPerCanonicalQuantity,
    dietaryTagIds: ingredient.dietaryTagIds ?? [],
    defaultStoreSectionId:
      ingredient.defaultStoreSectionId ?? storeSections[0]?.id ?? null,
  } satisfies IngredientVersionPublishInput;
  const previousDiscardToken = useRef(discardToken);
  const dirty = JSON.stringify(input) !== JSON.stringify(initialInput);
  useEffect(() => {
    onDirtyChange?.(dirty);
    return () => onDirtyChange?.(false);
  }, [dirty, onDirtyChange]);
  useEffect(() => {
    if (previousDiscardToken.current === discardToken) return;
    previousDiscardToken.current = discardToken;
    setInput(initialInput);
  }, [discardToken, initialInput]);
  if (ingredient.retired) return null;
  async function submit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    try {
      await queueIngredientVersionPublish(userId, organizationId, input);
      setMessage(t("ingredientsCatalog.versionQueued"));
    } catch {
      setMessage(t("ingredientsCatalog.errors.unavailable"));
    }
  }
  return (
    <details>
      <summary>{t("ingredientsCatalog.editVersion")}</summary>
      <p>
        {t("ingredientsCatalog.history")}: {ingredient.versions?.length ?? 1}
      </p>
      {ingredient.versions?.length ? (
        <ul aria-label={t("ingredientsCatalog.history")}>
          <li>
            {t("ingredientsCatalog.currentVersion")}: {ingredient.versionId}
          </li>
          {ingredient.versions
            .filter((version) => version.id !== ingredient.versionId)
            .map((version) => (
              <li key={version.id}>
                {version.id} · {version.name} · {version.canonicalUnitName} ·{" "}
                {version.mass}
                {version.basedOnVersionId
                  ? ` · ${t("ingredientsCatalog.basedOn")}: ${version.basedOnVersionId}`
                  : ""}
              </li>
            ))}
        </ul>
      ) : null}
      {current ? (
        <p>
          {current.name} · {current.canonicalUnitName} · {current.mass}
        </p>
      ) : null}
      <form onSubmit={(event) => void submit(event)}>
        <label>
          {t("ingredientsCatalog.name")}
          <input
            value={input.name}
            required
            onChange={(event) =>
              setInput({ ...input, name: event.target.value })
            }
          />
        </label>
        <label>
          {t("ingredientsCatalog.canonicalUnit")}
          <select
            value={input.canonicalUnitId}
            required
            onChange={(event) =>
              setInput({ ...input, canonicalUnitId: event.target.value })
            }
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
            value={input.massPerCanonicalQuantity}
            required
            onChange={(event) =>
              setInput({
                ...input,
                massPerCanonicalQuantity: event.target.value,
              })
            }
          />
        </label>
        <label>
          {t("ingredientsCatalog.defaultStoreSection")}
          <select
            required
            disabled={!catalog.storeSections.length}
            value={input.defaultStoreSectionId ?? ""}
            onChange={(event) =>
              setInput({ ...input, defaultStoreSectionId: event.target.value })
            }
          >
            {catalog.storeSections.map((section) => (
              <option key={section.id} value={section.id}>
                {section.name}
              </option>
            ))}
          </select>
        </label>
        <fieldset>
          <legend>{t("ingredientsCatalog.dietaryTags")}</legend>
          {catalog.dietaryTags.map((tag) => (
            <label key={tag.id}>
              <input
                type="checkbox"
                checked={input.dietaryTagIds.includes(tag.id)}
                onChange={(event) =>
                  setInput({
                    ...input,
                    dietaryTagIds: event.target.checked
                      ? [...input.dietaryTagIds, tag.id]
                      : input.dietaryTagIds.filter((id) => id !== tag.id),
                  })
                }
              />
              {tag.name}
            </label>
          ))}
        </fieldset>
        {!catalog.storeSections.length ? (
          <p role="status">{t("ingredientsCatalog.noStoreSections")}</p>
        ) : null}
        <button disabled={!catalog.storeSections.length} type="submit">
          {t("ingredientsCatalog.publishVersion")}
        </button>
      </form>
      {message ? <p role="status">{message}</p> : null}
    </details>
  );
}

function IngredientPriceEditor({
  ingredient,
  catalog,
  organizationId,
  userId,
  discardToken = 0,
  onDirtyChange,
}: {
  ingredient: IngredientCatalogProjection["ingredients"][number];
  catalog: IngredientCatalogProjection;
  organizationId: string;
  userId: string;
  discardToken?: number;
  onDirtyChange?: (dirty: boolean) => void;
}) {
  const { t } = useTranslation();
  const compatible = catalog.units.filter(
    (unit) =>
      unit.dimension ===
      catalog.units.find((item) => item.id === ingredient.canonicalUnitId)
        ?.dimension,
  );
  const [input, setInput] = useState<IngredientPricePublishInput>({
    ingredientId: ingredient.id,
    amount: ingredient.currentPrice?.amount ?? "",
    pricedQuantity: ingredient.currentPrice?.quantity ?? "1",
    unitId: ingredient.currentPrice?.unitId ?? compatible[0]?.id ?? "",
    currency: catalog.organizationDefaultCurrency,
  });
  const [message, setMessage] = useState<string>();
  const initialInput = {
    ingredientId: ingredient.id,
    amount: ingredient.currentPrice?.amount ?? "",
    pricedQuantity: ingredient.currentPrice?.quantity ?? "1",
    unitId: ingredient.currentPrice?.unitId ?? compatible[0]?.id ?? "",
    currency: catalog.organizationDefaultCurrency,
  } satisfies IngredientPricePublishInput;
  const previousDiscardToken = useRef(discardToken);
  const dirty = JSON.stringify(input) !== JSON.stringify(initialInput);
  useEffect(() => {
    onDirtyChange?.(dirty);
    return () => onDirtyChange?.(false);
  }, [dirty, onDirtyChange]);
  useEffect(() => {
    if (previousDiscardToken.current === discardToken) return;
    previousDiscardToken.current = discardToken;
    setInput(initialInput);
  }, [discardToken, initialInput]);
  if (ingredient.retired) return null;
  async function submit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    try {
      await queueIngredientPricePublish(userId, organizationId, input);
      setMessage(t("ingredientsCatalog.priceQueued"));
    } catch {
      setMessage(t("ingredientsCatalog.errors.unavailable"));
    }
  }
  return (
    <details>
      <summary>{t("ingredientsCatalog.priceHeading")}</summary>
      <p>
        {ingredient.currentPrice
          ? t("ingredientsCatalog.currentPrice", {
              amount: ingredient.currentPrice.amount,
              quantity: ingredient.currentPrice.quantity,
              unit:
                compatible.find(
                  (unit) => unit.id === ingredient.currentPrice?.unitId,
                )?.name ?? "—",
              currency: ingredient.currentPrice.currency,
            })
          : t("ingredientsCatalog.noPrice")}
      </p>
      <form onSubmit={(event) => void submit(event)}>
        <label>
          {t("ingredientsCatalog.priceAmount")}
          <input
            inputMode="decimal"
            pattern="(?:0|[1-9][0-9]*)(?:\.[0-9]+)?"
            required
            value={input.amount}
            onChange={(event) =>
              setInput({ ...input, amount: event.target.value })
            }
          />
        </label>
        <label>
          {t("ingredientsCatalog.priceQuantity")}
          <input
            inputMode="decimal"
            required
            value={input.pricedQuantity}
            onChange={(event) =>
              setInput({ ...input, pricedQuantity: event.target.value })
            }
          />
        </label>
        <label>
          {t("ingredientsCatalog.priceUnit")}
          <select
            required
            value={input.unitId}
            onChange={(event) =>
              setInput({ ...input, unitId: event.target.value })
            }
          >
            {compatible.map((unit) => (
              <option key={unit.id} value={unit.id}>
                {unit.name}
              </option>
            ))}
          </select>
        </label>
        <button disabled={!compatible.length} type="submit">
          {t("ingredientsCatalog.publishPrice")}
        </button>
      </form>
      {message ? <p role="status">{message}</p> : null}
    </details>
  );
}

export function IngredientCatalog({
  organizationId,
  userId,
  onUnauthenticated,
  onBackToCatalog,
  onOpenIngredient,
  selectedIngredientId,
  discardToken,
  onDirtyChange,
}: {
  organizationId: string;
  userId: string;
  onUnauthenticated: () => void;
  onBackToCatalog?: () => void;
  onOpenIngredient?: (ingredientId: string) => void;
  selectedIngredientId?: string;
  discardToken?: number;
  onDirtyChange?: (dirty: boolean) => void;
}) {
  const { t } = useTranslation();
  const [state, setState] = useState<CatalogState>({ status: "loading" });
  const [showRetired, setShowRetired] = useState(false);
  const [query, setQuery] = useState("");
  useEffect(() => {
    const subscription = liveQuery(() =>
      readIngredientCatalog(userId, organizationId, true),
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
  const ingredients = state.catalog.ingredients.filter(
    (ingredient) => showRetired || !ingredient.retired,
  );
  const normalizedQuery = normalizeIngredientSearch(query);
  const filteredIngredients = normalizedQuery
    ? rankIngredients(ingredients, query, showRetired).filter((ingredient) =>
        matchesIngredient(ingredient.name, query),
      )
    : ingredients;
  if (selectedIngredientId === "__invalid__")
    return (
      <div className="ingredient-catalog">
        {onBackToCatalog ? (
          <a
            href={`/organizations/${organizationId}/ingredients`}
            onClick={(event) => {
              event.preventDefault();
              onBackToCatalog();
            }}
          >
            {t("ingredientsCatalog.backToCatalog")}
          </a>
        ) : null}
        <p role="status">{t("ingredientsCatalog.unavailable")}</p>
      </div>
    );
  if (selectedIngredientId) {
    const selected = state.catalog.ingredients.find(
      (ingredient) => ingredient.id.toLowerCase() === selectedIngredientId,
    );
    return (
      <div className="ingredient-catalog">
        {onBackToCatalog ? (
          <a
            href={`/organizations/${organizationId}/ingredients`}
            onClick={(event) => {
              event.preventDefault();
              onBackToCatalog();
            }}
          >
            {t("ingredientsCatalog.backToCatalog")}
          </a>
        ) : null}
        {!selected ? (
          <p role="status">{t("ingredientsCatalog.unavailable")}</p>
        ) : (
          <IngredientDetail
            catalog={state.catalog}
            ingredient={selected}
            organizationId={organizationId}
            userId={userId}
            discardToken={discardToken}
            onDirtyChange={onDirtyChange}
            onUnauthenticated={onUnauthenticated}
          />
        )}
      </div>
    );
  }
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
      <label>
        <input
          checked={showRetired}
          onChange={(event) => setShowRetired(event.target.checked)}
          type="checkbox"
        />
        {t("ingredientsCatalog.showRetired")}
      </label>
      <label>
        {t("ingredientsCatalog.search")}
        <input
          aria-label={t("ingredientsCatalog.search")}
          onChange={(event) => setQuery(event.target.value)}
          placeholder={t("ingredientsCatalog.searchPlaceholder")}
          type="search"
          value={query}
        />
      </label>
      {query ? (
        <button onClick={() => setQuery("")} type="button">
          {t("ingredientsCatalog.clearSearch")}
        </button>
      ) : null}
      {!filteredIngredients.length ? (
        <p role="status">
          {ingredients.length
            ? t("ingredientsCatalog.searchEmpty")
            : t("ingredientsCatalog.empty")}
        </p>
      ) : (
        <ul className="ingredient-list">
          {filteredIngredients.map((ingredient) => (
            <li key={ingredient.id}>
              <h3>
                {onOpenIngredient ? (
                  <a
                    href={`/organizations/${organizationId}/ingredients/${ingredient.id}`}
                    onClick={(event) => {
                      event.preventDefault();
                      onOpenIngredient(ingredient.id);
                    }}
                  >
                    {ingredient.name}
                  </a>
                ) : (
                  ingredient.name
                )}
              </h3>
              {ingredient.retired ? (
                <p>{t("ingredientsCatalog.retired")}</p>
              ) : null}
              <p>
                {t("ingredientsCatalog.canonical", {
                  unit: ingredient.canonicalUnitName,
                  mass: ingredient.massPerCanonicalQuantity,
                })}
              </p>
              <IngredientLifecycleControl
                ingredient={ingredient}
                organizationId={organizationId}
                userId={userId}
              />
              <IngredientVersionEditor
                ingredient={ingredient}
                catalog={state.catalog}
                organizationId={organizationId}
                userId={userId}
              />
              <IngredientPriceEditor
                ingredient={ingredient}
                catalog={state.catalog}
                organizationId={organizationId}
                userId={userId}
              />
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function IngredientDetail({
  catalog,
  ingredient,
  organizationId,
  userId,
  discardToken,
  onDirtyChange,
  onUnauthenticated,
}: {
  catalog: IngredientCatalogProjection;
  ingredient: IngredientCatalogProjection["ingredients"][number];
  organizationId: string;
  userId: string;
  discardToken?: number;
  onDirtyChange?: (dirty: boolean) => void;
  onUnauthenticated: () => void;
}) {
  const { t } = useTranslation();
  const [dirtyEditors, setDirtyEditors] = useState<Set<string>>(
    () => new Set(),
  );
  const reportDirty = useCallback((key: string, dirty: boolean) => {
    setDirtyEditors((current) => {
      const next = new Set(current);
      if (dirty) next.add(key);
      else next.delete(key);
      return next;
    });
  }, []);
  const reportVersionDirty = useCallback(
    (dirty: boolean) => reportDirty("version", dirty),
    [reportDirty],
  );
  const reportPriceDirty = useCallback(
    (dirty: boolean) => reportDirty("price", dirty),
    [reportDirty],
  );
  useEffect(() => {
    onDirtyChange?.(dirtyEditors.size > 0);
    return () => onDirtyChange?.(false);
  }, [dirtyEditors, onDirtyChange]);
  return (
    <article aria-labelledby="ingredient-detail-heading">
      <h2 id="ingredient-detail-heading">{ingredient.name}</h2>
      {ingredient.retired ? <p>{t("ingredientsCatalog.retired")}</p> : null}
      <p>
        {t("ingredientsCatalog.canonical", {
          unit: ingredient.canonicalUnitName,
          mass: ingredient.massPerCanonicalQuantity,
        })}
      </p>
      <IngredientLifecycleControl
        ingredient={ingredient}
        organizationId={organizationId}
        userId={userId}
      />
      <IngredientVersionEditor
        catalog={catalog}
        ingredient={ingredient}
        organizationId={organizationId}
        userId={userId}
        discardToken={discardToken}
        onDirtyChange={reportVersionDirty}
      />
      <IngredientPriceEditor
        catalog={catalog}
        ingredient={ingredient}
        organizationId={organizationId}
        userId={userId}
        discardToken={discardToken}
        onDirtyChange={reportPriceDirty}
      />
      {ingredient.retired ? (
        <p>{t("ingredientsCatalog.copyRetired")}</p>
      ) : (
        <IngredientCopyPanel
          ingredient={ingredient}
          onUnauthenticated={onUnauthenticated}
          organizationId={organizationId}
          userId={userId}
        />
      )}
    </article>
  );
}
