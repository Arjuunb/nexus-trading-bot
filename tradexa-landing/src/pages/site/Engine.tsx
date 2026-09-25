import { useEffect, useRef, useState } from "react";
import { motion, useReducedMotion } from "framer-motion";
import { Cpu, Layers, Network, Terminal, Zap } from "lucide-react";
import { useVisibleActive } from "@/lib/useVisibleActive";
import { EngineBackdrop } from "@/components/site/backdrops";
import {
  PipelineDiagram,
  ArchitectureDiagram,
  STAGES,
} from "@/components/site/engine/PipelineDiagram";
import { DecisionCore } from "@/components/site/engine/DecisionCore";
import { useRouteMeta } from "@/site/seo";
import { routeFor } from "@/site/routes";
import { cn } from "@/lib/utils";

const EASE = [0.22, 1, 0.36, 1] as const;

/**
 * /engine — the decision engine, stage by stage.
 *
 * Palette: graphite under electric blue and cyan. Nothing gold appears above
 * the fold, which is the point — the landing page is warm and this is cold
 * instrumentation, and the reader should feel they have opened a different
 * application rather than scrolled further down the same one.
 */

/**
 * Hero panel in the style of an engine status readout. Every row is a fact
 * about how the engine runs (not a reading). It used to tick through invented
 * throughput — "1840 frames/s", "412 decisions/h", "risk.checks 13/13" —
 * generated in the browser.
 */
function Telemetry() {
  const reduced = useReducedMotion() ?? false;
  const rows = [
    { k: "data", v: "Binance USDⓈ-M", c: "text-electric-soft" },
    { k: "decides.on", v: "closed candles", c: "text-aqua-soft" },
    { k: "strategies", v: "7 prod · 4 research", c: "text-aqua-soft" },
    { k: "quality", v: "score 0–100", c: "text-aqua-soft" },
    { k: "risk", v: "every order", c: "text-emerald-soft" },
    { k: "live.routing", v: "locked", c: "text-white/70" },
  ];

  return (
    <div className="overflow-hidden rounded-2xl border border-graphite-500/70 bg-black/50 backdrop-blur-xl">
      <div className="flex items-center gap-2 border-b border-graphite-600 bg-graphite-800/60 px-4 py-2.5">
        <Terminal className="h-3.5 w-3.5 text-electric-soft" />
        <span className="font-mono text-[11px] text-white/45">nexus-engine · how it runs</span>
        <span className="ml-auto flex items-center gap-1.5 font-mono text-[10px] text-gold-soft">
          <span className="h-1.5 w-1.5 rounded-full bg-gold" />
          paper
        </span>
      </div>
      <div className="grid grid-cols-2 gap-x-6 gap-y-2 p-4 font-mono text-[11px]">
        {rows.map((r, i) => (
          <motion.div
            key={r.k}
            initial={{ opacity: 0, y: reduced ? 0 : 4 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ duration: 0.4, delay: reduced ? 0 : 0.3 + i * 0.06 }}
            className="flex items-baseline justify-between gap-2"
          >
            <span className="text-white/25">{r.k}</span>
            <span className={cn("tabular", r.c)}>{r.v}</span>
          </motion.div>
        ))}
      </div>
      <div className="border-t border-graphite-600 px-4 py-2.5">
        <p className="font-mono text-[10px] leading-relaxed text-white/30">
          <span className="text-electric-soft">▸</span> paper mode · 8-step path ·{" "}
          <span className="text-aqua-soft">every candle written down</span>
          {!reduced && <span className="ml-0.5 inline-block h-3 w-[7px] translate-y-[1px] bg-aqua/70 align-middle motion-safe:animate-caret-blink" />}
        </p>
      </div>
    </div>
  );
}

