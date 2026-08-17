import { liveQuery } from "dexie";
import { useEffect, useState } from "react";

import {
  queueCatalogConfiguration,
  type CatalogKind,
} from "./catalog-configuration";
import { readVisibleRecords } from "./visible-records";
import { mealRoleLabels } from "./meal-role-labels";

const kinds: CatalogKind[] = [
  "recipe_tag",
  "dietary_tag",
  "store_section",
  "unit_definition",
  "organization_meal_role_preset",
];
const labels: Record<CatalogKind, { cs: string; en: string }> = {
  recipe_tag: { cs: "Štítky receptů", en: "Recipe tags" },
  dietary_tag: { cs: "Dietní štítky", en: "Dietary tags" },
  store_section: { cs: "Oddělení obchodu", en: "Store sections" },
  unit_definition: { cs: "Vlastní jednotky", en: "Custom units" },
  organization_meal_role_preset: { cs: "Role jídel", en: "Meal roles" },
};
export function CatalogAdministration({
  userId,
  organizationId,
  locale,
}: {
  userId: string;
  organizationId: string;
  locale: "cs" | "en";
}) {
  const [records, setRecords] = useState<
    Record<CatalogKind, Awaited<ReturnType<typeof readVisibleRecords>>>
  >({
    recipe_tag: [],
    dietary_tag: [],
    store_section: [],
    unit_definition: [],
    organization_meal_role_preset: [],
  });
  useEffect(() => {
    const subscription = liveQuery(
      async () =>
        Object.fromEntries(
          await Promise.all(
            kinds.map(async (kind) => [
              kind,
              await readVisibleRecords(userId, organizationId, kind, true),
            ]),
          ),
        ) as Record<
          CatalogKind,
          Awaited<ReturnType<typeof readVisibleRecords>>
        >,
    ).subscribe({ next: setRecords });
    return () => subscription.unsubscribe();
  }, [userId, organizationId]);
  return (
    <section
      aria-label={
        locale === "cs" ? "Správa katalogu" : "Catalog administration"
      }
    >
      <h3>{locale === "cs" ? "Správa katalogu" : "Catalog administration"}</h3>
      {kinds.map((kind) => (
        <CatalogGroup
          key={kind}
          kind={kind}
          label={labels[kind][locale]}
          records={(kind === "store_section" || kind === "organization_meal_role_preset" ? [...records[kind]].sort((a, b) => String(a.fields.position_key ?? "").localeCompare(String(b.fields.position_key ?? "")) || a.entityId.localeCompare(b.entityId)) : records[kind])}
          userId={userId}
          organizationId={organizationId}
          locale={locale}
        />
      ))}
    </section>
  );
}

function CatalogGroup({
  kind,
  label,
  records,
  userId,
  organizationId,
  locale,
}: {
  kind: CatalogKind;
  label: string;
  records: Awaited<ReturnType<typeof readVisibleRecords>>;
  userId: string;
  organizationId: string;
  locale: "cs" | "en";
}) {
  const [name, setName] = useState("");
  const [color, setColor] = useState("#336699");
  const add = async (event: React.FormEvent) => {
    event.preventDefault();
    if (!name.trim()) return;
    await queueCatalogConfiguration(userId, organizationId, kind, "create", {
      name,
      ...(kind === "recipe_tag" || kind === "dietary_tag" ? { color } : {}),
      ...(kind === "store_section" ? { position_key: "z" } : {}),
      ...(kind === "organization_meal_role_preset" ? { position_key: "z" } : {}),
      ...(kind === "unit_definition"
        ? { allows_ingredient_quantity: true, allows_recipe_scaling: true }
        : {}),
    });
    setName("");
  };
  return (
    <section>
      <h4>{label}</h4>
      <form onSubmit={(event) => void add(event)}>
        <label>
          {locale === "cs" ? "Název" : "Name"}
          <input
            required
            maxLength={200}
            value={name}
            onChange={(event) => setName(event.target.value)}
          />
        </label>
        {(kind === "recipe_tag" || kind === "dietary_tag") && (
          <label>
            {locale === "cs" ? "Barva" : "Color"}
            <input
              type="color"
              value={color}
              onChange={(event) => setColor(event.target.value)}
            />
          </label>
        )}
        <button type="submit">{locale === "cs" ? "Přidat" : "Add"}</button>
      </form>
      <ul>
        {records.map((record) => (
          <CatalogRow
            key={record.entityId}
            kind={kind}
            locale={locale}
            organizationId={organizationId}
            record={record}
            userId={userId}
          />
        ))}
      </ul>
    </section>
  );
}

