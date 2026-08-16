import { liveQuery } from "dexie";
import { useCallback, useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";

import {
  readRecipeCatalog,
  projectRecipeCatalogUpdate,
  type RecipeCatalogProjection,
} from "./recipe-catalog";
import { queueRecipeCreate, type RecipeCreateInput } from "./recipe-create";
import {
  queueRecipeVersionPublish,
  type RecipeVersionInput,
} from "./recipe-publish";
import { queueRecipeLifecycle } from "./recipe-lifecycle";
import { pullOrganization, SyncRequestError } from "./sync-bootstrap";
import { defaultMassForUnit, queueIngredientCreateWithVersion, type IngredientCreateInput } from "./ingredient-create";
import { rankIngredients } from "./ingredient-fuzzy";

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

function IngredientCombobox({
  catalog,
  organizationId,
  userId,
  selectedVersionId,
  onSelect,
}: {
  catalog: RecipeCatalogProjection;
  organizationId: string;
  userId: string;
  selectedVersionId: string;
  onSelect: (versionId: string) => void;
}) {
  const { t } = useTranslation();
  const selected = catalog.ingredients.find((ingredient) => ingredient.versionId === selectedVersionId);
  const [query, setQuery] = useState(selected?.name ?? "");
  const [committedVersionId, setCommittedVersionId] = useState(selectedVersionId);
  const [committedLabel, setCommittedLabel] = useState(selected?.name ?? "");
  const [open, setOpen] = useState(false);
  const [activeIndex, setActiveIndex] = useState(-1);
  const [creating, setCreating] = useState(false);
  const [createInput, setCreateInput] = useState<IngredientCreateInput>({
    name: "",
    canonicalUnitId: catalog.units[0]?.id ?? "",
    massPerCanonicalQuantity: defaultMassForUnit(catalog.units[0]),
    dietaryTagIds: [],
  });
  const [createError, setCreateError] = useState<string>();
  const [createPending, setCreatePending] = useState(false);
  const listId = `ingredient-options-${selectedVersionId || "new"}`;
  const results = rankIngredients(catalog.ingredients, query).slice(0, 12);
  const select = (ingredient: { versionId: string; name: string }) => {
    onSelect(ingredient.versionId);
    setCommittedVersionId(ingredient.versionId);
    setCommittedLabel(ingredient.name);
    setQuery(ingredient.name);
    setOpen(false);
    setActiveIndex(0);
  };
  async function createIngredient() {
    if (createPending) return;
    setCreatePending(true);
    setCreateError(undefined);
    try {
      const created = await queueIngredientCreateWithVersion(userId, organizationId, createInput);
      select({ versionId: created.ingredientVersionId, name: createInput.name.normalize("NFC").trim() });
      setCreating(false);
      setCreateInput({ ...createInput, name: "" });
    } catch (reason) {
      setCreateError(reason instanceof Error ? reason.message : "unavailable");
    } finally {
      setCreatePending(false);
    }
  }
  function keyDown(event: React.KeyboardEvent<HTMLInputElement>) {
    if (event.key === "ArrowDown") {
      event.preventDefault();
      setOpen(true);
      setActiveIndex((index) => Math.min(index + 1, Math.max(results.length - 1, 0)));
    } else if (event.key === "ArrowUp") {
      event.preventDefault();
      setActiveIndex((index) => index < 0 ? Math.max(results.length - 1, 0) : Math.max(index - 1, 0));
    } else if (event.key === "Enter" && open && activeIndex >= 0 && results[activeIndex]) {
      event.preventDefault();
      select(results[activeIndex]);
    } else if (event.key === "Escape") {
      setOpen(false);
      setQuery(committedLabel);
      onSelect(committedVersionId);
    }
  }
  return (
    <div className="ingredient-combobox">
      <input
        aria-activedescendant={open && results[activeIndex] ? `${listId}-${results[activeIndex].versionId}` : undefined}
        aria-controls={listId}
        aria-expanded={open}
        aria-label={t("recipesCatalog.ingredient")}
        aria-haspopup="listbox"
        autoComplete="off"
        onChange={(event) => {
          setQuery(event.target.value);
          if (committedVersionId) onSelect("");
          setCommittedVersionId("");
          setOpen(true);
          setActiveIndex(-1);
        }}
        onFocus={() => { setOpen(true); setActiveIndex(-1); }}
        onKeyDown={keyDown}
        role="combobox"
        value={query}
      />
      {open ? (
        <div id={listId} role="listbox">
          {results.map((ingredient, index) => (
            /* biome-ignore lint/a11y/useFocusableInteractive: the input owns keyboard focus through aria-activedescendant */
            /* biome-ignore lint/a11y/useKeyWithClickEvents: keyboard selection is handled by the owning combobox input */
            <div
              aria-selected={index === activeIndex}
              id={`${listId}-${ingredient.versionId}`}
              key={ingredient.versionId}
              onClick={() => select(ingredient)}
              onMouseDown={(event) => event.preventDefault()}
              role="option"
            >
              {ingredient.name} · {ingredient.canonicalUnitName}
            </div>
          ))}
          {!results.length ? <div role="status">{t("recipesCatalog.ingredientSearchEmpty")}</div> : null}
        </div>
      ) : null}
      {open ? <button onClick={() => setCreating(true)} type="button">{t("recipesCatalog.createIngredient")}</button> : null}
      {creating ? (
        <fieldset>
          <legend>{t("recipesCatalog.createIngredient")}</legend>
          <label>{t("recipesCatalog.newIngredientName")}<input required value={createInput.name} onChange={(event) => setCreateInput({ ...createInput, name: event.target.value })} /></label>
          <label>{t("recipesCatalog.ingredientUnit")}<select required value={createInput.canonicalUnitId} onChange={(event) => { const unit = catalog.units.find((item) => item.id === event.target.value); setCreateInput({ ...createInput, canonicalUnitId: event.target.value, massPerCanonicalQuantity: defaultMassForUnit(unit) }); }}>{catalog.units.map((unit) => <option key={unit.id} value={unit.id}>{unit.name}</option>)}</select></label>
          <label>{t("recipesCatalog.ingredientMass")}<input inputMode="decimal" required value={createInput.massPerCanonicalQuantity} onChange={(event) => setCreateInput({ ...createInput, massPerCanonicalQuantity: event.target.value })} /></label>
          {createError ? <p role="alert">{t(`recipesCatalog.errors.${createError}`, { defaultValue: t("recipesCatalog.errors.unavailable") })}</p> : null}
          <button disabled={createPending || !catalog.units.length} onClick={() => void createIngredient()} type="button">{t("recipesCatalog.saveIngredient")}</button>
          <button onClick={() => setCreating(false)} type="button">{t("recipesCatalog.cancel")}</button>
        </fieldset>
      ) : null}
    </div>
  );
}

function RecipeEditor({
  catalog,
  recipe,
  organizationId,
  userId,
  initiallyOpen = false,
  onDirtyChange,
  discardToken = 0,
}: {
  catalog: RecipeCatalogProjection;
  recipe: RecipeCatalogProjection["recipes"][number];
  organizationId: string;
  userId: string;
  initiallyOpen?: boolean;
  onDirtyChange?: (recipeId: string, dirty: boolean) => void;
  discardToken?: number;
}) {
  const { t } = useTranslation();
  const activeIngredients = catalog.ingredients.filter(
    (ingredient) =>
      ingredient.retired !== true && ingredient.historical !== true,
  );
  const [open, setOpen] = useState(initiallyOpen);
  const [input, setInput] = useState<RecipeVersionInput>(() => ({
    recipeId: recipe.id,
    basedOnVersionId: recipe.versionId,
    name: recipe.name,
    description: recipe.description ?? "",
    scalingUnitId: recipe.scalingUnitId,
    baseScalingAmount: recipe.baseScalingAmount,
    ingredientLines: recipe.ingredientLines,
    recipeTagIds: recipe.recipeTagIds,
    estimatedDinersPerScalingUnit: recipe.estimatedDinersPerScalingUnit,
    roundSuggestionsUp: recipe.roundSuggestionsUp,
  }));
  const [error, setError] = useState<string>();
  const [saved, setSaved] = useState(false);
  const previousDiscardToken = useRef(discardToken);
  const initialInput = {
    recipeId: recipe.id,
    basedOnVersionId: recipe.versionId,
    name: recipe.name,
    description: recipe.description ?? "",
    scalingUnitId: recipe.scalingUnitId,
    baseScalingAmount: recipe.baseScalingAmount,
    ingredientLines: recipe.ingredientLines,
    recipeTagIds: recipe.recipeTagIds,
    estimatedDinersPerScalingUnit: recipe.estimatedDinersPerScalingUnit,
    roundSuggestionsUp: recipe.roundSuggestionsUp,
  } satisfies RecipeVersionInput;
  const dirty = JSON.stringify(input) !== JSON.stringify(initialInput);
  useEffect(
    () =>
      setInput((current) => ({
        ...current,
        recipeId: recipe.id,
        basedOnVersionId: recipe.versionId,
      })),
    [recipe.id, recipe.versionId],
  );
  useEffect(() => {
    setOpen(initiallyOpen);
  }, [initiallyOpen]);
  useEffect(() => {
    if (previousDiscardToken.current === discardToken) return;
    previousDiscardToken.current = discardToken;
    setInput(initialInput);
    setOpen(false);
  }, [discardToken, initialInput]);
  useEffect(() => {
    onDirtyChange?.(recipe.id, open && dirty);
    return () => onDirtyChange?.(recipe.id, false);
  }, [dirty, onDirtyChange, open, recipe.id]);
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
            <IngredientCombobox
              catalog={catalog}
              organizationId={organizationId}
              selectedVersionId={line.ingredientVersionId}
              userId={userId}
              onSelect={(ingredientVersionId) => setInput((current) => ({
                ...current,
                ingredientLines: current.ingredientLines.map((item, itemIndex) => itemIndex === index ? { ...item, ingredientVersionId } : item),
              }))}
            />
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
      <button
        onClick={() => {
          if (dirty && !window.confirm(t("recipesCatalog.discardChanges"))) return;
          setInput(initialInput);
          setOpen(false);
        }}
        type="button"
      >
        {t("recipesCatalog.cancel")}
      </button>
    </form>
  );
}

