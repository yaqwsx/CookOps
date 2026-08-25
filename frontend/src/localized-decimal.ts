const decimal = /^(?:0|[1-9]\d*)(?:\.\d+)?$/;

export function formatLocalizedDecimal(
  value: string,
  locale: string,
  options: Intl.NumberFormatOptions = {},
): string | undefined {
  if (!decimal.test(value)) return undefined;
  try {
    const formatter = new Intl.NumberFormat(locale, {
      ...options,
      maximumFractionDigits: 0,
    });
    const [integer, fraction] = value.split(".");
    const parts = formatter.formatToParts(BigInt(integer));
    if (fraction) {
      const fractionFormatter = new Intl.NumberFormat(locale, {
        ...options,
        minimumFractionDigits: 1,
        maximumFractionDigits: 1,
      });
      const separator = fractionFormatter
        .formatToParts(1.1)
        .find((part) => part.type === "decimal")?.value;
      if (!separator) return undefined;
      const localizedFraction = [...fraction]
        .map(
          (digit) =>
            fractionFormatter
              .formatToParts(BigInt(digit))
              .find((part) => part.type === "integer")?.value ?? digit,
        )
        .join("");
      let integerEnd = -1;
      parts.forEach((part, index) => {
        if (part.type === "integer") integerEnd = index;
      });
      parts.splice(
        integerEnd + 1,
        0,
        { type: "decimal", value: separator },
        { type: "fraction", value: localizedFraction },
      );
    }
    return parts.map((part) => part.value).join("");
  } catch {
    return undefined;
  }
}
