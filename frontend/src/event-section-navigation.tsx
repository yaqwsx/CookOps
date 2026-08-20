import { useTranslation } from "react-i18next";

type EventSection = "planner" | "shopping" | "costs" | "receipts" | "settings";

export function EventSectionNavigation({
  organizationId,
  eventId,
  current,
}: {
  organizationId: string;
  eventId: string;
  current: EventSection;
}) {
  const { t } = useTranslation();
  const base = `/organizations/${organizationId}/events/${eventId}`;
  const sections: EventSection[] = ["planner", "shopping", "costs", "receipts", "settings"];
  return (
    <nav aria-label={t("eventNavigation.label")}>
      {sections.map((section) => (
        <a
          aria-current={section === current ? "page" : undefined}
          href={`${base}/${section}`}
          key={section}
        >
          {t(`eventNavigation.${section}`)}
        </a>
      ))}
    </nav>
  );
}
