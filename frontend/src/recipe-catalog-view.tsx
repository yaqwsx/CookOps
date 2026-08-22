import { liveQuery } from "dexie";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { defaultValueCtx, Editor, rootCtx } from "@milkdown/kit/core";
import { editorViewCtx, parserCtx, serializerCtx } from "@milkdown/kit/core";
import { commonmark } from "@milkdown/kit/preset/commonmark";
import { Milkdown, MilkdownProvider, useEditor, useInstance } from "@milkdown/react";
import { listener, listenerCtx } from "@milkdown/plugin-listener";

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
import { queueCatalogConfiguration } from "./catalog-configuration";
import { pullOrganization, SyncRequestError } from "./sync-bootstrap";
import { defaultMassForUnit, queueIngredientCreateWithVersion, type IngredientCreateInput } from "./ingredient-create";
import { matchesIngredient, rankIngredients } from "./ingredient-fuzzy";
import { RecipeCopyPanel } from "./recipe-copy-panel";

type CatalogState =
  | { status: "loading" }
  | { status: "ready" | "offline"; catalog: RecipeCatalogProjection }
  | { status: "error" };

const initialInput: RecipeCreateInput = {
  name: "",
  description: "",
  scalingUnitId: "",
  baseScalingAmount: "1",
  recipeTagIds: [],
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
      <RecipeTagPicker catalog={catalog} organizationId={organizationId} userId={userId} selected={input.recipeTagIds ?? []} onChange={(recipeTagIds) => setInput((current) => ({ ...current, recipeTagIds }))} />
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

function RecipeTagPicker({ catalog, organizationId, userId, selected, onChange }: { catalog: RecipeCatalogProjection; organizationId: string; userId: string; selected: string[]; onChange: (ids: string[]) => void }) {
  const { t } = useTranslation();
  const [creating, setCreating] = useState(false);
  const [name, setName] = useState("");
  const [color, setColor] = useState("#336699");
  const [error, setError] = useState(false);
  const [createdTags, setCreatedTags] = useState<{ id: string; name: string }[]>([]);
  const tags = [
    ...catalog.tags.filter((tag) => !tag.retired || selected.includes(tag.id)),
    ...createdTags.filter((tag) => !catalog.tags.some((item) => item.id === tag.id)),
  ];
  async function create() {
    const canonical = name.normalize("NFC").trim();
    const activeNames = [...catalog.tags.filter((tag) => !tag.retired).map((tag) => tag.name), ...createdTags.map((tag) => tag.name)].map((tag) => tag.normalize("NFC").trim().toLocaleLowerCase());
    if (!canonical || canonical.length > 200 || !/^#[0-9A-Fa-f]{6}$/.test(color) || activeNames.includes(canonical.toLocaleLowerCase())) {
      setError(true);
      return;
    }
    try {
      const id = await queueCatalogConfiguration(userId, organizationId, "recipe_tag", "create", { name: canonical, color });
      if (id) {
        setCreatedTags((current) => [...current, { id, name: canonical }]);
        onChange([...new Set([...selected, id])]);
      }
    } catch {
      setError(true);
      return;
    }
    setName("");
    setCreating(false);
    setError(false);
  }
  return (
    <fieldset>
      <legend>{t("recipesCatalog.tags")}</legend>
      {tags.map((tag) => (
        <label key={tag.id}>
          <input checked={selected.includes(tag.id)} onChange={(event) => onChange(event.target.checked ? [...selected, tag.id] : selected.filter((id) => id !== tag.id))} type="checkbox" />
          {tag.name}
        </label>
      ))}
      {creating ? (
        <div>
          <label>{t("recipesCatalog.newTagName")}<input autoComplete="off" maxLength={200} required value={name} onChange={(event) => setName(event.target.value)} /></label>
          <label>{t("recipesCatalog.tagColor")}<input type="color" value={color} onChange={(event) => setColor(event.target.value)} /></label>
          {error ? <p role="alert">{t("recipesCatalog.tagCreateError")}</p> : null}
          <button onClick={() => void create()} type="button">{t("recipesCatalog.saveTag")}</button>
        </div>
      ) : <button onClick={() => setCreating(true)} type="button">{t("recipesCatalog.createTag")}</button>}
    </fieldset>
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
    defaultStoreSectionId: catalog.storeSections[0]?.id ?? "",
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
  function openCreate() {
    const unit = catalog.units[0];
    setCreateInput({
      ...createInput,
      name: "",
      canonicalUnitId: unit?.id ?? "",
      massPerCanonicalQuantity: defaultMassForUnit(unit),
      defaultStoreSectionId: catalog.storeSections[0]?.id ?? "",
    });
    setCreating(true);
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
      {open ? <button onClick={openCreate} type="button">{t("recipesCatalog.createIngredient")}</button> : null}
      {creating ? (
        <fieldset>
          <legend>{t("recipesCatalog.createIngredient")}</legend>
          <label>{t("recipesCatalog.newIngredientName")}<input required value={createInput.name} onChange={(event) => setCreateInput({ ...createInput, name: event.target.value })} /></label>
          <label>{t("recipesCatalog.ingredientUnit")}<select required value={createInput.canonicalUnitId} onChange={(event) => { const unit = catalog.units.find((item) => item.id === event.target.value); setCreateInput({ ...createInput, canonicalUnitId: event.target.value, massPerCanonicalQuantity: defaultMassForUnit(unit) }); }}>{catalog.units.map((unit) => <option key={unit.id} value={unit.id}>{unit.name}</option>)}</select></label>
          <label>{t("recipesCatalog.ingredientMass")}<input inputMode="decimal" required value={createInput.massPerCanonicalQuantity} onChange={(event) => setCreateInput({ ...createInput, massPerCanonicalQuantity: event.target.value })} /></label>
          <label>{t("ingredientsCatalog.defaultStoreSection")}<select required disabled={!catalog.storeSections.length} value={createInput.defaultStoreSectionId ?? ""} onChange={(event) => setCreateInput({ ...createInput, defaultStoreSectionId: event.target.value })}>{catalog.storeSections.map((section) => <option key={section.id} value={section.id}>{section.name}</option>)}</select></label>
          {!catalog.storeSections.length ? <p role="status">{t("ingredientsCatalog.noStoreSections")}</p> : null}
          {createError ? <p role="alert">{t(`recipesCatalog.errors.${createError}`, { defaultValue: t("recipesCatalog.errors.unavailable") })}</p> : null}
          <button disabled={createPending || !catalog.units.length || !catalog.storeSections.length} onClick={() => void createIngredient()} type="button">{t("recipesCatalog.saveIngredient")}</button>
          <button onClick={() => setCreating(false)} type="button">{t("recipesCatalog.cancel")}</button>
        </fieldset>
      ) : null}
    </div>
  );
}

function MarkdownVisualEditor({ value, onChange, onUnsupported, onSupported }: { value: string; onChange: (value: string) => void; onUnsupported: () => void; onSupported: () => void }) {
  const onChangeRef = useRef(onChange);
  const lastMarkdownRef = useRef(value);
  onChangeRef.current = onChange;
  const [loading, getEditor] = useInstance();
  useEditor((root) =>
    Editor.make()
      .config((ctx) => {
        ctx.set(rootCtx, root);
        ctx.set(defaultValueCtx, value);
        const source = value;
        ctx.get(listenerCtx).mounted((mountedCtx) => {
          const serialized = mountedCtx.get(serializerCtx)(mountedCtx.get(parserCtx)(source));
          if (serialized.trimEnd() !== source.trimEnd() || /<[^>]+>|^\s*\|.*\|/m.test(source)) onUnsupported();
          else onSupported();
        });
        ctx.get(listenerCtx).markdownUpdated((_, markdown) => {
          lastMarkdownRef.current = markdown;
          if (markdown.trimEnd() === source.trimEnd()) return;
          onChangeRef.current(markdown);
        });
      })
      .use(commonmark)
      .use(listener),
  []);
  useEffect(() => {
    if (loading || value === lastMarkdownRef.current) return;
    getEditor()?.action((ctx) => {
      const view = ctx.get(editorViewCtx);
      const document = ctx.get(parserCtx)(value);
      view.dispatch(view.state.tr.replaceWith(0, view.state.doc.content.size, document.content));
    });
    lastMarkdownRef.current = value;
  }, [getEditor, loading, value]);
  return <Milkdown />;
}

function RecipeEditor({
  catalog,
  recipe,
  organizationId,
  userId,
  initiallyOpen = false,
  onRouteBack,
  onDirtyChange,
  discardToken = 0,
}: {
  catalog: RecipeCatalogProjection;
  recipe: RecipeCatalogProjection["recipes"][number];
  organizationId: string;
  userId: string;
  initiallyOpen?: boolean;
  onRouteBack?: () => void;
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
  const [descriptionMode, setDescriptionMode] = useState<"visual" | "markdown">("visual");
  const [unsupportedDescription, setUnsupportedDescription] = useState(false);
  const dialogRef = useRef<HTMLDialogElement>(null);
  const openerRef = useRef<HTMLButtonElement>(null);
  const descriptionId = `recipe-description-${recipe.id}`;
  const previousDiscardToken = useRef(discardToken);
  const initialInput = useMemo(() => ({
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
  } satisfies RecipeVersionInput), [recipe]);
  const dirty = JSON.stringify(input) !== JSON.stringify(initialInput);
  useEffect(() => {
    if (!dirty) {
      setInput(initialInput);
      setUnsupportedDescription(false);
    }
  }, [initialInput, dirty]);
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
    const dialog = dialogRef.current;
    if (!dialog) return;
    if (open) {
      if (!dialog.open) {
        if (typeof dialog.showModal === "function") dialog.showModal();
        else dialog.setAttribute("open", "");
      }
      dialog.querySelector<HTMLElement>("input, select, textarea, button")?.focus();
    } else if (dialog.open) {
      if (typeof dialog.close === "function") dialog.close();
      else dialog.removeAttribute("open");
    }
  }, [open]);
  useEffect(() => {
    onDirtyChange?.(recipe.id, open && dirty);
    return () => onDirtyChange?.(recipe.id, false);
  }, [dirty, onDirtyChange, open, recipe.id]);
  function closeEditor() {
    if (onRouteBack) {
      onRouteBack();
      return;
    }
    if (dirty && !window.confirm(t("recipesCatalog.discardChanges"))) return;
    setInput(initialInput);
    setOpen(false);
    requestAnimationFrame(() => openerRef.current?.focus());
  }
  if (!open)
    return (
      <button onClick={() => setOpen(true)} ref={openerRef} type="button">
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
      if (onRouteBack) requestAnimationFrame(onRouteBack);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "unavailable");
    }
  }
  function changeDescriptionMode(event: React.KeyboardEvent<HTMLButtonElement>) {
    const tabs = Array.from(event.currentTarget.parentElement?.querySelectorAll<HTMLButtonElement>("[role=tab]") ?? []);
    const index = tabs.indexOf(event.currentTarget);
    const next = event.key === "ArrowRight" || event.key === "ArrowDown" ? (index + 1) % tabs.length : event.key === "ArrowLeft" || event.key === "ArrowUp" ? (index - 1 + tabs.length) % tabs.length : event.key === "Home" ? 0 : event.key === "End" ? tabs.length - 1 : -1;
    if (next < 0) return;
    event.preventDefault();
    tabs[next]?.focus();
    setDescriptionMode(next === 0 ? "visual" : "markdown");
  }
  const compatibleUnits = (ingredientVersionId: string) => {
    const dimension = catalog.ingredients.find((item) => item.versionId === ingredientVersionId)?.canonicalUnitId;
    const canonicalDimension = catalog.units.find((unit) => unit.id === dimension)?.dimension;
    return canonicalDimension ? catalog.units.filter((unit) => unit.dimension === canonicalDimension) : [];
  };
  return (
    <dialog
      aria-labelledby={`recipe-editor-${recipe.id}`}
      className="recipe-editor-dialog"
      onCancel={(event) => {
        event.preventDefault();
        closeEditor();
      }}
      ref={dialogRef}
    >
      <form className="recipe-create" onSubmit={(event) => void submit(event)}>
        <h4 id={`recipe-editor-${recipe.id}`}>{t("recipesCatalog.editHeading")}</h4>
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
        <div aria-label={t("recipesCatalog.descriptionMode")} className="recipe-description-mode" role="tablist">
          <button aria-controls={`${descriptionId}-visual`} aria-selected={descriptionMode === "visual"} id={`${descriptionId}-visual-tab`} onClick={() => setDescriptionMode("visual")} onKeyDown={changeDescriptionMode} role="tab" tabIndex={descriptionMode === "visual" ? 0 : -1} type="button">
            {t("recipesCatalog.descriptionVisual")}
          </button>
          <button aria-controls={`${descriptionId}-markdown`} aria-selected={descriptionMode === "markdown"} id={`${descriptionId}-markdown-tab`} onClick={() => setDescriptionMode("markdown")} onKeyDown={changeDescriptionMode} role="tab" tabIndex={descriptionMode === "markdown" ? 0 : -1} type="button">
            {t("recipesCatalog.descriptionMarkdown")}
          </button>
        </div>
        {unsupportedDescription ? <p role="status">{t("recipesCatalog.descriptionUnsupported")}</p> : null}
        {descriptionMode === "markdown" ? (
          <div aria-labelledby={`${descriptionId}-markdown-tab`} id={`${descriptionId}-markdown`} role="tabpanel">
            <textarea
              aria-label={t("recipesCatalog.descriptionMarkdown")}
              onChange={(event) => change("description", event.target.value)}
              value={input.description}
            />
          </div>
        ) : (
          <div aria-labelledby={`${descriptionId}-visual-tab`} className="recipe-description-preview" id={`${descriptionId}-visual`} role="tabpanel">
            <MilkdownProvider key={recipe.versionId}>
              <MarkdownVisualEditor
                onChange={(description) => change("description", description)}
                onUnsupported={() => {
                  setUnsupportedDescription(true);
                  setDescriptionMode("markdown");
                }}
                onSupported={() => setUnsupportedDescription(false)}
                value={input.description}
              />
            </MilkdownProvider>
          </div>
        )}
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
                ingredientLines: current.ingredientLines.map((item, itemIndex) => {
                  if (itemIndex !== index) return item;
                  const preferred = compatibleUnits(ingredientVersionId).some((unit) => unit.id === item.preferredDisplayUnitId)
                    ? item.preferredDisplayUnitId
                    : undefined;
                  return { ...item, ingredientVersionId, preferredDisplayUnitId: preferred };
                }),
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
            <label>
              {t("recipesCatalog.scalingBehavior")}
              <select
                aria-label={t("recipesCatalog.scalingBehavior")}
                value={line.scalingBehavior}
                onChange={(event) => setInput((current) => ({ ...current, ingredientLines: current.ingredientLines.map((item, itemIndex) => itemIndex === index ? { ...item, scalingBehavior: event.target.value as "proportional" | "fixed" } : item) }))}
              >
                <option value="proportional">{t("recipesCatalog.proportional")}</option>
                <option value="fixed">{t("recipesCatalog.fixed")}</option>
              </select>
            </label>
            <label>
              <input
                checked={line.includeInPortionWeight}
                onChange={(event) => setInput((current) => ({ ...current, ingredientLines: current.ingredientLines.map((item, itemIndex) => itemIndex === index ? { ...item, includeInPortionWeight: event.target.checked } : item) }))}
                type="checkbox"
              />
              {t("recipesCatalog.includeInPortionWeight")}
            </label>
            <label>
              {t("recipesCatalog.preferredDisplayUnit")}
              <select
                aria-label={t("recipesCatalog.preferredDisplayUnit")}
                value={line.preferredDisplayUnitId ?? ""}
                onChange={(event) => setInput((current) => ({ ...current, ingredientLines: current.ingredientLines.map((item, itemIndex) => itemIndex === index ? { ...item, ...(event.target.value ? { preferredDisplayUnitId: event.target.value } : { preferredDisplayUnitId: undefined }) } : item) }))}
              >
                <option value="">{t("recipesCatalog.noPreferredDisplayUnit")}</option>
                {compatibleUnits(line.ingredientVersionId).map((unit) => <option key={unit.id} value={unit.id}>{unit.name}</option>)}
              </select>
            </label>
            <label>
              {t("recipesCatalog.note")}
              <input
                aria-label={t("recipesCatalog.note")}
                value={line.note}
                onChange={(event) => setInput((current) => ({ ...current, ingredientLines: current.ingredientLines.map((item, itemIndex) => itemIndex === index ? { ...item, note: event.target.value } : item) }))}
              />
            </label>
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
      <RecipeTagPicker catalog={catalog} organizationId={organizationId} userId={userId} selected={input.recipeTagIds} onChange={(recipeTagIds) => setInput((current) => ({ ...current, recipeTagIds }))} />
      {error ? <p role="alert">{t(`recipesCatalog.errors.${error}`)}</p> : null}
      {saved ? <p role="status">{t("recipesCatalog.saved")}</p> : null}
      <button type="submit">{t("recipesCatalog.publish")}</button>
        <button className={onRouteBack ? "recipe-editor-back" : undefined} onClick={closeEditor} type="button">
          {onRouteBack ? t("recipesCatalog.backToCatalog") : t("recipesCatalog.cancel")}
        </button>
      </form>
    </dialog>
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
  const [tagFilter, setTagFilter] = useState("");
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
      (showRetired || !recipe.retired) || recipe.id === selectedRecipeId || recipe.id === editRecipeId,
  );
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
    if (tagFilter && !recipe.recipeTagIds.includes(tagFilter) && recipe.id !== editRecipeId) return false;
    if (!query.trim()) return true;
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
    ].some((value) => matchesIngredient(value, query));
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
      <label>{t("recipesCatalog.tagFilter")}<select aria-label={t("recipesCatalog.tagFilter")} value={tagFilter} onChange={(event) => setTagFilter(event.target.value)}><option value="">{t("recipesCatalog.allTags")}</option>{state.catalog.tags.map((tag) => <option key={tag.id} value={tag.id}>{tag.name}</option>)}</select></label>
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
              <details>
                <summary>{t("recipesCatalog.versionHistory")}</summary>
                {(() => {
                  const history = recipe.versionHistory;
                  const current = history.find((version) => version.id === recipe.versionId);
                  const previous = history.filter((version) => version.id !== recipe.versionId);
                  const metadata = (version: typeof history[number]) => <>{version.id}{version.publishedAt ? <> · <time dateTime={version.publishedAt}>{version.publishedAt}</time></> : null}{version.publishedByUserId ? ` · ${t("recipesCatalog.publishedBy")}: ${version.publishedByUserId}` : ""}</>;
                  return <>
                    <p><strong>{t("recipesCatalog.currentVersion")}</strong> {current?.name ?? recipe.name} · {current ? metadata(current) : recipe.versionId}</p>
                    {previous.length ? (
                  <ul aria-label={t("recipesCatalog.versionHistory")}>
                    {previous.map((version) => (
                      <li key={version.id}>{version.name ? `${version.name} · ` : ""}{metadata(version)}</li>
                    ))}
                  </ul>
                    ) : <p>{t("recipesCatalog.noVersionHistory")}</p>}
                  </>;
                })()}
              </details>
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
                onRouteBack={recipe.id === editRecipeId ? onBackToCatalog : undefined}
                discardToken={discardToken}
                onDirtyChange={reportRecipeDirty}
                userId={userId}
              />
              <RecipeLifecycleControl
                organizationId={organizationId}
                recipe={recipe}
                userId={userId}
              />
              {!recipe.retired ? <RecipeCopyPanel
                organizationId={organizationId}
                onUnauthenticated={onUnauthenticated}
                recipe={recipe}
                userId={userId}
              /> : null}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
