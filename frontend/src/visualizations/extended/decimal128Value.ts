/**
 * Independent, bounded BSON Decimal128 BID/text validation. Compatibility:
 * https://github.com/mongodb/mongo-python-driver/blob/4.17.0/bson/decimal128.py
 * https://github.com/mongodb/specifications/blob/master/source/bson-decimal128/decimal128.md
 * All coefficient arithmetic is exact BigInt; supplied text is never parsed.
 */
function canonicalDecimal128(bid: string): string | undefined {
  let bits = 0n;
  // Exactly sixteen bytes, least-significant byte first; no attacker-sized
  // BigInt strings or loops are admitted by the public validator.
  for (let index = 15; index >= 0; index--) {
    bits = (bits << 8n) | BigInt(`0x${bid.slice(index * 2, index * 2 + 2)}`);
  }
  const low = bits & 0xffffffffffffffffn;
  const high = bits >> 64n;
  const sign = (high & 0x8000000000000000n) !== 0n ? '-' : '';
  // Signed/signaling/payload NaNs all use the pinned driver's "NaN" text.
  if ((high & 0x7c00000000000000n) === 0x7c00000000000000n) return 'NaN';
  if ((high & 0x7800000000000000n) === 0x7800000000000000n) return `${sign}Infinity`;
  let exponent: number;
  let coefficient: bigint;
  if ((high & 0x6000000000000000n) === 0x6000000000000000n) {
    // Reserved finite coefficient form: signed zero at the encoded exponent.
    exponent = Number((high & 0x1fffe00000000000n) >> 47n) - 6176;
    coefficient = 0n;
  } else {
    exponent = Number((high & 0x7fff800000000000n) >> 49n) - 6176;
    coefficient = ((high & 0x0001ffffffffffffn) << 64n) | low;
    // Match precision-34 driver context: only an exact zero-ending reduction
    // is valid. Other 35-digit coefficients signal Inexact, never rounding.
    if (coefficient >= 10n ** 34n) {
      if (coefficient % 10n !== 0n) return undefined;
      coefficient /= 10n;
      exponent++;
    }
  }
  if (exponent < -6176 || exponent > 6111) return undefined;
  const digits = coefficient.toString();
  const adjusted = exponent + digits.length - 1;
  let rendered: string;
  if (exponent <= 0 && adjusted >= -6) {
    const point = digits.length + exponent;
    if (exponent === 0) rendered = digits;
    else if (point > 0) rendered = `${digits.slice(0, point)}.${digits.slice(point)}`;
    else rendered = `0.${'0'.repeat(-point)}${digits}`;
  } else {
    const mantissa = digits[0] + (digits.length > 1 ? `.${digits.slice(1)}` : '');
    rendered = `${mantissa}E${adjusted >= 0 ? '+' : '-'}${Math.abs(adjusted)}`;
  }
  return sign + rendered;
}

export function validateDecimal128(value: unknown, bid: unknown): boolean {
  if (typeof value !== 'string' || value.length < 1 || value.length > 50) return false;
  if (typeof bid !== 'string' || bid.length !== 32 || !/^[0-9a-f]{32}$/.test(bid)) return false;
  return value === canonicalDecimal128(bid);
}