function CatalogRow({
  kind,
  locale,
  organizationId,
  record,
  userId,
}: {
  kind: CatalogKind;
  locale: "cs" | "en";
  organizationId: string;
  record: Awaited<ReturnType<typeof readVisibleRecords>>[number];
  userId: string;
}) {
  const [name, setName] = useState(
    String(record.fields.name ?? record.fields.custom_name ?? ""),
  );
  const [position, setPosition] = useState(
    String(record.fields.position_key ?? "z"),
  );
  const [color, setColor] = useState(String(record.fields.color ?? "#336699"));
  const builtInKey = typeof record.fields.built_in_translation_key === "string"
    ? record.fields.built_in_translation_key
    : undefined;
  const displayName = builtInKey
    ? (mealRoleLabels[builtInKey]?.[locale] ?? builtInKey)
    : String(record.fields.name ?? record.fields.custom_name ?? record.fields.code ?? "");
  const save = async (event: React.FormEvent) => {
    event.preventDefault();
    await queueCatalogConfiguration(
      userId,
      organizationId,
      kind,
      "update",
      {
        ...(builtInKey ? { built_in_translation_key: builtInKey } : { name }),
        ...(kind === "store_section" || kind === "organization_meal_role_preset" ? { position_key: position } : {}),
        ...(kind === "recipe_tag" || kind === "dietary_tag" ? { color } : {}),
      },
      record.entityId,
    );
  };
  return (
    <li>
      {displayName}{" "}
      {record.lifecycle === "retired"
        ? `(${locale === "cs" ? "vyřazeno" : "retired"})`
        : ""}
      <button
        type="button"
        onClick={() =>
          void queueCatalogConfiguration(
            userId,
            organizationId,
            kind,
            record.lifecycle === "retired" ? "restore" : "retire",
            {},
            record.entityId,
          )
        }
      >
        {record.lifecycle === "retired"
          ? locale === "cs"
            ? "Obnovit"
            : "Restore"
          : locale === "cs"
            ? "Vyřadit"
            : "Retire"}
      </button>
      {record.lifecycle === "active" && (
        <details>
          <summary>{locale === "cs" ? "Upravit" : "Edit"}</summary>
          <form onSubmit={(event) => void save(event)}>
            {!builtInKey && <label>
              {locale === "cs" ? "Název" : "Name"}
              <input maxLength={200} required value={name} onChange={(event) => setName(event.target.value)} />
            </label>}
            {(kind === "store_section" || kind === "organization_meal_role_preset") && (
              <label>
                {locale === "cs" ? "Pořadí" : "Position"}
                <input
                  required
                  pattern="[0-9A-Za-z]+"
                  value={position}
                  onChange={(event) => setPosition(event.target.value)}
                />
              </label>
            )}
            {(kind === "recipe_tag" || kind === "dietary_tag") && (
              <label>
                {locale === "cs" ? "Barva" : "Color"}
                <input
                  type="color"
                  value={color}
                  onChange={(event) => setColor(event.target.value)}
                />
              </label>
            )}
            <button type="submit">{locale === "cs" ? "Uložit" : "Save"}</button>
          </form>
        </details>
      )}
    </li>
  );
}
