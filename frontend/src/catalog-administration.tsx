import { liveQuery } from "dexie";
import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";

import {
  queueCatalogConfiguration,
  type CatalogKind,
} from "./catalog-configuration";
import { readVisibleRecords } from "./visible-records";
import { mealRoleLabels } from "./meal-role-labels";
import "./i18n";

const kinds: CatalogKind[] = [
  "recipe_tag",
  "dietary_tag",
  "store_section",
  "unit_definition",
  "organization_meal_role_preset",
];
const labelKeys: Record<CatalogKind, string> = {
  recipe_tag: "recipeTags",
  dietary_tag: "dietaryTags",
  store_section: "storeSections",
  unit_definition: "customUnits",
  organization_meal_role_preset: "mealRoles",
};
export function CatalogAdministration({
  userId,
  organizationId,
  locale,
  refreshKey = 0,
}: {
  userId: string;
  organizationId: string;
  locale: "cs" | "en";
  refreshKey?: number;
}) {
  const { t } = useTranslation();
  const [records, setRecords] = useState<
    Record<CatalogKind, Awaited<ReturnType<typeof readVisibleRecords>>>
  >({
    recipe_tag: [],
    dietary_tag: [],
    store_section: [],
    unit_definition: [],
    organization_meal_role_preset: [],
  });
  // biome-ignore lint/correctness/useExhaustiveDependencies: requery after bootstrap completion.
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
  }, [userId, organizationId, refreshKey]);
  return (
    <section aria-label={t("catalogAdministration.heading", { lng: locale })}>
      <h3>{t("catalogAdministration.heading", { lng: locale })}</h3>
      {kinds.map((kind) => (
        <CatalogGroup
          key={kind}
          kind={kind}
          label={t(`catalogAdministration.${labelKeys[kind]}`, { lng: locale })}
          records={
            kind === "store_section" || kind === "organization_meal_role_preset"
              ? [...records[kind]].sort(
                  (a, b) =>
                    String(a.fields.position_key ?? "").localeCompare(
                      String(b.fields.position_key ?? ""),
                    ) || a.entityId.localeCompare(b.entityId),
                )
              : records[kind]
          }
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
  const { t } = useTranslation();
  const [name, setName] = useState("");
  const [color, setColor] = useState("#336699");
  const add = async (event: React.FormEvent) => {
    event.preventDefault();
    if (!name.trim()) return;
    await queueCatalogConfiguration(userId, organizationId, kind, "create", {
      name,
      ...(kind === "recipe_tag" || kind === "dietary_tag" ? { color } : {}),
      ...(kind === "store_section" ? { position_key: "z" } : {}),
      ...(kind === "organization_meal_role_preset"
        ? { position_key: "z" }
        : {}),
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
          {t("catalogAdministration.name", { lng: locale })}
          <input
            required
            maxLength={200}
            value={name}
            onChange={(event) => setName(event.target.value)}
          />
        </label>
        {(kind === "recipe_tag" || kind === "dietary_tag") && (
          <label>
            {t("catalogAdministration.color", { lng: locale })}
            <input
              type="color"
              value={color}
              onChange={(event) => setColor(event.target.value)}
            />
          </label>
        )}
        <button type="submit">
          {t("catalogAdministration.add", { lng: locale })}
        </button>
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
  const { t } = useTranslation();
  const [name, setName] = useState(
    String(record.fields.name ?? record.fields.custom_name ?? ""),
  );
  const [position, setPosition] = useState(
    String(record.fields.position_key ?? "z"),
  );
  const [color, setColor] = useState(String(record.fields.color ?? "#336699"));
  const builtInKey =
    typeof record.fields.built_in_translation_key === "string"
      ? record.fields.built_in_translation_key
      : undefined;
  const displayName = builtInKey
    ? (mealRoleLabels[builtInKey]?.[locale] ?? builtInKey)
    : String(
        record.fields.name ??
          record.fields.custom_name ??
          record.fields.code ??
          "",
      );
  const save = async (event: React.FormEvent) => {
    event.preventDefault();
    await queueCatalogConfiguration(
      userId,
      organizationId,
      kind,
      "update",
      {
        ...(builtInKey ? { built_in_translation_key: builtInKey } : { name }),
        ...(kind === "store_section" || kind === "organization_meal_role_preset"
          ? { position_key: position }
          : {}),
        ...(kind === "recipe_tag" || kind === "dietary_tag" ? { color } : {}),
      },
      record.entityId,
    );
  };
  return (
    <li>
      {displayName}{" "}
      {record.lifecycle === "retired"
        ? `(${t("catalogAdministration.retired", { lng: locale })})`
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
          ? t("catalogAdministration.restore", { lng: locale })
          : t("catalogAdministration.retire", { lng: locale })}
      </button>
      {record.lifecycle === "active" && (
        <details>
          <summary>{t("catalogAdministration.edit", { lng: locale })}</summary>
          <form onSubmit={(event) => void save(event)}>
            {!builtInKey && (
              <label>
                {t("catalogAdministration.name", { lng: locale })}
                <input
                  maxLength={200}
                  required
                  value={name}
                  onChange={(event) => setName(event.target.value)}
                />
              </label>
            )}
            {(kind === "store_section" ||
              kind === "organization_meal_role_preset") && (
              <label>
                {t("catalogAdministration.position", { lng: locale })}
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
                {t("catalogAdministration.color", { lng: locale })}
                <input
                  type="color"
                  value={color}
                  onChange={(event) => setColor(event.target.value)}
                />
              </label>
            )}
            <button type="submit">
              {t("catalogAdministration.save", { lng: locale })}
            </button>
          </form>
        </details>
      )}
    </li>
  );
}
