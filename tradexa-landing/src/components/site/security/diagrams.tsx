import { useEffect, useRef, useState } from "react";
import { motion, useReducedMotion } from "framer-motion";
import { Check, Lock, X } from "lucide-react";
import { useVisibleActive } from "@/lib/useVisibleActive";
import { cn } from "@/lib/utils";

/**
 * Security diagrams.
 *
 * Security copy is unusually easy to write and unusually hard to believe, so
 * each of these draws a *mechanism* rather than a reassurance: where the key
 * physically is, which boundary a request crosses, what the log chains to.
 * A claim you can trace is worth more than a claim you can read.
 */

/* ── Envelope encryption ─────────────────────────────────────────────── */

const ENVELOPE_STEPS = [
  { label: "API secret", note: "entered once, in the browser", cls: "border-white/15 text-white/70" },
  { label: "Data key", note: "unique per tenant", cls: "border-aqua/40 text-aqua-soft" },
  { label: "Master key", note: "server environment · never in a database or backup", cls: "border-emerald/45 text-emerald-soft" },
  { label: "Ciphertext at rest", note: "useless without both keys", cls: "border-emerald/45 text-emerald-soft" },
];

export function EnvelopeDiagram() {
  const reduced = useReducedMotion() ?? false;
  const ref = useRef<HTMLDivElement>(null);
  const active = useVisibleActive(ref);
  const [step, setStep] = useState(reduced ? ENVELOPE_STEPS.length - 1 : 0);

  useEffect(() => {
    if (reduced || !active) return;
    const id = window.setInterval(
      () => setStep((s) => (s + 1) % (ENVELOPE_STEPS.length + 1)),
      1700,
    );
    return () => window.clearInterval(id);
  }, [reduced, active]);

  return (
    <div ref={ref} className="rounded-2xl border border-navy-500/60 bg-navy-800/70 p-5 backdrop-blur-sm sm:p-6">
      <div className="flex items-center gap-2">
        <Lock className="h-3.5 w-3.5 text-emerald-soft" />
        <h3 className="font-mono text-[10px] uppercase tracking-[0.18em] text-white/45">
          Envelope encryption
        </h3>
      </div>

      {/* nested boxes — each wraps the previous, which is the whole idea */}
      <div className="mt-5">
        {ENVELOPE_STEPS.map((s, i) => {
          const reached = i <= step;
          return (
            <motion.div
              key={s.label}
              animate={{
                opacity: reached ? 1 : 0.25,
                borderColor: reached ? undefined : "rgba(255,255,255,0.06)",
              }}
              transition={{ duration: 0.45 }}
              className={cn(
                "rounded-xl border p-3",
                s.cls,
                i > 0 && "mt-0 border-t-0 rounded-t-none",
              )}
              // The nesting step is deliberately small: at 14px each side the
              // innermost box lost 84px of width, and its label and note then
              // set a min-content wider than a phone's content column, which
              // scrolled the whole page sideways.
              style={{ marginLeft: i * 8, marginRight: i * 8 }}
            >
              <div className="flex flex-wrap items-baseline justify-between gap-x-3 gap-y-0.5">
                <span className="text-[13px] font-medium">{s.label}</span>
                <span className="font-mono text-[9px] text-white/30">{s.note}</span>
              </div>
              {i === 3 && (
                <p className="mt-2 truncate font-mono text-[10px] text-emerald-soft/50">
                  a7f3·9c21·4e8b·d05a·6f7e·13c9·88b2·e4d1
                </p>
              )}
            </motion.div>
          );
        })}
      </div>

      <p className="mt-5 border-t border-navy-500/60 pt-3 text-xs leading-relaxed text-white/45">
        The secret is decrypted only in memory, for the moment it is needed. It is never written
        to a log, never returned by an API, and never visible in the product again after it is
        entered — the dashboard shows a four-character hint.
      </p>
    </div>
  );
}

/* ── API key permission matrix ───────────────────────────────────────── */

const PERMISSIONS: [string, boolean, string][] = [
  ["Read account", true, "accepted"],
  ["Spot and margin trading", true, "accepted · unused while live routing is locked"],
  ["Futures trading", true, "accepted · unused while live routing is locked"],
  ["Withdraw funds", false, "refused before the key is stored"],
  ["Internal transfer", false, "refused before the key is stored"],
  ["Universal transfer", false, "refused before the key is stored"],
];

