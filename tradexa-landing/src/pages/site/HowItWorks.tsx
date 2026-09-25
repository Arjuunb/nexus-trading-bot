import { useRef, useState } from "react";
import { motion, useReducedMotion, useScroll, useTransform } from "framer-motion";
import { ArrowDown } from "lucide-react";
import { JourneyBackdrop } from "@/components/site/backdrops";
import { STAGES, StageVisual } from "@/components/site/how/stages";
import { useRouteMeta } from "@/site/seo";
import { routeFor } from "@/site/routes";
import { cn } from "@/lib/utils";

const EASE = [0.22, 1, 0.36, 1] as const;

/**
 * /how-it-works — the journey, told by scrolling.
 *
 * The only page on the site whose layout is driven by scroll position rather
 * than by clicks. A pipeline is a sequence, and a sequence explained by a grid
 * of seven cards asks the reader to reconstruct the order themselves. Here the
 * order is the interaction: the diagram is pinned, the narrative moves past it,
 * and the ambient colour of the whole page migrates from blue to gold as the
 * trade progresses from venue to journal.
 *
 * The pinned column is `position: sticky` rather than a scroll-driven
 * transform, which means it costs nothing per frame and behaves correctly when
 * the reader jumps with the scrollbar or lands on an anchor.
 */
