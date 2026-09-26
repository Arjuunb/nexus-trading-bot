// Display formatting for exact decimal amounts sent by the backend as strings
// ("125.42000000"). Amounts are rounded here for display only, digit by digit
// (half-up, like the backend), never through a float. Each amount keeps its
// own settlement currency: USDT is shown as USDT, never as dollars.

const SYMBOL: Record<string, string> = { USD: "$", GBP: "£", EUR: "€", JPY: "¥" };
const PLACES: Record<string, number> = { JPY: 0, BTC: 6, ETH: 6, BNB: 4 };

export const currencyPlaces = (currency: string): number => PLACES[currency] ?? 2;

interface Rounded { negative: boolean; nonZero: boolean; whole: string; fraction: string }

/** Round a decimal string to `places` without floating point. */
export function roundDecimal(value: string, places: number): Rounded | null {
  const m = /^([+-])?(\d+)(?:\.(\d+))?$/.exec(value.trim());
  if (!m) return null;
  const fractionRaw = m[3] ?? "";
  const nonZero = /[1-9]/.test(m[2] + fractionRaw);
  const digits = BigInt(m[2] + fractionRaw.padEnd(places + 1, "0").slice(0, places + 1));
  const ten = BigInt(10);
  let scaled = digits / ten;
  if (digits % ten >= BigInt(5)) scaled += BigInt(1);
  const text = scaled.toString().padStart(places + 1, "0");
  return {
    negative: m[1] === "-" && nonZero,
    nonZero,
    whole: text.slice(0, text.length - places).replace(/\B(?=(\d{3})+(?!\d))/g, ","),
    fraction: places ? text.slice(-places) : "",
  };
}

/**
 * `+£125.42`, `-$40.00`, `+125.42 USDT`, `£0.00`.
 * `signed` (default) prefixes + for profit and - for loss; zero has no sign.
 * The sign follows the exact value, so a tiny profit still reads as a profit
 * even when it rounds to 0.00. Unsigned mode shows a magnitude.
 */
export function formatMoney(value: string | null | undefined, currency: string,
  { signed = true }: { signed?: boolean } = {}): string {
  if (value == null || value === "") return "Not recorded";
  const r = roundDecimal(value, currencyPlaces(currency));
  if (!r) return value;
  const sign = !signed || !r.nonZero ? "" : r.negative ? "-" : "+";
  const number = r.fraction ? `${r.whole}.${r.fraction}` : r.whole;
  if (currency === "UNKNOWN") return `${sign}${number} (currency not recorded)`;
  const symbol = SYMBOL[currency];
  return symbol ? `${sign}${symbol}${number}` : `${sign}${number} ${currency}`;
}

/** Price or quantity: exact string trimmed of trailing zeros, grouped. */
export function formatDecimal(value: string | null | undefined): string {
  if (value == null || value === "") return "Not recorded";
  const m = /^([+-])?(\d+)(?:\.(\d+))?$/.exec(value.trim());
  if (!m) return value;
  const fraction = (m[3] ?? "").replace(/0+$/, "");
  const whole = m[2].replace(/\B(?=(\d{3})+(?!\d))/g, ",");
  return `${m[1] === "-" ? "-" : ""}${whole}${fraction ? `.${fraction}` : ""}`;
}

/** Sign of an exact decimal string: 1, -1 or 0. */
export function decimalSign(value: string | null | undefined): number {
  if (!value) return 0;
  const r = roundDecimal(value, 8);
  if (!r || !r.nonZero) return 0;
  return r.negative ? -1 : 1;
}