export function PermissionMatrix() {
  return (
    <div className="overflow-hidden rounded-2xl border border-navy-500/60 bg-navy-800/70 backdrop-blur-sm">
      <div className="border-b border-navy-500/60 px-5 py-3">
        <h3 className="font-mono text-[10px] uppercase tracking-[0.18em] text-white/45">
          Key scope, asked of the exchange
        </h3>
      </div>
      <ul className="divide-y divide-navy-600/60">
        {PERMISSIONS.map(([label, allowed, note], i) => (
          <motion.li
            key={label}
            initial={{ opacity: 0, x: -6 }}
            whileInView={{ opacity: 1, x: 0 }}
            viewport={{ once: true, margin: "-40px" }}
            transition={{ duration: 0.35, delay: i * 0.04 }}
            className="flex items-center gap-3 px-5 py-2.5"
          >
            <span
              className={cn(
                "flex h-5 w-5 shrink-0 items-center justify-center rounded-md border",
                allowed
                  ? "border-emerald/45 bg-emerald/12 text-emerald-soft"
                  : "border-loss/40 bg-loss/10 text-loss-soft",
              )}
            >
              {allowed ? <Check className="h-3 w-3" strokeWidth={3} /> : <X className="h-3 w-3" strokeWidth={3} />}
            </span>
            <span className="min-w-0 flex-1 truncate text-[13px] text-white/75">{label}</span>
            <span className="shrink-0 font-mono text-[10px] text-white/30">{note}</span>
          </motion.li>
        ))}
      </ul>
      <p className="border-t border-navy-500/60 px-5 py-3 text-xs leading-relaxed text-white/45">
        The exchange is asked what the key may do before it is stored, and a key that can move
        funds out is refused rather than merely unused — as is one whose permissions the exchange
        will not confirm. Binance only, the one venue connected today.
      </p>
    </div>
  );
}

/* ── Zero-trust boundaries ───────────────────────────────────────────── */

interface Zone {
  id: string;
  label: string;
  detail: string;
  r: number;
  color: string;
}

const ZONES: Zone[] = [
  {
    id: "public",
    label: "Public edge",
    detail: "nginx terminates TLS (1.2 and 1.3; TLS 1.3 only on the API host) and forwards to the app. It holds no trading secret and never touches a database.",
    r: 118,
    color: "#1B3050",
  },
  {
    id: "session",
    label: "Authenticated request",
    detail: "Every request needs a signed session cookie, a scoped API key or the control key — optional TOTP on sign-in, rate limits on sign-in and webhooks. The source address is never a credential.",
    r: 92,
    color: "#13456B",
  },
  {
    id: "service",
    label: "Role and scope",
    detail: "Authenticated is not authorised: API keys carry read or control scope, and accounts without the operator role cannot reach the trading engine at all.",
    r: 66,
    color: "#12695A",
  },
  {
    id: "exec",
    label: "Key vault",
    detail: "Exchange secrets are unwrapped only in memory, only when they are needed, and responses and logs pass a redaction filter before anything is written or returned.",
    r: 38,
    color: "#1E9457",
  },
];

export function ZeroTrustDiagram() {
  const [active, setActive] = useState<string>("exec");
  const zone = ZONES.find((z) => z.id === active)!;

  return (
    <div className="grid gap-6 rounded-2xl border border-navy-500/60 bg-navy-800/70 p-5 backdrop-blur-sm sm:p-7 lg:grid-cols-[minmax(0,320px)_1fr] lg:items-center">
      <svg viewBox="0 0 280 280" className="mx-auto w-full max-w-[300px]" role="img" aria-label="Concentric trust boundaries from the public edge to the key vault">
        {ZONES.map((z) => {
          const on = z.id === active;
          // Hover only. An `onFocus` here would never fire — a bare <g> is not
          // focusable — and pretending otherwise hides the fact that the
          // buttons beside the diagram are what make it keyboard-operable.
          return (
            <g key={z.id} onMouseEnter={() => setActive(z.id)}>
              <circle
                cx="140"
                cy="140"
                r={z.r}
                fill={on ? `${z.color}44` : `${z.color}1f`}
                stroke={z.color}
                strokeWidth={on ? 2 : 1}
                strokeDasharray={z.id === "public" ? "5 4" : undefined}
                className="cursor-pointer transition-all duration-300"
              />
            </g>
          );
        })}
        {/* labels sit on the ring they name */}
        {ZONES.map((z) => (
          <text
            key={z.id}
            x="140"
            y={140 - z.r + 14}
            textAnchor="middle"
            fill={z.id === active ? "#fff" : "rgba(255,255,255,0.4)"}
            className="pointer-events-none font-mono"
            style={{ fontSize: 8.5 }}
          >
            {z.label}
          </text>
        ))}
        <text x="140" y="144" textAnchor="middle" fill="#4FD98E" className="pointer-events-none" style={{ fontSize: 9, fontWeight: 600 }}>
          keys
        </text>
      </svg>

      <div>
        <div className="flex flex-wrap gap-1.5">
          {ZONES.map((z) => (
            <button
              key={z.id}
              onClick={() => setActive(z.id)}
              className={cn(
                "rounded-lg border px-2.5 py-1 font-mono text-[10px] transition-colors",
                z.id === active
                  ? "border-emerald/45 bg-emerald/10 text-emerald-soft"
                  : "border-navy-500 text-white/35 hover:text-white/70",
              )}
            >
              {z.label}
            </button>
          ))}
        </div>
        <motion.div
          key={zone.id}
          initial={{ opacity: 0, y: 8 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ duration: 0.35 }}
          className="mt-4"
        >
          <h3 className="text-lg font-semibold text-white">{zone.label}</h3>
          <p className="mt-2 leading-relaxed text-white/55">{zone.detail}</p>
        </motion.div>
        <p className="mt-5 border-t border-navy-600 pt-3 font-mono text-[10px] leading-relaxed text-white/30">
          The platform runs as one application behind nginx, so these are checks inside it rather
          than separate services — each one still refuses a caller the previous one let through.
        </p>
      </div>
    </div>
  );
}