export default function HowItWorksPage() {
  const route = routeFor("/how-it-works")!;
  useRouteMeta(route);

  const reduced = useReducedMotion() ?? false;
  const [active, setActive] = useState(0);

  // Which stage the pinned diagram shows is decided by each narrative section
  // announcing itself as it enters the middle band of the screen, not by
  // slicing overall scroll progress into seven equal parts. Progress across
  // the container runs from "top of the first section hits the top of the
  // viewport" to "bottom of the last hits the bottom", so an even split sits
  // about half a viewport behind what the reader is actually looking at — the
  // diagram showed Exchange while the copy said Analysis.
  const stage = STAGES[active];

  // Hero parallax: the stage rail drifts up as the hero leaves.
  const heroRef = useRef<HTMLElement>(null);
  const { scrollYProgress: heroProgress } = useScroll({
    target: heroRef,
    offset: ["start start", "end start"],
  });
  const heroY = useTransform(heroProgress, [0, 1], [0, -32]);

  return (
    <>
      <JourneyBackdrop color={stage.color} />

      {/* ── Hero: the whole journey at a glance ─────────────────────────── */}
      <section ref={heroRef} className="container-x flex min-h-[86vh] flex-col justify-center pt-28">
        <motion.div style={reduced ? undefined : { y: heroY }}>
          <motion.span
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            transition={{ duration: 0.6 }}
            className="font-mono text-[11px] uppercase tracking-[0.26em] text-white/35"
          >
            How it works
          </motion.span>

          <motion.h1
            initial={{ opacity: 0, y: 20 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ duration: 0.7, delay: 0.06, ease: EASE }}
            className="mt-6 max-w-4xl text-balance text-4xl font-extrabold leading-[1.05] tracking-tight text-white sm:text-5xl lg:text-[4rem]"
          >
            One trade, from the exchange
            <br className="hidden sm:block" /> to the record it leaves behind.
          </motion.h1>

          <motion.p
            initial={{ opacity: 0, y: 20 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ duration: 0.7, delay: 0.14, ease: EASE }}
            className="mt-6 max-w-2xl text-[17px] leading-relaxed text-white/55"
          >
            Seven stages, in this order, every time. Scroll and the diagram follows — each stage
            is drawn as it is described, and the page changes colour as the trade moves through
            it.
          </motion.p>

          {/* the seven-stage rail */}
          <motion.ol
            initial="hidden"
            animate="shown"
            variants={{ shown: { transition: { staggerChildren: 0.07, delayChildren: 0.2 } } }}
            className="mt-14 grid grid-cols-2 gap-3 sm:grid-cols-4 lg:grid-cols-7"
          >
            {STAGES.map((s) => (
              <motion.li
                key={s.id}
                variants={{
                  hidden: { opacity: 0, y: 16 },
                  shown: { opacity: 1, y: 0, transition: { duration: 0.5, ease: EASE } },
                }}
                className="group relative rounded-xl border border-white/[0.07] bg-black/30 p-3"
              >
                <span
                  className="absolute inset-x-3 top-0 h-px opacity-70"
                  style={{ background: s.color }}
                />
                <s.icon className="h-4 w-4" style={{ color: s.color }} />
                <p className="mt-2.5 font-mono text-[9px] text-white/25">{s.n}</p>
                <p className="text-[13px] font-medium text-white/85">{s.label}</p>
              </motion.li>
            ))}
          </motion.ol>

          {!reduced && (
            <motion.div
              initial={{ opacity: 0 }}
              animate={{ opacity: 1 }}
              transition={{ delay: 1 }}
              className="mt-14 flex items-center gap-2 text-white/30"
            >
              <motion.span
                animate={{ y: [0, 5, 0] }}
                transition={{ duration: 1.8, repeat: Infinity, ease: "easeInOut" }}
              >
                <ArrowDown className="h-4 w-4" />
              </motion.span>
              <span className="font-mono text-[10px] uppercase tracking-[0.2em]">
                Scroll to begin
              </span>
            </motion.div>
          )}
        </motion.div>
      </section>

      {/* ── Scroll narrative ────────────────────────────────────────────── */}
      <div className="container-x relative mt-16">
        <div className="grid gap-10 lg:grid-cols-[minmax(0,1fr)_minmax(0,1fr)] lg:gap-16">
          {/* pinned diagram + progress rail */}
          <div className="hidden lg:block">
            <div className="sticky top-24 space-y-5">
              <StageVisual stage={stage} active />

              {/* progress rail */}
              <ol className="flex gap-1.5">
                {STAGES.map((s, i) => (
                  <li key={s.id} className="flex-1">
                    <button
                      onClick={() => {
                        const el = document.getElementById(`stage-${s.id}`);
                        el?.scrollIntoView({ behavior: reduced ? "auto" : "smooth", block: "center" });
                      }}
                      className="group block w-full text-left"
                      aria-label={`Jump to ${s.label}`}
                    >
                      <span
                        className="block h-[3px] rounded-full transition-all duration-500"
                        style={{
                          background: i <= active ? s.color : "rgba(255,255,255,0.09)",
                          opacity: i === active ? 1 : i < active ? 0.5 : 1,
                        }}
                      />
                      <span
                        className={cn(
                          "mt-2 block truncate font-mono text-[9px] uppercase tracking-wider transition-colors",
                          i === active ? "text-white/70" : "text-white/25 group-hover:text-white/45",
                        )}
                      >
                        {s.label}
                      </span>
                    </button>
                  </li>
                ))}
              </ol>
            </div>
          </div>

          {/* the narrative itself */}
          <div>
            {STAGES.map((s, i) => (
              <section
                key={s.id}
                id={`stage-${s.id}`}
                className="flex min-h-[88vh] flex-col justify-center py-16 lg:min-h-screen lg:py-0"
              >
                <motion.div
                  initial={{ opacity: 0, y: 24 }}
                  whileInView={{ opacity: 1, y: 0 }}
                  // The band is deliberately narrow: exactly one section can
                  // occupy the middle 30% of the screen at a time, so the
                  // pinned diagram never flickers between two stages.
                  viewport={{ once: false, margin: "-35% 0px -35% 0px" }}
                  onViewportEnter={() => setActive(i)}
                  transition={{ duration: 0.6, ease: EASE }}
                >
                  <div className="flex items-center gap-3">
                    <span
                      className="flex h-9 w-9 items-center justify-center rounded-lg border"
                      style={{ borderColor: `${s.color}66`, background: `${s.color}14`, color: s.color }}
                    >
                      <s.icon className="h-4 w-4" />
                    </span>
                    <span className="font-mono text-[11px] uppercase tracking-[0.22em] text-white/30">
                      Stage {s.n} · {s.label}
                    </span>
                  </div>

                  <h2 className="mt-6 text-balance text-3xl font-bold leading-tight tracking-tight text-white sm:text-4xl">
                    {s.headline}
                  </h2>

                  <p className="mt-5 max-w-xl text-[17px] leading-relaxed text-white/55">{s.body}</p>

                  {/* on phones the pinned column is hidden, so the diagram
                      travels inline with its own stage instead */}
                  <div className="mt-8 lg:hidden">
                    <StageVisual stage={s} active={i === active} />
                  </div>

                  <dl className="mt-8 grid gap-3 sm:grid-cols-3">
                    {s.facts.map(([k, v]) => (
                      <div key={k} className="rounded-xl border border-white/[0.07] bg-black/25 p-3">
                        <dt className="font-mono text-[9px] uppercase tracking-[0.14em] text-white/25">
                          {k}
                        </dt>
                        <dd className="mt-1 text-[13px] text-white/75">{v}</dd>
                      </div>
                    ))}
                  </dl>
                </motion.div>
              </section>
            ))}
          </div>
        </div>
      </div>

      {/* ── The loop closes ────────────────────────────────────────────── */}
      <section className="container-x pb-28 pt-8">
        <motion.div
          initial={{ opacity: 0, y: 20 }}
          whileInView={{ opacity: 1, y: 0 }}
          viewport={{ once: true, margin: "-80px" }}
          transition={{ duration: 0.6, ease: EASE }}
          className="relative overflow-hidden rounded-3xl border border-white/[0.08] bg-black/40 p-8 backdrop-blur-sm sm:p-12"
        >
          <div
            aria-hidden
            className="pointer-events-none absolute inset-0 opacity-70"
            style={{
              background:
                "linear-gradient(100deg, rgba(62,123,214,0.10), rgba(34,211,238,0.07) 40%, rgba(201,162,75,0.10) 100%)",
            }}
          />
          <div className="relative">
            <span className="font-mono text-[11px] uppercase tracking-[0.22em] text-white/35">
              And then it starts again
            </span>
            <h2 className="mt-4 max-w-2xl text-balance text-3xl font-bold tracking-tight text-white sm:text-4xl">
              What feeds back, and what does not
            </h2>
            <p className="mt-4 max-w-2xl leading-relaxed text-white/55">
              What feeds back on its own is size, never rules. After every closed trade an
              instance re-reads its own record: a losing pattern that repeats — on a symbol, in a
              regime, in one direction — trades smaller, never below half size; two losses in a
              row halve risk and four quarter it; and trading below its recent equity average
              halves it again. A pattern that has proven itself may add at most 25%, and only
              when nothing else is cutting size. Lessons lapse after 14 days unless newer trades
              confirm them. A strategy's rules, its risk settings and where it trades change only
              when a person changes them.
            </p>

            <ol className="mt-8 flex flex-wrap items-center gap-x-2 gap-y-3">
              {STAGES.map((s, i) => (
                <li key={s.id} className="flex items-center gap-2">
                  <span
                    className="rounded-lg border px-2.5 py-1 font-mono text-[10px]"
                    style={{ borderColor: `${s.color}55`, color: s.color, background: `${s.color}0f` }}
                  >
                    {s.label}
                  </span>
                  {i < STAGES.length - 1 && <span className="text-white/20">→</span>}
                </li>
              ))}
              <li className="flex items-center gap-2">
                <span className="text-white/20">↺</span>
                <span className="font-mono text-[10px] text-white/35">back to 03</span>
              </li>
            </ol>
          </div>
        </motion.div>
      </section>
    </>
  );
}
