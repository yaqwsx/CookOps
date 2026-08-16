export type Fraction = { numerator: bigint; denominator: bigint };

export function multiply(left: Fraction, right: Fraction): Fraction {
  return {
    numerator: left.numerator * right.numerator,
    denominator: left.denominator * right.denominator,
  };
}

export function divide(left: Fraction, right: Fraction): Fraction | undefined {
  if (right.numerator === 0n) return undefined;
  return {
    numerator: left.numerator * right.denominator,
    denominator: left.denominator * right.numerator,
  };
}

/** Display a rounded advisory monetary value without converting it to a JS number. */
export function money(value: Fraction): string {
  const sign = value.numerator < 0n ? "-" : "";
  const absolute =
    value.numerator < 0n ? { ...value, numerator: -value.numerator } : value;
  const scale = 100n;
  const rounded =
    (absolute.numerator * scale * 2n + absolute.denominator) /
    (absolute.denominator * 2n);
  return `${sign}${rounded / scale}.${(rounded % scale).toString().padStart(2, "0")}`;
}
