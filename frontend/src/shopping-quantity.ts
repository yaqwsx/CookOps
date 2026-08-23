import { formatLocalizedDecimal } from "./localized-decimal";

export function formatShoppingQuantity(
  value: string,
  unit: string,
  locale: string,
): string {
  return `${formatLocalizedDecimal(value, locale) ?? value} ${unit}`;
}
