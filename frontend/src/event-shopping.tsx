import { liveQuery } from "dexie";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useTranslation } from "react-i18next";

import {
  readEventPlanner,
  type EventPlannerProjection,
} from "./planner-projections";
import {
  hasQueuedShoppingListRefresh,
  queueShoppingList,
  queueShoppingListRename,
  queueShoppingListRefresh,
} from "./shopping-list";
import {
  queueAdHocShoppingItem,
  queueAdHocShoppingItemFulfilment,
  queueAdHocShoppingItemLifecycle,
  queueAdHocShoppingItemUpdate,
} from "./ad-hoc-shopping-item";
import {
  queueShoppingAvailableSupply,
  queueShoppingContributionFulfilment,
  queueShoppingManualPurchaseTarget,
  queueShoppingRowFulfilment,
  queueShoppingRowNote,
  queueShoppingStoreSectionOverride,
} from "./shopping-operations";
import {
  readShoppingList,
  readShoppingLists,
  type ShoppingListProjection,
  type ShoppingListSummary,
} from "./shopping-projections";
import { pullOrganization, SyncRequestError } from "./sync-bootstrap";
import { SynchronizationStatus } from "./synchronization-status";

type ShoppingState = "loading" | "ready" | "offline" | "error";

function ShoppingCreate({
  planner,
  eventId,
  organizationId,
  userId,
}: {
  planner: EventPlannerProjection;
  eventId: string;
  organizationId: string;
  userId: string;
}) {
  const { t } = useTranslation();
  const [name, setName] = useState("");
  const [selected, setSelected] = useState<string[]>([]);
  const [error, setError] = useState<string>();
  const [saved, setSaved] = useState(false);
  const inFlight = useRef(false);

  function toggle(id: string, checked: boolean) {
    setSelected((current) =>
      checked
        ? [...new Set([...current, id])]
        : current.filter((item) => item !== id),
    );
  }

  async function submit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (inFlight.current) return;
    inFlight.current = true;
    try {
      await queueShoppingList(userId, organizationId, {
        eventId,
        name,
        scheduledRecipeIds: selected,
      });
      setName("");
      setSelected([]);
      setError(undefined);
      setSaved(true);
    } catch {
      setSaved(false);
      setError("unavailable");
    } finally {
      inFlight.current = false;
    }
  }

  if (planner.lifecycle !== "active") return null;
  return (
    <form className="shopping-create" onSubmit={(event) => void submit(event)}>
      <h3>{t("shopping.createHeading")}</h3>
      <label>
        {t("shopping.name")}
        <input
          maxLength={200}
          onChange={(event) => setName(event.target.value)}
          required
          value={name}
        />
      </label>
      <fieldset>
        <legend>{t("shopping.sources")}</legend>
        {planner.scheduled.length ? (
          planner.scheduled.map((recipe) => (
            <label className="shopping-create__source" key={recipe.id}>
              <input
                checked={selected.includes(recipe.id)}
                onChange={(event) => toggle(recipe.id, event.target.checked)}
                type="checkbox"
              />
              <span>
                {recipe.name} ·{" "}
                {t("planner.diners", { count: recipe.dinerCount })}
              </span>
            </label>
          ))
        ) : (
          <p>{t("shopping.noSources")}</p>
        )}
      </fieldset>
      <button type="submit">{t("shopping.create")}</button>
      {error ? <p role="alert">{t(`shopping.errors.${error}`)}</p> : null}
      {saved ? <p role="status">{t("shopping.saved")}</p> : null}
    </form>
  );
}

function ShoppingIndex({
  lists,
  onOpenList,
}: {
  lists: ShoppingListSummary[];
  onOpenList: (shoppingListId: string) => void;
}) {
  const { t } = useTranslation();
  if (!lists.length) return <p>{t("shopping.empty")}</p>;
  return (
    <ul className="shopping-list-index">
      {lists.map((list) => (
        <li key={list.id}>
          <div>
            <h3>{list.name}</h3>
            <p>{t("shopping.sourceCount", { count: list.sourceCount })}</p>
          </div>
          <button onClick={() => onOpenList(list.id)} type="button">
            {t("shopping.open")}
          </button>
        </li>
      ))}
    </ul>
  );
}

