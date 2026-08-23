import { formatLocalizedDecimal } from "./localized-decimal";

const calendarDate = /^(\d{4})-(\d{2})-(\d{2})$/;
const currency = /^[A-Z]{3}$/;

export function isReceiptDate(value: string): boolean {
  const match = calendarDate.exec(value);
  if (!match) return false;
  const date = new Date(`${value}T00:00:00.000Z`);
  return Number.isFinite(date.valueOf()) && date.toISOString().slice(0, 10) === value;
}

export function formatReceiptDate(value: string, locale: string): string {
  if (!isReceiptDate(value)) return value;
  return new Intl.DateTimeFormat(locale, {
    day: "numeric",
    month: "long",
    year: "numeric",
    timeZone: "UTC",
  }).format(new Date(`${value}T00:00:00.000Z`));
}

export function formatReceiptAmount(amount: string, currencyCode: string, locale: string): string {
  if (!currency.test(currencyCode))
    return `${amount} ${currencyCode}`;
  const formatted = formatLocalizedDecimal(amount, locale, {
    style: "currency",
    currency: currencyCode,
  });
  return formatted ?? `${amount} ${currencyCode}`;
}