function RecipeCatalogUpdate({
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
  const preview = projectRecipeCatalogUpdate(
    recipe,
    catalog.ingredients,
    catalog.units,
  );
  const [pending, setPending] = useState(false);
  const [saved, setSaved] = useState(false);
  const [error, setError] = useState(false);
  if (!recipe.catalogUpdateAvailable || !preview.lines.length) return null;
  async function confirmUpdate() {
    if (pending || preview.blocked || !window.confirm(t("recipesCatalog.catalogUpdateConfirm"))) return;
    const updates = new Map(preview.lines.map((line) => [line.lineId, line]));
    const ingredientLines = recipe.ingredientLines.map((line) => {
      const update = updates.get(line.id);
      return update?.compatible && update.newIngredient && update.newQuantity
        ? { ...line, ingredientVersionId: update.newIngredient.versionId, baseQuantity: update.newQuantity }
        : line;
    });
    setPending(true);
    setError(false);
    try {
      await queueRecipeVersionPublish(userId, organizationId, {
        recipeId: recipe.id,
        basedOnVersionId: recipe.versionId,
        name: recipe.name,
        description: recipe.description ?? "",
        scalingUnitId: recipe.scalingUnitId,
        baseScalingAmount: recipe.baseScalingAmount,
        ingredientLines,
        recipeTagIds: recipe.recipeTagIds,
        estimatedDinersPerScalingUnit: recipe.estimatedDinersPerScalingUnit,
        roundSuggestionsUp: recipe.roundSuggestionsUp,
        catalogUpdate: true,
        expectedCurrentIngredientVersions: [
          ...new Map(
            preview.lines
              .filter((line) => line.compatible && line.newIngredient)
              .map((line) => [
                line.newIngredient?.id ?? "",
                { ingredientId: line.newIngredient?.id ?? "", versionId: line.newIngredient?.versionId ?? "" },
              ] as const),
          ).values(),
        ],
      });
      setSaved(true);
    } catch {
      setError(true);
    } finally {
      setPending(false);
    }
  }
  return (
    <details>
      <summary>{t("recipesCatalog.catalogUpdatePreview")}</summary>
      <ul>
        {preview.lines.map((line) => (
          <li key={line.lineId}>
            <span>{line.oldIngredient?.name ?? t("recipesCatalog.catalogUpdateMissing")}</span>
            {" → "}
            <span>{line.newIngredient?.name ?? t("recipesCatalog.catalogUpdateMissing")}</span>
            {": "}{line.oldQuantity}{" → "}{line.newQuantity ?? t("recipesCatalog.catalogUpdateBlocked")}
            {" "}{line.oldUnitName}{" → "}{line.newUnitName}
            {!line.compatible ? <strong> ({t("recipesCatalog.catalogUpdateBlocked")})</strong> : null}
          </li>
        ))}
      </ul>
      {preview.blocked ? <p role="alert">{t("recipesCatalog.catalogUpdateBlockedHelp")}</p> : null}
      {error ? <p role="alert">{t("recipesCatalog.errors.unavailable")}</p> : null}
      {saved ? <p role="status">{t("recipesCatalog.catalogUpdateSaved")}</p> : null}
      <button disabled={pending || preview.blocked} onClick={() => void confirmUpdate()} type="button">
        {t("recipesCatalog.catalogUpdateConfirm")}
      </button>
    </details>
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
  onBackToCatalog,
  selectedRecipeId,
  editRecipeId,
  onDirtyChange,
  discardToken,
}: {
  organizationId: string;
  userId: string;
  onUnauthenticated: () => void;
  onBackToCatalog?: () => void;
  selectedRecipeId?: string;
  editRecipeId?: string;
  onDirtyChange?: (dirty: boolean) => void;
  discardToken?: number;
}) {
  const { t } = useTranslation();
  const [state, setState] = useState<CatalogState>({ status: "loading" });
  const [showRetired, setShowRetired] = useState(false);
  const [query, setQuery] = useState("");
  const [dirtyRecipeIds, setDirtyRecipeIds] = useState<Set<string>>(
    () => new Set(),
  );
  const reportRecipeDirty = useCallback((recipeId: string, dirty: boolean) => {
    setDirtyRecipeIds((current) => {
      const next = new Set(current);
      if (dirty) next.add(recipeId);
      else next.delete(recipeId);
      return next;
    });
  }, []);
  useEffect(() => {
    onDirtyChange?.(dirtyRecipeIds.size > 0);
  }, [dirtyRecipeIds, onDirtyChange]);

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
  const visibleRecipes = state.catalog.recipes.filter(
    (recipe) =>
      (showRetired || !recipe.retired) || recipe.id === selectedRecipeId,
  );
  const normalizedQuery = query.normalize("NFC").trim().toLocaleLowerCase();
  const tagNameById = new Map(
    state.catalog.tags.map((tag) => [tag.id, tag.name]),
  );
  const ingredientNameByVersion = new Map(
    state.catalog.ingredients.map((ingredient) => [
      ingredient.versionId,
      ingredient.name,
    ]),
  );
  const recipes = visibleRecipes.filter((recipe) => {
    if (selectedRecipeId && recipe.id !== selectedRecipeId) return false;
    if (!normalizedQuery) return true;
    const tagNames = recipe.recipeTagIds
      .map((id) => tagNameById.get(id))
      .filter((name): name is string => Boolean(name));
    const ingredientNames = recipe.ingredientLines
      .map((line) => ingredientNameByVersion.get(line.ingredientVersionId))
      .filter((name): name is string => Boolean(name));
    return [
      recipe.name,
      recipe.description ?? "",
      ...tagNames,
      ...ingredientNames,
    ]
      .join("\n")
      .normalize("NFC")
      .toLocaleLowerCase()
      .includes(normalizedQuery);
  });
  return (
    <div className="recipe-catalog">
      {onBackToCatalog ? (
        <a
          href={`/organizations/${organizationId}/recipes`}
          onClick={(event) => {
            event.preventDefault();
            onBackToCatalog();
          }}
        >
          {t("recipesCatalog.backToCatalog")}
        </a>
      ) : null}
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
      <label>
        {t("recipesCatalog.search")}
        <input
          aria-label={t("recipesCatalog.search")}
          onChange={(event) => setQuery(event.target.value)}
          placeholder={t("recipesCatalog.searchPlaceholder")}
          type="search"
          value={query}
        />
      </label>
      {query ? (
        <button onClick={() => setQuery("")} type="button">
          {t("recipesCatalog.clearSearch")}
        </button>
      ) : null}
      {selectedRecipeId && !state.catalog.recipes.some((recipe) => recipe.id === selectedRecipeId) ? (
        <p role="status">{t("recipesCatalog.unavailable")}</p>
      ) : !recipes.length ? (
        <p role="status">
          {visibleRecipes.length
            ? t("recipesCatalog.searchEmpty")
            : t("recipesCatalog.empty")}
        </p>
      ) : (
        <ul className="recipe-list">
          {recipes.map((recipe) => (
            <li key={recipe.id}>
              <h3>{recipe.name}</h3>
              {recipe.retired ? <p>{t("recipesCatalog.retired")}</p> : null}
              {recipe.hasRetiredIngredientReference ? (
                <p role="alert">{t("recipesCatalog.retiredIngredientWarning")}</p>
              ) : null}
              {recipe.catalogUpdateAvailable ? (
                <p role="status">{t("recipesCatalog.catalogUpdateAvailable")}</p>
              ) : null}
              <RecipeCatalogUpdate
                catalog={state.catalog}
                organizationId={organizationId}
                recipe={recipe}
                userId={userId}
              />
              <p>
                {t("recipesCatalog.scaling", {
                  amount: recipe.baseScalingAmount,
                })}
              </p>
              {state.catalog.costs[recipe.id]?.total ? (
                <p role="status">
                  {t("recipesCatalog.estimatedCost", {
                    amount: new Intl.NumberFormat(undefined, {
                      style: "currency",
                      currency: state.catalog.costs[recipe.id].currency,
                    }).format(Number(state.catalog.costs[recipe.id].total)),
                  })}
                </p>
              ) : (
                <p role="status">
                  {t("recipesCatalog.incompleteCost", {
                    count: state.catalog.costs[recipe.id]?.missingCount ?? 0,
                  })}
                </p>
              )}
              {recipe.description ? <p>{recipe.description}</p> : null}
              <RecipeEditor
                catalog={state.catalog}
                organizationId={organizationId}
                recipe={recipe}
                initiallyOpen={recipe.id === editRecipeId}
                discardToken={discardToken}
                onDirtyChange={reportRecipeDirty}
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