function ShoppingDetail({
  shoppingList,
  onBack,
  organizationId,
  userId,
  editable,
  planner,
  refreshPending,
}: {
  shoppingList: ShoppingListProjection;
  onBack: () => void;
  organizationId: string;
  userId: string;
  editable: boolean;
  planner: EventPlannerProjection;
  refreshPending: boolean;
}) {
  const { t } = useTranslation();
  const [hideCompleted, setHideCompleted] = useState(false);
  const visibleRows = hideCompleted
    ? shoppingList.rows.filter((row) => !row.fulfilled && !row.notRequired)
    : shoppingList.rows;
  const sections = new Map<string, ShoppingListProjection["rows"]>();
  for (const row of visibleRows) {
    const section = row.sectionName ?? t("shopping.noSection");
    sections.set(section, [...(sections.get(section) ?? []), row]);
  }
  return (
    <section
      className="shopping-detail"
      aria-labelledby="shopping-detail-heading"
    >
      <button className="shopping-detail__back" onClick={onBack} type="button">
        {t("shopping.back")}
      </button>
      <h3 id="shopping-detail-heading">{shoppingList.name}</h3>
      {editable ? (
        <ShoppingListRename
          organizationId={organizationId}
          shoppingList={shoppingList}
          userId={userId}
        />
      ) : null}
      {editable ? (
        <ShoppingRefresh
          organizationId={organizationId}
          planner={planner}
          refreshPending={refreshPending}
          shoppingList={shoppingList}
          userId={userId}
        />
      ) : null}
      {editable ? (
        <AdHocShoppingCreate
          organizationId={organizationId}
          shoppingList={shoppingList}
          userId={userId}
        />
      ) : null}
      {shoppingList.rows.length ? (
        <label>
          <input
            checked={hideCompleted}
            onChange={(event) => setHideCompleted(event.currentTarget.checked)}
            type="checkbox"
          />
          {t("shopping.hideCompleted")}
        </label>
      ) : null}
      {visibleRows.length ? (
        [...sections].map(([section, rows]) => (
          <section className="shopping-section" key={section}>
            <h4>{section}</h4>
            <ul className="shopping-rows">
              {rows.map((row) => (
                <ShoppingRowControls
                  editable={editable}
                  key={row.id}
                  organizationId={organizationId}
                  row={row}
                  shoppingList={shoppingList}
                  shoppingListId={shoppingList.id}
                  userId={userId}
                />
              ))}
            </ul>
          </section>
        ))
      ) : (
        <p>{shoppingList.rows.length ? t("shopping.noFilteredRows") : t("shopping.noRows")}</p>
      )}
      {shoppingList.adHocItems.length ? (
        <section
          className="shopping-ad-hoc-items"
          aria-labelledby="shopping-ad-hoc-heading"
        >
          <h4 id="shopping-ad-hoc-heading">{t("shopping.adHoc.heading")}</h4>
          <ul>
            {shoppingList.adHocItems.map((item) => (
              <li key={item.id}>
                {editable && !item.retired ? (
                  <label className="shopping-row-controls__fulfilled">
                    <input
                      checked={item.fulfilled}
                      aria-checked={item.partial ? "mixed" : undefined}
                      onChange={(event) =>
                        void queueAdHocShoppingItemFulfilment(userId, organizationId, {
                          shoppingListId: shoppingList.id,
                          adHocShoppingItemId: item.id,
                          fulfilled: event.currentTarget.checked,
                        })
                      }
                      ref={(element) => {
                        if (element) element.indeterminate = item.partial;
                      }}
                      type="checkbox"
                    />
                    {t("shopping.fulfilled")}
                  </label>
                ) : null}
                <strong>{item.name}</strong> ·{" "}
                {t("shopping.quantity", {
                  amount: item.target,
                  unit: item.unit,
                })}
                {item.sectionName ? ` · ${item.sectionName}` : null}
                {item.retired ? ` · ${t("shopping.retired")}` : null}
                {item.note ? <p>{item.note}</p> : null}
                {editable && !item.retired ? (
                  <AdHocShoppingEdit
                    item={item}
                    organizationId={organizationId}
                    shoppingList={shoppingList}
                    userId={userId}
                  />
                ) : null}
                {editable ? (
                  <button
                    onClick={() =>
                      void queueAdHocShoppingItemLifecycle(userId, organizationId, {
                        shoppingListId: shoppingList.id,
                        adHocShoppingItemId: item.id,
                        operation: item.retired ? "restore" : "retire",
                      })
                    }
                    type="button"
                  >
                    {t(item.retired ? "shopping.adHoc.restore" : "shopping.adHoc.retire")}
                  </button>
                ) : null}
              </li>
            ))}
          </ul>
        </section>
      ) : null}
    </section>
  );
}

