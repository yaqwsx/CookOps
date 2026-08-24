import { useRef, useState } from "react";
import { useTranslation } from "react-i18next";

import type { EventPlannerProjection } from "./planner-projections";
import { queueShoppingList } from "./shopping-list";

export function ShoppingCreate({
  planner,
  eventId,
  organizationId,
  userId,
  onCreated,
}: {
  planner: EventPlannerProjection;
  eventId: string;
  organizationId: string;
  userId: string;
  onCreated?: (shoppingListId: string) => void;
}) {
  const { t } = useTranslation();
  const [name, setName] = useState("");
  const [selected, setSelected] = useState<string[]>([]);
  const [error, setError] = useState<string>();
  const [saved, setSaved] = useState(false);
  const inFlight = useRef(false);
  const days = planner.days ?? [];
  const roles = planner.roles ?? [];
  const visibleScheduled = (planner.scheduled ?? []).filter(
    (recipe) =>
      !recipe.retired &&
      days.some((day) => day.id === recipe.dayId) &&
      roles.some((role) => role.id === recipe.roleId),
  );

  async function submit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (inFlight.current || !name.trim()) return;
    inFlight.current = true;
    try {
      const shoppingListId = await queueShoppingList(userId, organizationId, {
        eventId,
        name,
        scheduledRecipeIds: selected,
      });
      setName("");
      setSelected([]);
      setError(undefined);
      setSaved(true);
      onCreated?.(shoppingListId);
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
        {visibleScheduled.length ? (
          days.map((day) => {
            const dayRecipes = visibleScheduled.filter(
              (recipe) => recipe.dayId === day.id,
            );
            if (!dayRecipes.length) return null;
            return (
              <fieldset key={day.id}>
                <legend>{day.date}</legend>
                {roles.map((role) => {
                  const roleRecipes = dayRecipes.filter(
                    (recipe) => recipe.roleId === role.id,
                  );
                  if (!roleRecipes.length) return null;
                  return (
                    <div key={role.id}>
                      <strong>{role.name}</strong>
                      {roleRecipes.map((recipe) => (
                        <label
                          className="shopping-create__source"
                          key={recipe.id}
                        >
                          <input
                            checked={selected.includes(recipe.id)}
                            onChange={(event) =>
                              setSelected((current) =>
                                event.target.checked
                                  ? [...new Set([...current, recipe.id])]
                                  : current.filter((id) => id !== recipe.id),
                              )
                            }
                            type="checkbox"
                          />
                          <span>
                            {recipe.name} ·{" "}
                            {t("planner.diners", { count: recipe.dinerCount })}
                          </span>
                        </label>
                      ))}
                    </div>
                  );
                })}
              </fieldset>
            );
          })
        ) : (
          <p>{t("shopping.noSources")}</p>
        )}
      </fieldset>
      <button disabled={!name.trim()} type="submit">
        {t("shopping.create")}
      </button>
      {error ? <p role="alert">{t(`shopping.errors.${error}`)}</p> : null}
      {saved ? <p role="status">{t("shopping.saved")}</p> : null}
    </form>
  );
}
