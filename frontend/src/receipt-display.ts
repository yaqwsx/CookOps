const calendarDate = /^(\d{4})-(\d{2})-(\d{2})$/;
const decimal = /^(?:0|[1-9]\d*)(?:\.\d+)?$/;
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
  if (!decimal.test(amount) || !currency.test(currencyCode))
    return `${amount} ${currencyCode}`;
  try {
    const formatter = new Intl.NumberFormat(locale, {
      style: "currency",
      currency: currencyCode,
      maximumFractionDigits: 0,
    });
    const [integer, fraction] = amount.split(".");
    const parts = formatter.formatToParts(BigInt(integer));
    if (fraction) {
      const fractionFormatter = new Intl.NumberFormat(locale, {
        style: "currency",
        currency: currencyCode,
        minimumFractionDigits: 1,
        maximumFractionDigits: 1,
      });
      const decimal = fractionFormatter.formatToParts(1.1).find((part) => part.type === "decimal")?.value;
      if (!decimal) return `${amount} ${currencyCode}`;
      const localizedFraction = [...fraction].map((digit) =>
        fractionFormatter.formatToParts(BigInt(digit)).find((part) => part.type === "integer")?.value ?? digit,
      ).join("");
      let integerEnd = -1;
      parts.forEach((part, index) => { if (part.type === "integer") integerEnd = index; });
      parts.splice(integerEnd + 1, 0, { type: "decimal", value: decimal }, { type: "fraction", value: localizedFraction });
    }
    return parts.map((part) => part.value).join("");
  } catch {
    return `${amount} ${currencyCode}`;
  }
}