function ShoppingListRename({
  organizationId,
  shoppingList,
  userId,
}: {
  organizationId: string;
  shoppingList: ShoppingListProjection;
  userId: string;
}) {
  const { t } = useTranslation();
  const [name, setName] = useState(shoppingList.name);
  const [error, setError] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  useEffect(() => setName(shoppingList.name), [shoppingList.name]);
  async function submit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (submitting) return;
    setSubmitting(true);
    try {
      await queueShoppingListRename(userId, organizationId, { shoppingListId: shoppingList.id, name });
      setError(false);
    } catch {
      setError(true);
    } finally {
      setSubmitting(false);
    }
  }
  return (
    <form aria-label={t("shopping.renameHeading")} onSubmit={(event) => void submit(event)}>
      <label>
        {t("shopping.name")}
        <input maxLength={200} onChange={(event) => setName(event.currentTarget.value)} required value={name} />
      </label>
      <button disabled={submitting} type="submit">{t("shopping.rename")}</button>
      {error ? <p role="alert">{t("shopping.errors.unavailable")}</p> : null}
    </form>
  );
}

function AdHocShoppingEdit({
  item,
  organizationId,
  shoppingList,
  userId,
}: {
  item: ShoppingListProjection["adHocItems"][number];
  organizationId: string;
  shoppingList: ShoppingListProjection;
  userId: string;
}) {
  const { t } = useTranslation();
  const [editing, setEditing] = useState(false);
  const [name, setName] = useState(item.name);
  const [targetAmount, setTargetAmount] = useState(item.target);
  const [unitId, setUnitId] = useState(item.unitId);
  const [sectionId, setSectionId] = useState(item.sectionId);
  const [note, setNote] = useState(item.note ?? "");
  const [error, setError] = useState(false);
  if (!editing)
    return <button onClick={() => setEditing(true)} type="button">{t("shopping.adHoc.edit")}</button>;
  async function submit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    try {
      await queueAdHocShoppingItemUpdate(userId, organizationId, {
        shoppingListId: shoppingList.id, adHocShoppingItemId: item.id, name, targetAmount,
        unitId, storeSectionId: sectionId, note,
      });
      setError(false);
      setEditing(false);
    } catch {
      setError(true);
    }
  }
  return (
    <form onSubmit={(event) => void submit(event)}>
      <label>{t("shopping.adHoc.name")}<input maxLength={200} onChange={(event) => setName(event.currentTarget.value)} required value={name} /></label>
      <label>{t("shopping.adHoc.amount")}<input inputMode="decimal" min="0" onChange={(event) => setTargetAmount(event.currentTarget.value)} required type="number" value={targetAmount} /></label>
      <label>{t("shopping.adHoc.unit")}<select onChange={(event) => setUnitId(event.currentTarget.value)} value={unitId}>{shoppingList.quantityUnits.map((unit) => <option key={unit.id} value={unit.id}>{unit.name}</option>)}</select></label>
      <label>{t("shopping.adHoc.section")}<select onChange={(event) => setSectionId(event.currentTarget.value)} value={sectionId}>{shoppingList.storeSections.map((section) => <option key={section.id} value={section.id}>{section.name}</option>)}</select></label>
      <label>{t("shopping.adHoc.note")}<input maxLength={4000} onChange={(event) => setNote(event.currentTarget.value)} value={note} /></label>
      <button type="submit">{t("shopping.adHoc.save")}</button>
      <button onClick={() => setEditing(false)} type="button">{t("shopping.cancel")}</button>
      {error ? <p role="alert">{t("shopping.errors.unavailable")}</p> : null}
    </form>
  );
}

