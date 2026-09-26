/**
 * Platform facts shown in the footer.
 *
 * ─────────────────────────────────────────────────────────────────────────
 * `PLATFORM_STATS` used to be operating metrics written by hand — "99.98%
 * uptime, trailing 90 days", "3,400+ active accounts", "48.2M decisions
 * processed since launch" — shown in the footer as if measured. Nothing
 * produced them, so they are gone.
 *
 * What is here instead are properties of how the platform is built, each one
 * true in the code today (automation-hub): live order routing is locked on
 * every venue (services/broker.py), forward-paper decisions use closed candles
 * only, every candle gets a decision report including WAIT, and the live feed
 * is Binance USD-M. If real telemetry is wired later, measured numbers can
 * come back — with their source — in this same shape.
 * ─────────────────────────────────────────────────────────────────────────
 */

export interface PlatformStat {
  /** Short label under the value. */
  label: string;
  /** The number itself, already formatted. */
  value: string;
  /** Optional qualifier — the period or scope the number covers. */
  note: string;
}

export const PLATFORM_STATS: PlatformStat[] = [
  { label: "Execution mode", value: "Paper", note: "live order routing locked by design" },
  { label: "Candles traded on", value: "Closed", note: "the forming candle never trades" },
  { label: "Candles explained", value: "Every", note: "a decision report, WAIT included" },
  { label: "Live market data", value: "Binance", note: "USDⓈ-M futures feed" },
];

/**
 * Venues, and which of them is live.
 *
 * The one list every venue mention on the site reads — the footer, the
 * landing status bar and the Features card — so they cannot disagree again.
 * They did: the footer and status bar showed Bybit and OKX as connected while
 * the Connectivity section said, correctly, that they are on the roadmap.
 *
 * `live` means Nexus streams live market data from the venue today. Order
 * execution is paper on every venue, including Binance; a venue's live
 * routing unlocks only when its integration passes safety review.
 */
export interface Venue {
  name: string;
  live: boolean;
}

export const VENUES: Venue[] = [
  { name: "Binance", live: true },
  { name: "Bybit", live: false },
  { name: "OKX", live: false },
  { name: "Hyperliquid", live: false },
  { name: "Coinbase", live: false },
];

/**
 * Security properties, shown as badges.
 *
 * These are architectural facts about the product, each of which is explained
 * on /security — not third-party certifications. Nothing here claims SOC 2,
 * ISO 27001 or a penetration-test attestation, because none of those have been
 * awarded, and a badge asserting one would be a fabricated credential rather
 * than a design flourish. If and when an audit is completed, add it here with
 * a link to the report.
 */
export interface TrustBadge {
  label: string;
  detail: string;
}

export const TRUST_BADGES: TrustBadge[] = [
  { label: "AES-256 envelope", detail: "Per-tenant data keys under a master key kept out of the database" },
  { label: "TLS in transit", detail: "TLS 1.2 and 1.3 on the site; TLS 1.3 only on the API host" },
  { label: "Withdrawal-disabled", detail: "Keys that can withdraw or transfer funds are refused before storage" },
  { label: "Append-only audit", detail: "Hash-chained; no product path can amend an entry" },
  { label: "No implicit trust", detail: "Every request authenticates, whatever address it comes from" },
];

/** The project's public repository. Used by the developer pages. */
export const REPO_URL = "https://github.com/Arjuunb/nexus-trading-bot";
