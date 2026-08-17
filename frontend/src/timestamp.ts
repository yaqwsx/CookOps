export function timestampNanoseconds(value: string): bigint | undefined {
  const match = /^(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.(\d+))?(Z|[+-]\d\d:\d\d)$/.exec(value);
  if (!match || (match[2]?.length ?? 0) > 9) return undefined;
  const milliseconds = Date.parse(`${match[1]}${match[3]}`);
  return Number.isFinite(milliseconds)
    ? BigInt(milliseconds) * 1_000_000n + BigInt((match[2] ?? "").padEnd(9, "0"))
    : undefined;
}