function AdHocShoppingCreate({
  organizationId,
  shoppingList,
  userId,
}: {
  organizationId: string;
  shoppingList: ShoppingListProjection;
  userId: string;
}) {
  const { t } = useTranslation();
  const units = shoppingList.quantityUnits;
  const sections = shoppingList.storeSections;
  const [name, setName] = useState("");
  const [targetAmount, setTargetAmount] = useState("");
  const [unitId, setUnitId] = useState(units[0]?.id ?? "");
  const [sectionId, setSectionId] = useState(sections[0]?.id ?? "");
  const [note, setNote] = useState("");
  const [error, setError] = useState(false);
  useEffect(
    () => setUnitId((current) => current || units[0]?.id || ""),
    [units],
  );
  useEffect(
    () => setSectionId((current) => current || sections[0]?.id || ""),
    [sections],
  );
  async function submit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    try {
      await queueAdHocShoppingItem(userId, organizationId, {
        shoppingListId: shoppingList.id,
        name,
        targetAmount,
        unitId,
        storeSectionId: sectionId,
        note,
      });
      setName("");
      setTargetAmount("");
      setNote("");
      setError(false);
    } catch {
      setError(true);
    }
  }
  if (!units.length || !sections.length) return null;
  return (
    <form
      className="shopping-ad-hoc-create"
      onSubmit={(event) => void submit(event)}
    >
      <h4>{t("shopping.adHoc.heading")}</h4>
      <label>
        {t("shopping.adHoc.name")}
        <input
          maxLength={200}
          onChange={(event) => setName(event.currentTarget.value)}
          required
          value={name}
        />
      </label>
      <label>
        {t("shopping.adHoc.amount")}
        <input
          inputMode="decimal"
          min="0"
          onChange={(event) => setTargetAmount(event.currentTarget.value)}
          required
          type="number"
          value={targetAmount}
        />
      </label>
      <label>
        {t("shopping.adHoc.unit")}
        <select
          onChange={(event) => setUnitId(event.currentTarget.value)}
          value={unitId}
        >
          {units.map((unit) => (
            <option key={unit.id} value={unit.id}>
              {unit.name}
            </option>
          ))}
        </select>
      </label>
      <label>
        {t("shopping.adHoc.section")}
        <select
          onChange={(event) => setSectionId(event.currentTarget.value)}
          value={sectionId}
        >
          {sections.map((section) => (
            <option key={section.id} value={section.id}>
              {section.name}
            </option>
          ))}
        </select>
      </label>
      <label>
        {t("shopping.adHoc.note")}
        <input
          maxLength={4000}
          onChange={(event) => setNote(event.currentTarget.value)}
          value={note}
        />
      </label>
      <button type="submit">{t("shopping.adHoc.create")}</button>
      {error ? <p role="alert">{t("shopping.errors.unavailable")}</p> : null}
    </form>
  );
}

