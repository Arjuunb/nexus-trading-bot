import { useEffect, useState } from "react";
import { motion } from "framer-motion";
import { cn } from "@/lib/utils";

/**
 * Forward paper records the owner chose to publish, from GET
 * /public/track-record (automation-hub/services/public_track_record.py).
 *
 * Every figure comes from a Trading Instance's own ledger: closed paper
 * trades after simulated fees, as a percentage of the paper account. Nothing
 * here is typed in, rounded up or filled in when the feed is empty — an empty
 * feed says so.
 */

interface Point { t: string | null; index: number }

interface PaperRecordEntry {
  id: string;
  strategy: string;
  strategy_version: string;
  symbol: string;
  timeframe: string;
  fill_model: string;
  risk_per_trade_pct: number;
  state: string;
  session_number: number;
  session_started_at: string | null;
  closed_trades: number;
  wins: number;
  losses: number;
  win_rate_pct: number;
  profit_factor: number | null;
  return_pct: number;
  max_drawdown_pct: number;
  open_positions: number | null;
  last_trade_at: string | null;
  sample_note: string | null;
  equity_index: Point[];
}

interface PaperRecordFeed {
  generated_at: string | null;
  instances: PaperRecordEntry[];
  note: string;
}

const EASE = [0.22, 1, 0.36, 1] as const;

function usePaperRecord() {
  const [feed, setFeed] = useState<PaperRecordFeed | null>(null);
  const [failed, setFailed] = useState(false);
  useEffect(() => {
    let alive = true;
    const load = async () => {
      try {
        const res = await fetch("/public/track-record", { headers: { Accept: "application/json" } });
        if (!res.ok) throw new Error(String(res.status));
        const body = (await res.json()) as PaperRecordFeed;
        if (!body || !Array.isArray(body.instances)) throw new Error("bad shape");
        if (alive) { setFeed(body); setFailed(false); }
      } catch {
        if (alive) setFailed(true);
      }
    };
    void load();
    const id = window.setInterval(() => { if (!document.hidden) void load(); }, 120_000);
    return () => { alive = false; window.clearInterval(id); };
  }, []);
  return { feed, failed };
}

function date(iso: string | null) {
  return iso ? new Date(iso).toLocaleDateString("en-GB", { day: "numeric", month: "short", year: "numeric" }) : "—";
}

function signed(value: number) {
  return `${value > 0 ? "+" : ""}${value.toFixed(2)}%`;
}

function Curve({ points }: { points: Point[] }) {
  if (points.length < 2) {
    return <div className="flex h-20 items-center text-xs text-white/35">No closed trade yet</div>;
  }
  const values = points.map((p) => p.index);
  const lo = Math.min(100, ...values), hi = Math.max(100, ...values);
  const span = hi - lo || 1;
  const x = (i: number) => (i / (points.length - 1)) * 300;
  const y = (v: number) => 76 - ((v - lo) / span) * 72;
  const d = points.map((p, i) => `${i ? "L" : "M"}${x(i).toFixed(1)},${y(p.index).toFixed(1)}`).join(" ");
  const up = values[values.length - 1] >= 100;
  return (
    <svg viewBox="0 0 300 80" className="h-20 w-full" preserveAspectRatio="none" role="img"
      aria-label={`Paper equity index from 100 to ${values[values.length - 1].toFixed(2)}`}>
      <line x1="0" x2="300" y1={y(100)} y2={y(100)} stroke="rgba(255,255,255,0.12)" strokeDasharray="3 4" />
      <path d={d} fill="none" stroke={up ? "#4ADE80" : "#F87171"} strokeWidth="1.6" vectorEffect="non-scaling-stroke" />
    </svg>
  );
}

function Stat({ label, value, tone }: { label: string; value: string; tone?: "up" | "down" }) {
  return (
    <div>
      <div className="font-mono text-[10px] uppercase tracking-wider text-white/40">{label}</div>
      <div className={cn("mt-1 font-mono text-[15px] font-semibold tabular text-white",
        tone === "up" && "text-emerald-soft", tone === "down" && "text-loss-soft")}>{value}</div>
    </div>
  );
}

export default function PaperRecord() {
  const { feed, failed } = usePaperRecord();

  if (!feed) {
    return (
      <p className="max-w-3xl rounded-2xl border border-white/[0.08] bg-white/[0.02] p-5 text-sm text-white/50">
        {failed ? "The paper record could not be loaded right now." : "Loading the paper record…"}
      </p>
    );
  }
  if (!feed.instances.length) {
    return (
      <p className="max-w-3xl rounded-2xl border border-white/[0.08] bg-white/[0.02] p-5 text-sm leading-relaxed text-white/50">
        No forward paper record has been published yet. When one is, it appears here straight from
        the instance's ledger — trade count, win rate, return and drawdown — with the sample size
        beside it.
      </p>
    );
  }

  return (
    <>
      <div className="grid gap-4 lg:grid-cols-2">
        {feed.instances.map((r, i) => (
          <motion.article
            key={r.id}
            initial={{ opacity: 0, y: 12 }}
            whileInView={{ opacity: 1, y: 0 }}
            viewport={{ once: true, margin: "-40px" }}
            transition={{ duration: 0.45, delay: i * 0.06, ease: EASE }}
            className="rounded-2xl border border-white/[0.08] bg-white/[0.02] p-5"
          >
            <div className="flex flex-wrap items-start justify-between gap-2">
              <div>
                <h3 className="text-[15px] font-semibold text-white">{r.strategy} <span className="font-mono text-xs text-white/40">v{r.strategy_version}</span></h3>
                <p className="mt-1 font-mono text-xs text-white/45">{r.symbol} · {r.timeframe} · {r.risk_per_trade_pct}% risk per trade</p>
              </div>
              <span className="rounded-md border border-gold/40 bg-gold/10 px-2 py-0.5 font-mono text-[10px] uppercase tracking-wider text-gold-soft">
                Paper · simulated fills
              </span>
            </div>
            <div className="mt-4"><Curve points={r.equity_index} /></div>
            <div className="mt-4 grid grid-cols-3 gap-4 sm:grid-cols-5">
              <Stat label="Closed trades" value={String(r.closed_trades)} />
              <Stat label="Win rate" value={`${r.win_rate_pct.toFixed(1)}%`} />
              <Stat label="Return" value={signed(r.return_pct)} tone={r.return_pct > 0 ? "up" : r.return_pct < 0 ? "down" : undefined} />
              <Stat label="Max drawdown" value={`${r.max_drawdown_pct.toFixed(2)}%`} />
              <Stat label="Profit factor" value={r.profit_factor === null ? "—" : r.profit_factor.toFixed(2)} />
            </div>
            <p className="mt-4 text-xs leading-relaxed text-white/40">
              Since {date(r.session_started_at)}
              {r.session_number > 1 ? ` · account session ${r.session_number} (the paper account was reset ${r.session_number - 1}×)` : ""}
              {" · "}last closed trade {date(r.last_trade_at)} · {r.state}
              {r.open_positions ? ` · ${r.open_positions} open position${r.open_positions === 1 ? "" : "s"} not counted until closed` : ""}
            </p>
            {r.sample_note && <p className="mt-2 text-xs font-medium text-gold-soft">{r.sample_note}</p>}
          </motion.article>
        ))}
      </div>
      <p className="mt-4 max-w-3xl text-sm leading-relaxed text-white/45">{feed.note}</p>
    </>
  );
}
