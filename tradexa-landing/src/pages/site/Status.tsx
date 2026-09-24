import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { motion } from "framer-motion";
import { Activity, CheckCircle2, Rss } from "lucide-react";
import { StatusBackdrop } from "@/components/site/backdrops";
import { useRouteMeta } from "@/site/seo";
import { routeFor, prefetchRoute } from "@/site/routes";
import { cn } from "@/lib/utils";

const EASE = [0.22, 1, 0.36, 1] as const;

/**
 * /status — operational health, measured.
 *
 * Everything on this page comes from GET /status/public, which the backend's
 * status monitor (automation-hub/services/status_monitor.py) fills by checking
 * each component once a minute. There is no hard-coded service list, uptime
 * figure or incident here: before monitoring began a day shows as "no data",
 * and if the feed cannot be reached the page says so instead of guessing.
 *
 * This used to be an illustrative mock-up — seeded random uptime bars and
 * three invented incidents — clearly labelled as such.
 */

type State = "operational" | "degraded" | "outage" | "unknown";

interface Day { date: string; state: State; uptime: number | null }
interface Component {
  id: string; name: string; description: string; state: State; detail: string;
  since: string | null; uptime_pct: number | null; days: Day[];
}
interface Incident {
  id: number; component_name: string; state: State; detail: string;
  started_at: string; ended_at: string | null; duration_min: number; ongoing: boolean;
}
interface StatusFeed {
  overall: State; generated_at: string; last_sample_at: string | null;
  monitoring_since: string | null; components: Component[]; incidents: Incident[];
}

const META: Record<State, { label: string; dot: string; text: string; bar: string }> = {
  operational: { label: "Operational", dot: "bg-emerald", text: "text-emerald-soft", bar: "bg-emerald/45 hover:bg-emerald" },
  degraded: { label: "Degraded", dot: "bg-gold", text: "text-gold-soft", bar: "bg-gold/60 hover:bg-gold" },
  outage: { label: "Outage", dot: "bg-loss", text: "text-loss-soft", bar: "bg-loss/70 hover:bg-loss" },
  unknown: { label: "No data", dot: "bg-white/25", text: "text-white/40", bar: "bg-white/[0.07] hover:bg-white/15" },
};

const HEADLINE: Record<State, [string, string]> = {
  operational: ["All systems operational", "Every service is within its normal operating range."],
  degraded: ["Some services degraded", "At least one service is running below normal. Details are below."],
  outage: ["Service disruption", "At least one service is not working. Details are below."],
  unknown: ["Status unknown", "The monitor has not reported recently, so the current state cannot be confirmed."],
};

function useStatusFeed() {
  const [feed, setFeed] = useState<StatusFeed | null>(null);
  const [failed, setFailed] = useState(false);
  useEffect(() => {
    let alive = true;
    const load = async () => {
      try {
        const res = await fetch("/status/public", { headers: { Accept: "application/json" } });
        if (!res.ok) throw new Error(String(res.status));
        const body = (await res.json()) as StatusFeed;
        if (!body || !Array.isArray(body.components)) throw new Error("bad shape");
        if (alive) { setFeed(body); setFailed(false); }
      } catch {
        if (alive) setFailed(true);
      }
    };
    void load();
    const id = window.setInterval(() => { if (!document.hidden) void load(); }, 60_000);
    return () => { alive = false; window.clearInterval(id); };
  }, []);
  return { feed, failed };
}

function formatDate(iso: string) {
  return new Date(iso).toLocaleDateString("en-GB", { day: "numeric", month: "short", year: "numeric" });
}