function ShoppingRefresh({
  organizationId,
  planner,
  refreshPending,
  shoppingList,
  userId,
}: {
  organizationId: string;
  planner: EventPlannerProjection;
  refreshPending: boolean;
  shoppingList: ShoppingListProjection;
  userId: string;
}) {
  const { t } = useTranslation();
  const [open, setOpen] = useState(false);
  const currentSources = useMemo(() => {
    const selectableSourceIds = new Set(
      planner.scheduled.map((recipe) => recipe.id),
    );
    return shoppingList.sourceRecipeIds.filter((id) =>
      selectableSourceIds.has(id),
    );
  }, [planner.scheduled, shoppingList.sourceRecipeIds]);
  const [selected, setSelected] = useState(currentSources);
  const [error, setError] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [queued, setQueued] = useState(false);
  useEffect(() => setSelected(currentSources), [currentSources]);
  function toggle(id: string, checked: boolean) {
    setSelected((current) =>
      checked
        ? [...new Set([...current, id])]
        : current.filter((item) => item !== id),
    );
  }
  async function submit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (submitting) return;
    setSubmitting(true);
    try {
      await queueShoppingListRefresh(userId, organizationId, {
        shoppingListId: shoppingList.id,
        parentGenerationRevisionId: shoppingList.currentGenerationRevisionId,
        scheduledRecipeIds: selected,
      });
      setError(false);
      setOpen(false);
      setQueued(true);
    } catch {
      setError(true);
    } finally {
      setSubmitting(false);
    }
  }
  if (refreshPending || queued)
    return <p role="status">{t("shopping.refreshPending")}</p>;
  return open ? (
    <form
      aria-label={t("shopping.refreshHeading")}
      onSubmit={(event) => void submit(event)}
      role="dialog"
    >
      <h4>{t("shopping.refreshHeading")}</h4>
      <fieldset>
        <legend>{t("shopping.sources")}</legend>
        {planner.scheduled.map((recipe) => (
          <label className="shopping-create__source" key={recipe.id}>
            <input
              checked={selected.includes(recipe.id)}
              onChange={(event) => toggle(recipe.id, event.target.checked)}
              type="checkbox"
            />
            <span>
              {recipe.name} ·{" "}
              {t("planner.diners", { count: recipe.dinerCount })}
            </span>
          </label>
        ))}
      </fieldset>
      <button disabled={submitting} type="submit">
        {t("shopping.refresh")}
      </button>
      <button
        disabled={submitting}
        onClick={() => setOpen(false)}
        type="button"
      >
        {t("shopping.cancel")}
      </button>
      {error ? <p role="alert">{t("shopping.errors.unavailable")}</p> : null}
    </form>
  ) : (
    <button onClick={() => setOpen(true)} type="button">
      {t("shopping.refresh")}
    </button>
  );
}