/** Continuous data-flow strip — the "it never stops" statement, drawn. */
function FlowStrip() {
  const reduced = useReducedMotion() ?? false;
  const ref = useRef<HTMLDivElement>(null);
  // Fourteen packets on four lanes, each an independent infinite spring. They
  // are cheap individually and not cheap together, and none of them mean
  // anything while the strip is off screen.
  const active = useVisibleActive(ref);
  const lanes = [
    { label: "closed candles", color: "#EAB54F", speed: 5.5, count: 5 },
    { label: "signals", color: "#B0B8C4", speed: 7, count: 4 },
    { label: "approved", color: "#F2C766", speed: 9.5, count: 3 },
    { label: "paper fills", color: "#22C55E", speed: 12, count: 2 },
  ];

  return (
    <div ref={ref} className="overflow-hidden rounded-2xl border border-graphite-500/60 bg-graphite-800/50">
      {lanes.map((lane, li) => (
        <div
          key={lane.label}
          className={cn(
            "relative flex h-14 items-center gap-4 px-4",
            li > 0 && "border-t border-graphite-600/70",
          )}
        >
          <span className="z-10 w-28 shrink-0 font-mono text-[10px] uppercase tracking-[0.12em] text-white/30">
            {lane.label}
          </span>
          <div className="relative h-px flex-1 bg-graphite-500">
            {!reduced && active &&
              Array.from({ length: lane.count }).map((_, i) => (
                <motion.span
                  key={i}
                  className="absolute top-1/2 h-6 w-16 -translate-y-1/2 rounded-full"
                  style={{
                    background: `linear-gradient(90deg, transparent, ${lane.color}33, transparent)`,
                  }}
                  initial={{ left: "-10%" }}
                  animate={{ left: ["-10%", "100%"] }}
                  transition={{
                    duration: lane.speed,
                    delay: (i * lane.speed) / lane.count,
                    repeat: Infinity,
                    ease: "linear",
                  }}
                >
                  <span
                    className="absolute right-2 top-1/2 h-1.5 w-1.5 -translate-y-1/2 rounded-full"
                    style={{ background: lane.color, boxShadow: `0 0 10px 1px ${lane.color}` }}
                  />
                </motion.span>
              ))}
          </div>
          <span className="z-10 shrink-0 font-mono text-[10px] tabular text-white/25">
            {["every candle", "some candles", "fewer", "fewest"][li]}
          </span>
        </div>
      ))}
    </div>
  );
}