/* ── Audit log ───────────────────────────────────────────────────────── */

// Illustrative rows in the shape the real log stores (services/audit_log.py).
const AUDIT_ROWS = [
  { t: "14:02:11", actor: "you", action: "settings.update", from: "daily loss 3.0%", to: "2.5%", hash: "9c21f0" },
  { t: "13:47:52", actor: "you", action: "key.attach", from: "…Qx7a", to: "…M2c9", hash: "4e8bd0" },
  { t: "11:20:04", actor: "you", action: "POST /controls/pause-all", from: "—", to: "200", hash: "a7f39c" },
  { t: "09:58:33", actor: "api-key:research", action: "POST /v1/strategies/…/promote", from: "—", to: "409", hash: "13c988" },
  { t: "09:14:07", actor: "tradingview-webhook", action: "POST /webhook", from: "—", to: "200", hash: "6f7e13" },
];

export function AuditLog() {
  const reduced = useReducedMotion() ?? false;

  return (
    <div className="overflow-hidden rounded-2xl border border-navy-500/60 bg-navy-800/70 backdrop-blur-sm">
      <div className="flex items-center justify-between border-b border-navy-500/60 px-5 py-3">
        <h3 className="font-mono text-[10px] uppercase tracking-[0.18em] text-white/45">
          Append-only audit log
        </h3>
        <span className="flex items-center gap-1.5 font-mono text-[10px] text-emerald-soft">
          <span className={cn("h-1.5 w-1.5 rounded-full bg-emerald", !reduced && "motion-safe:animate-pulse")} />
          chain intact
        </span>
      </div>

      <div className="overflow-x-auto">
        <table className="w-full min-w-[620px] border-collapse font-mono text-[10px]">
          <thead>
            <tr className="border-b border-navy-600 text-left uppercase tracking-wider text-white/25">
              {["time", "actor", "action", "from", "to", "prev hash"].map((h) => (
                <th key={h} className="px-5 py-2 font-normal">
                  {h}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {AUDIT_ROWS.map((r, i) => (
              <motion.tr
                key={r.hash}
                initial={{ opacity: 0 }}
                whileInView={{ opacity: 1 }}
                viewport={{ once: true }}
                transition={{ duration: 0.3, delay: i * 0.05 }}
                className="border-b border-navy-600/50 last:border-0"
              >
                <td className="px-5 py-2 text-white/30">{r.t}</td>
                <td className="px-5 py-2 text-white/60">{r.actor}</td>
                <td className="px-5 py-2 text-emerald-soft">{r.action}</td>
                <td className="px-5 py-2 text-white/35">{r.from}</td>
                <td className="px-5 py-2 text-white/70">{r.to}</td>
                <td className="px-5 py-2 text-white/25">{r.hash}…</td>
              </motion.tr>
            ))}
          </tbody>
        </table>
      </div>

      <p className="border-t border-navy-500/60 px-5 py-3 text-xs leading-relaxed text-white/45">
        Illustrative rows. Each entry carries the hash of the one before it, so a deleted or edited
        row breaks the chain and the check in Settings → Security finds the break. Database
        triggers refuse updates and deletes, and nothing in the product exposes a way to amend an
        entry.
      </p>
    </div>
  );
}

/* ── Deployment ──────────────────────────────────────────────────────── */

const HOST = [
  { code: "edge", title: "nginx", role: "TLS, HTTP to HTTPS, the API host", note: "Certificates renew in place" },
  { code: "app", title: "One application", role: "Engine, dashboard, /v1 API, webhooks", note: "Single worker, restarts on failure" },
  { code: "data", title: "SQLite on a volume", role: "Databases, vault, audit log, backups", note: "Daily backups · encrypted with the vault key when set" },
];

export function DeploymentMap() {
  return (
    <div className="grid gap-3 sm:grid-cols-3">
      {HOST.map((r, i) => (
        <motion.div
          key={r.code}
          initial={{ opacity: 0, y: 14 }}
          whileInView={{ opacity: 1, y: 0 }}
          viewport={{ once: true, margin: "-60px" }}
          transition={{ duration: 0.45, delay: i * 0.07 }}
          className="relative overflow-hidden rounded-2xl border border-navy-500/60 bg-navy-800/70 p-5"
        >
          <span className="absolute inset-x-5 top-0 h-px bg-emerald/50" />
          <p className="font-mono text-[10px] uppercase tracking-[0.16em] text-emerald-soft">
            {r.code}
          </p>
          <p className="mt-2 text-lg font-semibold text-white">{r.title}</p>
          <p className="mt-1 text-[13px] text-white/50">{r.role}</p>
          <p className="mt-4 border-t border-navy-600 pt-3 font-mono text-[10px] text-white/30">
            {r.note}
          </p>
        </motion.div>
      ))}
    </div>
  );
}