function ShoppingRowControls({
  editable,
  organizationId,
  row,
  shoppingList,
  shoppingListId,
  userId,
}: {
  editable: boolean;
  organizationId: string;
  row: ShoppingListProjection["rows"][number];
  shoppingList: ShoppingListProjection;
  shoppingListId: string;
  userId: string;
}) {
  const { t } = useTranslation();
  const [error, setError] = useState(false);
  const [availableSupply, setAvailableSupply] = useState(row.availableSupply);
  const [manualTarget, setManualTarget] = useState(
    row.manualPurchaseTarget ?? row.target,
  );
  const [sectionId, setSectionId] = useState(row.storeSectionOverrideId ?? "");
  const [note, setNote] = useState(row.note ?? "");
  const fulfilmentInput = useRef<HTMLInputElement>(null);
  useEffect(
    () => setAvailableSupply(row.availableSupply),
    [row.availableSupply],
  );
  useEffect(
    () => setManualTarget(row.manualPurchaseTarget ?? row.target),
    [row.manualPurchaseTarget, row.target],
  );
  useEffect(() => setSectionId(row.storeSectionOverrideId ?? ""), [row.storeSectionOverrideId]);
  useEffect(() => setNote(row.note ?? ""), [row.note]);
  useEffect(() => {
    if (fulfilmentInput.current) fulfilmentInput.current.indeterminate = row.partial;
  }, [row.partial]);
  const input = { shoppingListId, shoppingIngredientRowId: row.id };
  async function run(work: () => Promise<void>) {
    try {
      await work();
      setError(false);
    } catch {
      setError(true);
    }
  }
  return (
    <li>
      <div>
        <h5>{row.ingredientName}</h5>
        <dl>
          <div>
            <dt>{t("shopping.remaining")}</dt>
            <dd>
              {t("shopping.quantity", {
                amount: row.remaining,
                unit: row.unit,
              })}
            </dd>
          </div>
          <div>
            <dt>{t("shopping.target")}</dt>
            <dd>
              {t("shopping.quantity", { amount: row.target, unit: row.unit })}
            </dd>
          </div>
        </dl>
      </div>
      {editable ? (
        <div className="shopping-row-controls">
          <label>
            {t("shopping.availableSupply", { unit: row.unit })}
            <input
              onChange={(event) =>
                setAvailableSupply(event.currentTarget.value)
              }
              inputMode="decimal"
              min="0"
              onBlur={() =>
                availableSupply === row.availableSupply
                  ? undefined
                  : void run(() =>
                      queueShoppingAvailableSupply(userId, organizationId, {
                        ...input,
                        quantity: availableSupply,
                      }),
                    )
              }
              type="number"
              value={availableSupply}
            />
          </label>
          <label>
            {t("shopping.manualTarget", { unit: row.unit })}
            <input
              onChange={(event) => setManualTarget(event.currentTarget.value)}
              inputMode="decimal"
              min="0"
              onBlur={() =>
                manualTarget === (row.manualPurchaseTarget ?? row.target)
                  ? undefined
                  : void run(() =>
                      queueShoppingManualPurchaseTarget(
                        userId,
                        organizationId,
                        {
                          ...input,
                          quantity: manualTarget || null,
                        },
                      ),
                    )
              }
              type="number"
              value={manualTarget}
            />
          </label>
          <label>
            {t("shopping.storeSection")}
            <select
              onChange={(event) => {
                const next = event.currentTarget.value;
                setSectionId(next);
                if (next !== sectionId)
                  void run(() =>
                    queueShoppingStoreSectionOverride(userId, organizationId, {
                      ...input,
                      storeSectionId: next || null,
                    }),
                  );
              }}
              value={sectionId}
            >
              <option value="">{t("shopping.defaultStoreSection")}</option>
              {shoppingList.storeSections.map((section) => (
                <option key={section.id} value={section.id}>{section.name}</option>
              ))}
            </select>
          </label>
          <label>
            {t("shopping.note")}
            <textarea
              aria-label={t("shopping.note")}
              onChange={(event) => setNote(event.currentTarget.value)}
              onBlur={() =>
                note !== (row.note ?? "")
                  ? void run(() =>
                      queueShoppingRowNote(userId, organizationId, {
                        ...input,
                        note,
                      }),
                    )
                  : undefined
              }
              value={note}
            />
          </label>
          {row.note !== null ? (
            <button
              onClick={() => {
                setNote("");
                void run(() =>
                  queueShoppingRowNote(userId, organizationId, {
                    ...input,
                    note: null,
                  }),
                );
              }}
              type="button"
            >
              {t("shopping.clearNote")}
            </button>
          ) : null}
          <label className="shopping-row-controls__fulfilled">
            <input
              aria-checked={row.partial ? "mixed" : undefined}
              checked={row.fulfilled}
              disabled={row.notRequired}
              onChange={(event) =>
                void run(() =>
                  queueShoppingRowFulfilment(userId, organizationId, {
                    ...input,
                    fulfilled: event.currentTarget.checked,
                  }),
                )
              }
              ref={fulfilmentInput}
              type="checkbox"
            />
            {row.notRequired
              ? t("shopping.notRequired")
              : t("shopping.fulfilled")}
          </label>
          {row.manualPurchaseTarget !== null ? (
            <button
              onClick={() =>
                void run(() =>
                  queueShoppingManualPurchaseTarget(userId, organizationId, {
                    ...input,
                    quantity: null,
                  }),
                )
              }
              type="button"
            >
              {t("shopping.clearManualTarget")}
            </button>
          ) : null}
          {error ? (
            <p role="alert">{t("shopping.errors.unavailable")}</p>
          ) : null}
        </div>
      ) : null}
      {row.contributions.length ? (
        <details className="shopping-contributions">
          <summary>{t("shopping.contributions")}</summary>
          <ul>
            {row.contributions.map((contribution) => {
              const requiredQuantity = contribution.requiredQuantity ?? contribution.generated;
              const lineNotes = contribution.lineNotes ?? [];
              const recipeNotes = contribution.recipeNotes ?? [];
              const ingredientNotes = contribution.ingredientNotes ?? [];
              const label = `${contribution.source ?? t("shopping.scheduledRecipe")} · ${t(
                "shopping.quantity",
                { amount: requiredQuantity, unit: row.unit },
              )}${contribution.retired ? ` · ${t("shopping.retired")}` : ""}`;
              return (
                <li key={contribution.id}>
                  {editable ? (
                    <label>
                      <input
                        aria-label={label}
                        aria-checked={contribution.partial ? "mixed" : undefined}
                        checked={contribution.fulfilled}
                        onChange={(event) =>
                          void run(() =>
                            queueShoppingContributionFulfilment(
                              userId,
                              organizationId,
                              {
                                ...input,
                                shoppingContributionId: contribution.id,
                                fulfilled: event.currentTarget.checked,
                              },
                            ),
                          )
                        }
                        ref={(element) => {
                          if (element) element.indeterminate = contribution.partial;
                        }}
                        type="checkbox"
                      />
                      {label}
                    </label>
                  ) : (
                    <span>{label}</span>
                  )}
                  <dl>
                    <div>
                      <dt>{t("shopping.generatedRequirement")}</dt>
                      <dd>
                        {t("shopping.quantity", {
                          amount: requiredQuantity,
                          unit: row.unit,
                        })}
                      </dd>
                    </div>
                    <div>
                      <dt>{t("shopping.purchaseTarget")}</dt>
                      <dd>
                        {t("shopping.quantity", {
                          amount: row.target,
                          unit: row.unit,
                        })}
                      </dd>
                    </div>
                    {contribution.day ? (
                      <div>
                        <dt>{t("shopping.day")}</dt>
                        <dd>{contribution.day}</dd>
                      </div>
                    ) : null}
                    {contribution.mealRole ? (
                      <div>
                        <dt>{t("shopping.mealRole")}</dt>
                        <dd>{contribution.mealRole}</dd>
                      </div>
                    ) : null}
                    {contribution.recipeDescription ? (
                      <div>
                        <dt>{t("shopping.recipeDescription")}</dt>
                        <dd>{contribution.recipeDescription}</dd>
                      </div>
                    ) : null}
                    {contribution.estimatedUnitPrice ? (
                      <div>
                        <dt>{t("shopping.estimatedUnitPrice")}</dt>
                        <dd>{contribution.estimatedUnitPrice}</dd>
                      </div>
                    ) : (
                      <div>
                        <dt>{t("shopping.estimatedUnitPrice")}</dt>
                        <dd>{t("shopping.priceUnavailable")}</dd>
                      </div>
                    )}
                    {contribution.expectedCost ? (
                      <div>
                        <dt>{t("shopping.expectedCost")}</dt>
                        <dd>{contribution.expectedCost}</dd>
                      </div>
                    ) : (
                      <div>
                        <dt>{t("shopping.expectedCost")}</dt>
                        <dd>{t("shopping.priceUnavailable")}</dd>
                      </div>
                    )}
                  </dl>
                  {lineNotes.length ? (
                    <p>
                      <strong>{t("shopping.lineNotes")}:</strong>{" "}
                      {lineNotes.join(" · ")}
                    </p>
                  ) : null}
                  {recipeNotes.length ? (
                    <p>
                      <strong>{t("shopping.recipeNotes")}:</strong>{" "}
                      {recipeNotes.join(" · ")}
                    </p>
                  ) : null}
                  {ingredientNotes.length ? (
                    <p>
                      <strong>{t("shopping.ingredientNotes")}:</strong>{" "}
                      {ingredientNotes.join(" · ")}
                    </p>
                  ) : null}
                </li>
              );
            })}
          </ul>
        </details>
      ) : null}
    </li>
  );
}