function formatTime(iso: string) {
  return new Date(iso).toLocaleString("en-GB", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit", timeZoneName: "short" });
}

function ago(iso: string | null) {
  if (!iso) return "never";
  const s = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  if (s < 90) return "just now";
  if (s < 3600) return `${Math.round(s / 60)} min ago`;
  return `${Math.round(s / 3600)} h ago`;
}

function duration(min: number) {
  if (min < 60) return `${Math.max(1, min)} min`;
  const h = Math.floor(min / 60), m = min % 60;
  return m ? `${h} h ${m} min` : `${h} h`;
}

function UptimeBars({ days }: { days: Day[] }) {
  return (
    <div className="flex h-8 items-end gap-[2px]">
      {days.map((d) => (
        <span
          key={d.date}
          title={`${formatDate(d.date)} · ${META[d.state].label}${d.uptime !== null ? ` · ${d.uptime}% measured uptime` : ""}`}
          className={cn("h-full flex-1 rounded-[1px] transition-all duration-200 hover:scale-y-110", META[d.state].bar)}
        />
      ))}
    </div>
  );
}

export default function StatusPage() {
  const route = routeFor("/status")!;
  useRouteMeta(route);
  const { feed, failed } = useStatusFeed();

  const overall: State = feed ? feed.overall : "unknown";
  const [title, subtitle] = feed ? HEADLINE[overall]
    : failed ? ["Status could not be loaded", "The status feed did not answer. If the rest of the site works, the service behind it may be restarting — try again in a minute."]
      : ["Checking status…", "Asking the monitor for the current state of every service."];
  const tone = !feed ? "neutral" : overall === "operational" ? "good" : overall === "unknown" ? "neutral" : "bad";

  return (
    <>
      <StatusBackdrop healthy={tone !== "bad"} />

      {/* headline verdict */}
      <section className="container-x pt-32 sm:pt-40">
        <motion.div initial={{ opacity: 0, y: 14 }} animate={{ opacity: 1, y: 0 }} transition={{ duration: 0.55, ease: EASE }}>
          <span className="inline-flex items-center gap-2 font-mono text-[11px] uppercase tracking-[0.22em] text-white/35">
            <Activity className="h-3.5 w-3.5" />
            System status
          </span>

          <div
            className={cn(
              "mt-6 flex flex-wrap items-center gap-4 rounded-2xl border p-6 sm:p-7",
              tone === "good" && "border-emerald/30 bg-emerald/[0.06]",
              tone === "bad" && (overall === "outage" ? "border-loss/30 bg-loss/[0.06]" : "border-gold/30 bg-gold/[0.06]"),
              tone === "neutral" && "border-white/10 bg-white/[0.03]",
            )}
            aria-live="polite"
          >
            <span className="relative flex h-3 w-3 shrink-0">
              {tone === "good" && <span className="absolute inline-flex h-full w-full rounded-full bg-emerald opacity-70 motion-safe:animate-ping-ring" />}
              <span className={cn("relative inline-flex h-3 w-3 rounded-full", META[overall].dot)} />
            </span>
            <div className="min-w-0">
              <h1 className="text-2xl font-bold tracking-tight text-white sm:text-3xl">{title}</h1>
              <p className="mt-1.5 text-sm text-white/50">{subtitle}</p>
            </div>
          </div>

          <p className="mt-4 max-w-2xl text-sm leading-relaxed text-white/35">
            Each service is checked every minute by the platform's own monitor, and every figure on
            this page comes from those checks.
            {feed?.monitoring_since && <> Monitoring began on {formatDate(feed.monitoring_since)}; earlier days show as no data.</>}
            {feed && <> Last check {ago(feed.last_sample_at)}.</>}
          </p>
        </motion.div>
      </section>

      {/* services */}
      <section className="container-x mt-12">
        <div className="overflow-hidden rounded-2xl border border-white/[0.08] bg-black/40 backdrop-blur-sm">
          {!feed && (
            <div className="p-6 text-sm text-white/40">
              {failed ? "Service states are unavailable until the status feed answers." : "Loading service states…"}
            </div>
          )}
          {feed?.components.map((s, i) => {
            const meta = META[s.state];
            return (
              <motion.div
                key={s.id}
                initial={{ opacity: 0, y: 8 }}
                whileInView={{ opacity: 1, y: 0 }}
                viewport={{ once: true, margin: "-40px" }}
                transition={{ duration: 0.4, delay: i * 0.05, ease: EASE }}
                className={cn("p-5 transition-colors hover:bg-white/[0.02] sm:p-6", i > 0 && "border-t border-white/[0.06]")}
              >
                <div className="flex flex-wrap items-center justify-between gap-3">
                  <div className="flex min-w-0 items-center gap-3">
                    <span className={cn("h-2 w-2 shrink-0 rounded-full", meta.dot)} />
                    <div className="min-w-0">
                      <p className="text-[15px] font-medium text-white">{s.name}</p>
                      <p className="font-mono text-[11px] text-white/30">{s.description}</p>
                    </div>
                  </div>
                  <div className="flex shrink-0 items-center gap-5">
                    <span className="font-mono text-[11px] tabular text-white/40" title="Measured uptime over the days shown">
                      {s.uptime_pct !== null ? `${s.uptime_pct === 100 ? "100" : s.uptime_pct.toFixed(2)}%` : "—"}
                    </span>
                    <span className={cn("font-mono text-[11px]", meta.text)}>{meta.label}</span>
                  </div>
                </div>
                <p className="mt-2 pl-5 text-[13px] text-white/45">{s.detail}</p>
                <div className="mt-4">
                  <UptimeBars days={s.days} />
                  <div className="mt-1.5 flex justify-between font-mono text-[9px] text-white/20">
                    <span>{s.days.length} days ago</span>
                    <span>today</span>
                  </div>
                </div>
              </motion.div>
            );
          })}
        </div>
      </section>

      {/* incidents */}
      <section className="container-x mt-14 pb-24">
        <div className="grid gap-10 lg:grid-cols-[1fr_260px] lg:gap-16">
          <div>
            <h2 className="text-2xl font-bold tracking-tight text-white">Incident history</h2>
            <p className="mt-2 max-w-2xl text-sm leading-relaxed text-white/45">
              An incident opens when a service has been unhealthy for two checks in a row, and
              closes after two healthy ones, so a single slow minute is not reported as an outage.
              Times are when the monitor first and last saw the problem.
            </p>

            {feed && feed.incidents.length === 0 && (
              <p className="mt-8 rounded-xl border border-white/[0.08] bg-black/30 p-5 text-sm text-white/45">
                No incidents recorded
                {feed.monitoring_since ? ` since monitoring began on ${formatDate(feed.monitoring_since)}` : ""}.
              </p>
            )}

            {feed && feed.incidents.length > 0 && (
              <ol className="relative mt-8 space-y-6 pl-8">
                <span aria-hidden className="absolute bottom-2 left-[7px] top-2 w-px bg-gradient-to-b from-white/15 to-transparent" />
                {feed.incidents.map((inc, i) => (
                  <motion.li
                    key={inc.id}
                    initial={{ opacity: 0, x: -8 }}
                    whileInView={{ opacity: 1, x: 0 }}
                    viewport={{ once: true, margin: "-60px" }}
                    transition={{ duration: 0.45, delay: Math.min(i, 6) * 0.05, ease: EASE }}
                    className="relative"
                  >
                    <span
                      className={cn(
                        "absolute -left-8 top-1.5 h-[15px] w-[15px] rounded-full border-2 border-[#050708]",
                        inc.ongoing ? (inc.state === "outage" ? "bg-loss" : "bg-gold") : "bg-emerald",
                      )}
                    />
                    <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
                      <time dateTime={inc.started_at} className="font-mono text-[11px] text-white/30">{formatTime(inc.started_at)}</time>
                      <span className="font-mono text-[11px] text-white/25">
                        · {inc.ongoing ? `ongoing, ${duration(inc.duration_min)} so far` : duration(inc.duration_min)}
                      </span>
                    </div>
                    <h3 className="mt-1 text-[15px] font-semibold text-white">
                      {inc.component_name} — {META[inc.state].label.toLowerCase()}
                      {inc.ongoing && <span className="ml-2 rounded-md border border-gold/40 bg-gold/10 px-1.5 py-0.5 align-middle font-mono text-[10px] uppercase tracking-wider text-gold-soft">open</span>}
                    </h3>
                    <p className="mt-2 max-w-2xl text-sm leading-relaxed text-white/50">{inc.detail}</p>
                  </motion.li>
                ))}
              </ol>
            )}
          </div>

          <aside className="lg:sticky lg:top-24 lg:self-start">
            <div className="rounded-2xl border border-white/[0.08] bg-black/40 p-5">
              <h3 className="flex items-center gap-2 text-[14px] font-semibold text-white">
                <Rss className="h-4 w-4 text-emerald-soft" />
                Get notified
              </h3>
              <p className="mt-2 text-sm leading-relaxed text-white/45">
                When an incident opens, worsens or closes, the platform sends it to the alert
                channels connected in the dashboard — Telegram, Discord or email — so nobody has to
                watch this page.
              </p>
              <Link
                to="/support"
                onPointerEnter={() => prefetchRoute("/support")}
                className="mt-4 inline-flex items-center gap-1.5 text-[13px] text-emerald-soft underline-offset-4 hover:underline"
              >
                Report something not shown here →
              </Link>
            </div>

            <div className="mt-3 rounded-2xl border border-white/[0.08] bg-black/40 p-5">
              <h3 className="flex items-center gap-2 text-[14px] font-semibold text-white">
                <CheckCircle2 className="h-4 w-4 text-emerald-soft" />
                What a halt means
              </h3>
              <p className="mt-2 text-sm leading-relaxed text-white/45">
                The system fails closed. If a risk check cannot run, or market data arrives late,
                new trades stop rather than continue unchecked — that is the design working, not
                breaking. Open positions keep being managed to their stops and targets.
              </p>
              <Link
                to="/security"
                onPointerEnter={() => prefetchRoute("/security")}
                className="mt-4 inline-flex items-center gap-1.5 text-[13px] text-emerald-soft underline-offset-4 hover:underline"
              >
                How the architecture guarantees this →
              </Link>
            </div>
          </aside>
        </div>
      </section>
    </>
  );
}