export default function EnginePage() {
  const route = routeFor("/engine")!;
  useRouteMeta(route);

  const [activeStage, setActiveStage] = useState(STAGES[0].id);
  const [autoAdvance, setAutoAdvance] = useState(true);
  const reduced = useReducedMotion() ?? false;
  const pipelineRef = useRef<HTMLDivElement>(null);
  const pipelineActive = useVisibleActive(pipelineRef);

  // The pipeline walks itself until the reader takes over. A diagram that only
  // moves when clicked reads as static on first sight, and the flow is the
  // thing being explained. It walks only while it is on screen, so a reader
  // who scrolls to the architecture section and back does not return to find
  // it four stages further on than they left it.
  useEffect(() => {
    if (!autoAdvance || reduced || !pipelineActive) return;
    const id = window.setInterval(() => {
      setActiveStage((cur) => {
        const i = STAGES.findIndex((s) => s.id === cur);
        return STAGES[(i + 1) % STAGES.length].id;
      });
    }, 3800);
    return () => window.clearInterval(id);
  }, [autoAdvance, reduced, pipelineActive]);

  const stage = STAGES.find((s) => s.id === activeStage) ?? STAGES[0];

  return (
    <>
      <EngineBackdrop />

      {/* ── Hero: console split ─────────────────────────────────────────── */}
      <section className="container-x pt-32 sm:pt-40">
        <div className="grid gap-10 lg:grid-cols-[1.1fr_0.9fr] lg:items-center">
          <div>
            <motion.div
              initial={{ opacity: 0, x: -12 }}
              animate={{ opacity: 1, x: 0 }}
              transition={{ duration: 0.5, ease: EASE }}
              className="inline-flex items-center gap-2 rounded-full border border-electric/30 bg-electric/[0.08] px-3 py-1"
            >
              <Cpu className="h-3.5 w-3.5 text-electric-soft" />
              <span className="font-mono text-[11px] uppercase tracking-[0.18em] text-electric-soft">
                Nexus Engine
              </span>
            </motion.div>

            <motion.h1
              initial={{ opacity: 0, y: 18 }}
              animate={{ opacity: 1, y: 0 }}
              transition={{ duration: 0.65, delay: 0.06, ease: EASE }}
              className="mt-6 text-balance text-4xl font-extrabold leading-[1.04] tracking-tight text-white sm:text-5xl lg:text-[3.75rem]"
            >
              An operating system
              <br />
              for{" "}
              <span className="bg-electric-sheen bg-clip-text text-transparent">
                trading decisions
              </span>
            </motion.h1>

            <motion.p
              initial={{ opacity: 0, y: 18 }}
              animate={{ opacity: 1, y: 0 }}
              transition={{ duration: 0.65, delay: 0.14, ease: EASE }}
              className="mt-6 max-w-xl text-[17px] leading-relaxed text-white/55"
            >
              Every closed candle takes the same path, in the same order: data, context,
              strategy, quality score, sizing, risk checks, a paper fill and the record. It leaves
              as a paper order or a written reason there wasn’t one — and the reason is kept.
            </motion.p>

            <motion.div
              initial={{ opacity: 0, y: 18 }}
              animate={{ opacity: 1, y: 0 }}
              transition={{ duration: 0.65, delay: 0.2, ease: EASE }}
              className="mt-8 flex flex-wrap gap-x-8 gap-y-3"
            >
              {[
                ["8", "steps, one path"],
                ["7", "production strategies"],
                ["Every", "candle written down"],
              ].map(([v, k]) => (
                <div key={k}>
                  <p className="font-mono text-xl font-semibold tabular text-aqua-soft">{v}</p>
                  <p className="mt-0.5 font-mono text-[10px] uppercase tracking-[0.14em] text-white/30">
                    {k}
                  </p>
                </div>
              ))}
            </motion.div>
          </div>

          <motion.div
            initial={{ opacity: 0, y: 24 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ duration: 0.7, delay: 0.18, ease: EASE }}
          >
            <Telemetry />
          </motion.div>
        </div>
      </section>

      {/* ── Pipeline + inspector ────────────────────────────────────────── */}
      <section className="container-x mt-24 sm:mt-32">
        <div className="flex flex-wrap items-end justify-between gap-4">
          <div>
            <span className="inline-flex items-center gap-2 font-mono text-[11px] uppercase tracking-[0.2em] text-electric-soft">
              <Layers className="h-3.5 w-3.5" />
              The pipeline
            </span>
            <h2 className="mt-3 text-3xl font-bold tracking-tight text-white sm:text-4xl">
              Close to order, one path only
            </h2>
          </div>
          <button
            onClick={() => setAutoAdvance((a) => !a)}
            className="rounded-lg border border-graphite-500 bg-graphite-700/60 px-3 py-1.5 font-mono text-[11px] text-white/45 transition hover:border-electric/40 hover:text-electric-soft"
          >
            {autoAdvance ? "❚❚ pause walk" : "▶ resume walk"}
          </button>
        </div>

        <div ref={pipelineRef} className="mt-10 rounded-3xl border border-graphite-500/60 bg-graphite-800/40 p-5 backdrop-blur-sm sm:p-8">
          <PipelineDiagram
            activeId={activeStage}
            flowing={pipelineActive}
            onSelect={(id) => {
              setActiveStage(id);
              setAutoAdvance(false);
            }}
          />

          <div className="mt-8 grid gap-5 border-t border-graphite-600 pt-8 lg:grid-cols-[1fr_320px]">
            <motion.div
              key={stage.id}
              initial={{ opacity: 0, y: 10 }}
              animate={{ opacity: 1, y: 0 }}
              transition={{ duration: 0.4, ease: EASE }}
            >
              <div className="flex items-center gap-3">
                <span className="font-mono text-xs text-aqua-soft">
                  stage {String(STAGES.findIndex((s) => s.id === stage.id) + 1).padStart(2, "0")}
                </span>
                <h3 className="text-xl font-semibold text-white">{stage.label}</h3>
              </div>
              <p className="mt-3 max-w-2xl leading-relaxed text-white/55">{stage.detail}</p>
            </motion.div>

            <motion.div
              key={`${stage.id}-io`}
              initial={{ opacity: 0, x: 10 }}
              animate={{ opacity: 1, x: 0 }}
              transition={{ duration: 0.4, ease: EASE }}
              className="rounded-xl border border-graphite-600 bg-black/30 p-4 font-mono text-[11px]"
            >
              <div className="flex justify-between gap-3">
                <span className="text-white/25">in</span>
                <span className="text-right text-white/65">{stage.io.in}</span>
              </div>
              <div className="my-2.5 flex items-center gap-2 text-electric-soft">
                <span className="h-px flex-1 bg-graphite-500" />
                <Zap className="h-3 w-3" />
                <span className="h-px flex-1 bg-graphite-500" />
              </div>
              <div className="flex justify-between gap-3">
                <span className="text-white/25">out</span>
                <span className="text-right text-white/65">{stage.io.out}</span>
              </div>
              <div className="mt-4 flex justify-between border-t border-graphite-600 pt-3">
                <span className="text-white/25">records</span>
                <span className="tabular text-aqua-soft">{stage.budget}</span>
              </div>
            </motion.div>
          </div>
        </div>
      </section>

      {/* ── Decision engine ─────────────────────────────────────────────── */}
      <section className="container-x mt-24 sm:mt-32">
        <div className="max-w-2xl">
          <span className="inline-flex items-center gap-2 font-mono text-[11px] uppercase tracking-[0.2em] text-aqua-soft">
            <Network className="h-3.5 w-3.5" />
            Decision engine
          </span>
          <h2 className="mt-3 text-3xl font-bold tracking-tight text-white sm:text-4xl">
            One quality score. Every factor written down.
          </h2>
          <p className="mt-4 leading-relaxed text-white/55">
            The strategy decides where a trade could be; the Decision Brain decides whether it is
            worth taking. It scores the setup out of 100 from eight weighted factors, keeps the
            list of rules it passed and failed, and refuses some setups outright whatever they
            score.
          </p>
        </div>

        <div className="mt-10">
          <DecisionCore />
        </div>
      </section>

      {/* ── Data flow ───────────────────────────────────────────────────── */}
      <section className="container-x mt-24 sm:mt-32">
        <div className="grid gap-8 lg:grid-cols-[0.85fr_1.15fr] lg:items-center">
          <div>
            <span className="font-mono text-[11px] uppercase tracking-[0.2em] text-electric-soft">
              Data flow
            </span>
            <h2 className="mt-3 text-3xl font-bold tracking-tight text-white sm:text-4xl">
              Always on, and always narrow
            </h2>
            <p className="mt-4 leading-relaxed text-white/55">
              Every closed candle on every running instance is read and judged. Most end as WAIT
              with a written reason; a signal must then clear the quality score and every risk
              check before it becomes a paper order. The funnel narrows by design — the engine's
              job is mostly to decide against doing something.
            </p>
          </div>
          <FlowStrip />
        </div>
      </section>

      {/* ── Architecture ────────────────────────────────────────────────── */}
      <section className="container-x mt-24 pb-24 sm:mt-32">
        <div className="max-w-2xl">
          <span className="font-mono text-[11px] uppercase tracking-[0.2em] text-aqua-soft">
            Architecture
          </span>
          <h2 className="mt-3 text-3xl font-bold tracking-tight text-white sm:text-4xl">
            One path to an order
          </h2>
          <p className="mt-4 leading-relaxed text-white/55">
            Nexus runs as one application with a worker per Trading Instance, all fed by a single
            shared Binance hub. The consequence that matters is the one on the right: nothing
            reaches the broker without passing the risk checks, and the broker is paper — live
            order routing is locked.
          </p>
        </div>

        <div className="mt-10 rounded-3xl border border-graphite-500/60 bg-graphite-800/40 p-5 backdrop-blur-sm sm:p-8">
          <ArchitectureDiagram />
          <div className="mt-6 flex flex-wrap gap-x-6 gap-y-2 border-t border-graphite-600 pt-4 font-mono text-[10px] text-white/30">
            {[
              ["#26262B", "data in · fills out"],
              ["#EAB54F", "decision"],
              ["#22C55E", "guard"],
              ["#B0B8C4", "storage"],
            ].map(([c, l]) => (
              <span key={l} className="inline-flex items-center gap-2">
                <span className="h-2 w-2 rounded-sm" style={{ background: c }} />
                {l}
              </span>
            ))}
          </div>
        </div>
      </section>
    </>
  );
}