export function EventShopping({
  eventId,
  organizationId,
  shoppingListId,
  userId,
  onOpenList,
  onOpenPlanner,
  onBack,
  onUnauthenticated,
}: {
  eventId: string;
  organizationId: string;
  shoppingListId?: string;
  userId: string;
  onOpenList: (shoppingListId: string) => void;
  onOpenPlanner: () => void;
  onBack: () => void;
  onUnauthenticated: () => void;
}) {
  const { t } = useTranslation();
  const [state, setState] = useState<ShoppingState>("loading");
  const [planner, setPlanner] = useState<EventPlannerProjection>();
  const [lists, setLists] = useState<ShoppingListSummary[]>([]);
  const [shoppingList, setShoppingList] = useState<ShoppingListProjection>();
  const [refreshPending, setRefreshPending] = useState(false);
  const generation = useRef(0);
  const synchronize = useCallback(async () => {
    const current = generation.current;
    if (!navigator.onLine) return setState("offline");
    try {
      await pullOrganization(userId, organizationId);
      if (current === generation.current) setState("ready");
    } catch (error) {
      if (error instanceof SyncRequestError && error.status === 401)
        return onUnauthenticated();
      if (current === generation.current) setState("error");
    }
  }, [onUnauthenticated, organizationId, userId]);
  useEffect(() => {
    let active = true;
    generation.current += 1;
    const subscription = liveQuery(async () => ({
      planner: await readEventPlanner(userId, organizationId, eventId),
      lists: await readShoppingLists(userId, organizationId, eventId),
      shoppingList: shoppingListId
        ? await readShoppingList(
            userId,
            organizationId,
            eventId,
            shoppingListId,
          )
        : undefined,
      refreshPending: shoppingListId
        ? await hasQueuedShoppingListRefresh(
            userId,
            organizationId,
            shoppingListId,
          )
        : false,
    })).subscribe({
      next: (next) => {
        if (!active) return;
        setPlanner(next.planner);
        setLists(next.lists);
        setShoppingList(next.shoppingList);
        setRefreshPending(next.refreshPending);
      },
      error: () => active && setState("error"),
    });
    const offline = () => setState("offline");
    window.addEventListener("online", synchronize);
    window.addEventListener("offline", offline);
    void synchronize();
    return () => {
      active = false;
      generation.current += 1;
      subscription.unsubscribe();
      window.removeEventListener("online", synchronize);
      window.removeEventListener("offline", offline);
    };
  }, [eventId, organizationId, shoppingListId, synchronize, userId]);
  if (!planner && state === "loading")
    return <p role="status">{t("shopping.loading")}</p>;
  if (!planner)
    return (
      <div role="alert">
        <p>{t("shopping.unavailable")}</p>
        <button onClick={() => void synchronize()} type="button">
          {t("eventsOverview.retry")}
        </button>
      </div>
    );
  return (
    <section className="event-shopping" aria-labelledby="shopping-heading">
      <header className="event-workspace__summary">
        <div>
          <h2 id="shopping-heading">{t("shopping.heading")}</h2>
          <p>{planner.name}</p>
        </div>
        <SynchronizationStatus
          organizationId={organizationId}
          userId={userId}
        />
      </header>
      <button onClick={onOpenPlanner} type="button">
        {t("shopping.planner")}
      </button>
      {planner.lifecycle === "archived" ? (
        <p className="planner-archived" role="status">
          {t("shopping.archived")}
        </p>
      ) : null}
      {state === "offline" ? (
        <p role="status">{t("shopping.offline")}</p>
      ) : null}
      {shoppingListId ? (
        shoppingList ? (
          <ShoppingDetail
            editable={planner.lifecycle === "active"}
            onBack={onBack}
            organizationId={organizationId}
            planner={planner}
            refreshPending={refreshPending}
            shoppingList={shoppingList}
            userId={userId}
          />
        ) : (
          <div role="alert">
            <p>{t("shopping.listUnavailable")}</p>
            <button onClick={onBack} type="button">
              {t("shopping.back")}
            </button>
          </div>
        )
      ) : (
        <>
          <ShoppingCreate
            eventId={eventId}
            organizationId={organizationId}
            planner={planner}
            userId={userId}
          />
          <ShoppingIndex lists={lists} onOpenList={onOpenList} />
        </>
      )}
      {state === "error" ? (
        <div role="alert">
          <p>{t("shopping.error")}</p>
          <button onClick={() => void synchronize()} type="button">
            {t("eventsOverview.retry")}
          </button>
        </div>
      ) : null}
    </section>
  );
}
