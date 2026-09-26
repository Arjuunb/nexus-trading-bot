import { useEffect, useMemo, useRef, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { AnimatePresence, motion, useReducedMotion } from "framer-motion";
import { ChevronDown, CornerDownLeft, Search, SlidersHorizontal, X } from "lucide-react";
import { FeaturesBackdrop } from "@/components/site/backdrops";
import { ScreenMock } from "@/components/site/features/ScreenMock";
import {
  CATEGORIES,
  FEATURES,
  countsByCategory,
  matches,
  type CategoryId,
  type FeatureEntry,
} from "@/components/site/features/catalogue";
import { useRouteMeta } from "@/site/seo";
import { routeFor } from "@/site/routes";
import { useVisibleActive } from "@/lib/useVisibleActive";
import { cn } from "@/lib/utils";

const EASE = [0.22, 1, 0.36, 1] as const;

/**
 * /features — the capability explorer.
 *
 * Deliberately not a grid of six marketing cards, which is what the landing
 * page already does well. Someone on this page is evaluating, and evaluating
 * means asking "does it do X" — so the page is built around a search field
 * that answers that in one keystroke, with the full catalogue underneath and
 * every entry expandable into what it actually does.
 *
 * Palette: the shared ink base pushed cool, with signal blue as the primary
 * accent and gold demoted to a highlight — the inverse of the landing page's
 * gold-first treatment.
 */

/** Suggested queries — they double as an admission of what people search for. */
const SUGGESTIONS = ["stop loss", "backtest", "binance", "drawdown", "audit", "slippage"];

function HeroSearch({
  query,
  setQuery,
  inputRef,
  resultCount,
}: {
  query: string;
  setQuery: (v: string) => void;
  inputRef: React.RefObject<HTMLInputElement>;
  resultCount: number;
}) {
  const reduced = useReducedMotion() ?? false;
  const wrapRef = useRef<HTMLDivElement>(null);
  const onScreen = useVisibleActive(wrapRef);
  const [focused, setFocused] = useState(false);
  const [placeholderIndex, setPlaceholderIndex] = useState(0);

  // Cycle the placeholder through real searches. It stops the moment the field
  // is touched — a placeholder that keeps changing while you think about what
  // to type is a distraction, not a hint — and while the field is scrolled
  // out of sight, since nobody is reading a hint they cannot see.
  useEffect(() => {
    if (reduced || focused || query || !onScreen) return;
    const id = window.setInterval(
      () => setPlaceholderIndex((i) => (i + 1) % SUGGESTIONS.length),
      2600,
    );
    return () => window.clearInterval(id);
  }, [reduced, focused, query, onScreen]);

  return (
    <div ref={wrapRef} className="relative">
      <div
        className={cn(
          "relative flex items-center gap-3 rounded-2xl border bg-black/40 px-4 backdrop-blur-xl transition-all duration-300",
          focused
            ? "border-signal/50 shadow-[0_0_40px_-12px_rgba(62,123,214,0.55)]"
            : "border-line hover:border-line-strong",
        )}
      >
        <Search className={cn("h-5 w-5 shrink-0 transition-colors", focused ? "text-signal-soft" : "text-white/30")} />
        <input
          ref={inputRef}
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          onFocus={() => setFocused(true)}
          onBlur={() => setFocused(false)}
          onKeyDown={(e) => e.key === "Escape" && setQuery("")}
          type="search"
          role="searchbox"
          aria-label="Search capabilities"
          placeholder={`Search capabilities — try “${SUGGESTIONS[placeholderIndex]}”`}
          className="h-16 w-full min-w-0 bg-transparent text-base text-white outline-none placeholder:text-white/25 sm:text-lg"
        />
        {query ? (
          <button
            onClick={() => {
              setQuery("");
              inputRef.current?.focus();
            }}
            aria-label="Clear search"
            className="shrink-0 rounded-lg p-1.5 text-white/40 transition hover:bg-white/5 hover:text-white"
          >
            <X className="h-4 w-4" />
          </button>
        ) : (
          <kbd className="hidden shrink-0 rounded border border-line px-1.5 py-0.5 font-mono text-[10px] text-white/30 sm:block">
            /
          </kbd>
        )}
      </div>

      <div className="mt-3 flex flex-wrap items-center gap-2">
        <span className="text-[11px] uppercase tracking-[0.16em] text-white/25">Try</span>
        {SUGGESTIONS.map((s) => (
          <button
            key={s}
            onClick={() => setQuery(s)}
            className={cn(
              "rounded-full border px-2.5 py-1 text-xs transition-all duration-200",
              query === s
                ? "border-signal/50 bg-signal/10 text-signal-soft"
                : "border-line text-white/45 hover:border-line-strong hover:text-white/80",
            )}
          >
            {s}
          </button>
        ))}
        <AnimatePresence>
          {query && (
            <motion.span
              initial={{ opacity: 0, x: -4 }}
              animate={{ opacity: 1, x: 0 }}
              exit={{ opacity: 0 }}
              className="ml-auto font-mono text-xs tabular text-white/40"
              aria-hidden
            >
              {resultCount} / {FEATURES.length}
            </motion.span>
          )}
        </AnimatePresence>
      </div>

      {/*
        Filtering a list in place is silent to a screen reader: the results
        change and nothing announces it. The visible counter above is marked
        aria-hidden and the same fact is stated here as a sentence, because
        "4 / 22" read aloud is not an answer to anything.
      */}
      <div className="sr-only" role="status" aria-live="polite">
        {query
          ? `${resultCount} of ${FEATURES.length} capabilities match ${query}`
          : `Showing all ${FEATURES.length} capabilities`}
      </div>
    </div>
  );
}

/**
 * A single catalogue entry.
 *
 * Expansion is in-place rather than a modal: the comparison someone is making
 * is between features, and a dialog destroys that context every time it opens.
 * The grid-rows 0fr→1fr trick animates to the content's real height without
 * measuring it, so a card with four bullets and a screenshot opens as smoothly
 * as one with two.
 */
function FeatureCard({
  feature,
  expanded,
  onToggle,
  query,
}: {
  feature: FeatureEntry;
  expanded: boolean;
  onToggle: () => void;
  query: string;
}) {
  const [hovered, setHovered] = useState(false);
  const Icon = feature.icon;

  return (
    <motion.article
      layout="position"
      transition={{ duration: 0.35, ease: EASE }}
      onMouseEnter={() => setHovered(true)}
      onMouseLeave={() => setHovered(false)}
      className={cn(
        "group relative overflow-hidden rounded-2xl border bg-ink-700/40 backdrop-blur-sm transition-colors duration-300",
        expanded ? "border-signal/35 bg-ink-700/70" : "border-line hover:border-line-strong",
      )}
    >
      {/* pointer-following highlight — a soft wash that tracks nothing more
          than hover state, so it costs no per-frame work */}
      <div
        aria-hidden
        className={cn(
          "pointer-events-none absolute inset-0 bg-[radial-gradient(60%_60%_at_50%_0%,rgba(62,123,214,0.14),transparent_70%)] transition-opacity duration-500",
          hovered || expanded ? "opacity-100" : "opacity-0",
        )}
      />

      <button
        onClick={onToggle}
        aria-expanded={expanded}
        className="relative flex w-full items-start gap-4 p-5 text-left"
      >
        <span
          className={cn(
            "mt-0.5 flex h-10 w-10 shrink-0 items-center justify-center rounded-xl border transition-all duration-300",
            expanded
              ? "border-signal/40 bg-signal/15 text-signal-soft"
              : "border-line bg-white/[0.03] text-white/50 group-hover:border-signal/30 group-hover:bg-signal/10 group-hover:text-signal-soft",
          )}
        >
          <Icon className="h-5 w-5 transition-transform duration-300 group-hover:scale-110" />
        </span>

        <span className="min-w-0 flex-1">
          <span className="flex items-center gap-2">
            <h3 className="text-[15px] font-semibold text-white">
              <Highlight text={feature.title} query={query} />
            </h3>
          </span>
          <p className="mt-1.5 text-sm leading-relaxed text-white/50">
            <Highlight text={feature.summary} query={query} />
          </p>
        </span>

        <ChevronDown
          className={cn(
            "mt-1 h-4 w-4 shrink-0 text-white/25 transition-transform duration-300",
            expanded && "rotate-180 text-signal-soft",
          )}
        />
      </button>

      <div
        className="grid transition-[grid-template-rows] duration-500 ease-out motion-reduce:transition-none"
        style={{ gridTemplateRows: expanded ? "1fr" : "0fr" }}
      >
        <div className="overflow-hidden">
          <div className="border-t border-line px-5 pb-5 pt-4">
            <p className="text-sm leading-relaxed text-white/60">{feature.detail}</p>

            <ul className="mt-4 space-y-2">
              {feature.bullets.map((b) => (
                <li key={b} className="flex gap-2.5 text-sm text-white/55">
                  <span className="mt-[7px] h-1 w-1 shrink-0 rounded-full bg-signal" />
                  <span>{b}</span>
                </li>
              ))}
            </ul>

            {feature.specs && (
              <div className="mt-4 flex flex-wrap gap-2">
                {feature.specs.map((s) => (
                  <span
                    key={s.label}
                    className="rounded-lg border border-line bg-black/30 px-2.5 py-1.5"
                  >
                    <span className="block text-[9px] uppercase tracking-[0.14em] text-white/30">
                      {s.label}
                    </span>
                    <span className="block font-mono text-xs text-gold-soft">{s.value}</span>
                  </span>
                ))}
              </div>
            )}

            {feature.screen && expanded && (
              <div className="mt-4">
                <ScreenMock kind={feature.screen} />
              </div>
            )}
          </div>
        </div>
      </div>

      {/* bottom trace — travels the full width on hover */}
      <span
        aria-hidden
        className={cn(
          "pointer-events-none absolute inset-x-0 bottom-0 h-px origin-left bg-gradient-to-r from-transparent via-signal/60 to-transparent transition-transform duration-500",
          hovered || expanded ? "scale-x-100" : "scale-x-0",
        )}
      />
    </motion.article>
  );
}

/** Wraps matched substrings so a search result shows *why* it matched. */
function Highlight({ text, query }: { text: string; query: string }) {
  const q = query.trim();
  if (!q) return <>{text}</>;
  const terms = q.split(/\s+/).filter(Boolean).map((t) => t.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"));
  if (!terms.length) return <>{text}</>;
  const parts = text.split(new RegExp(`(${terms.join("|")})`, "ig"));
  return (
    <>
      {parts.map((p, i) =>
        terms.some((t) => new RegExp(`^${t}$`, "i").test(p)) ? (
          <mark key={i} className="rounded bg-signal/25 px-0.5 text-white">
            {p}
          </mark>
        ) : (
          <span key={i}>{p}</span>
        ),
      )}
    </>
  );
}

export default function FeaturesPage() {
  const route = routeFor("/features")!;
  useRouteMeta(route);

  /**
   * The explorer's state lives in the URL.
   *
   * "Does it do X" is a question people ask on someone else's behalf, and the
   * answer is worth sending: /features?q=stop+loss is a link, whereas local
   * state is something you have to describe. It also means Back undoes a
   * search rather than leaving the page, and a reload keeps your place.
   *
   * `replace` on every change so that typing eight characters does not put
   * eight entries in the history stack.
   */
  const [params, setParams] = useSearchParams();
  const query = params.get("q") ?? "";
  const rawCategory = params.get("c");
  const category: CategoryId | "all" =
    rawCategory && CATEGORIES.some((c) => c.id === rawCategory)
      ? (rawCategory as CategoryId)
      : "all";

  const patch = (next: { q?: string; c?: CategoryId | "all" }) => {
    const p = new URLSearchParams(params);
    if (next.q !== undefined) next.q ? p.set("q", next.q) : p.delete("q");
    if (next.c !== undefined) next.c !== "all" ? p.set("c", next.c) : p.delete("c");
    setParams(p, { replace: true });
  };
  const setQuery = (q: string) => patch({ q });
  const setCategory = (c: CategoryId | "all") => patch({ c });

  const [expanded, setExpanded] = useState<string | null>("nexus-engine");
  const inputRef = useRef<HTMLInputElement>(null);
  const counts = useMemo(countsByCategory, []);

  // "/" focuses search from anywhere on the page — the convention every tool
  // with a search field uses, and free to support.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "/" || e.metaKey || e.ctrlKey) return;
      const el = document.activeElement;
      if (el instanceof HTMLInputElement || el instanceof HTMLTextAreaElement) return;
      e.preventDefault();
      inputRef.current?.focus();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  const results = useMemo(
    () => FEATURES.filter((f) => (category === "all" || f.category === category) && matches(f, query)),
    [query, category],
  );

  // Searching narrows the list; leaving a card expanded from before the search
  // means the one visible result is often collapsed while an off-screen one is
  // open. Collapse on query change and let the reader choose again.
  useEffect(() => {
    if (query) setExpanded(null);
  }, [query]);

  return (
    <>
      <FeaturesBackdrop />

      {/* ── Hero: search-first, left-weighted ───────────────────────────── */}
      <section className="container-x pt-32 sm:pt-40">
        <div className="grid gap-10 lg:grid-cols-[1.15fr_0.85fr] lg:items-end">
          <div>
            <motion.span
              initial={{ opacity: 0, y: 10 }}
              animate={{ opacity: 1, y: 0 }}
              transition={{ duration: 0.5, ease: EASE }}
              className="inline-flex items-center gap-2 text-[11px] font-semibold uppercase tracking-[0.22em] text-signal-soft"
            >
              <SlidersHorizontal className="h-3.5 w-3.5" />
              Capability explorer
            </motion.span>

            <motion.h1
              initial={{ opacity: 0, y: 16 }}
              animate={{ opacity: 1, y: 0 }}
              transition={{ duration: 0.6, delay: 0.05, ease: EASE }}
              className="mt-5 text-balance text-4xl font-extrabold leading-[1.05] tracking-tight text-white sm:text-5xl lg:text-[3.5rem]"
            >
              {FEATURES.length} capabilities.
              <br />
              <span className="bg-gradient-to-r from-signal-soft via-white to-gold-soft bg-clip-text text-transparent">
                Ask it anything.
              </span>
            </motion.h1>

            <motion.p
              initial={{ opacity: 0, y: 16 }}
              animate={{ opacity: 1, y: 0 }}
              transition={{ duration: 0.6, delay: 0.12, ease: EASE }}
              className="mt-5 max-w-lg text-[17px] leading-relaxed text-white/55"
            >
              Every capability the platform runs today, written out in full and indexed by the
              words you would actually search for. Expand any card for what it does, how it is
              configured, and a sketch of how it appears in the product.
            </motion.p>
          </div>

          <motion.div
            initial={{ opacity: 0, y: 20 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ duration: 0.6, delay: 0.18, ease: EASE }}
            className="grid grid-cols-3 gap-3 lg:pb-2"
          >
            {[
              ["Categories", String(CATEGORIES.length)],
              ["Capabilities", String(FEATURES.length)],
              ["Sketches", "9"],
            ].map(([label, value]) => (
              <div key={label} className="rounded-xl border border-line bg-white/[0.02] p-4">
                <p className="font-mono text-2xl font-semibold tabular text-white">{value}</p>
                <p className="mt-0.5 text-[11px] uppercase tracking-[0.14em] text-white/35">
                  {label}
                </p>
              </div>
            ))}
          </motion.div>
        </div>

        <motion.div
          initial={{ opacity: 0, y: 20 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ duration: 0.6, delay: 0.24, ease: EASE }}
          className="mt-10"
        >
          <HeroSearch query={query} setQuery={setQuery} inputRef={inputRef} resultCount={results.length} />
        </motion.div>
      </section>

      {/* ── Explorer: sticky category rail + results ─────────────────────── */}
      <section className="container-x mt-16 pb-24 sm:mt-20">
        {/* `min-w-0` on both columns is load-bearing: a grid item defaults to
            min-width:auto, so the horizontal category scroller below would size
            the column to its full content width instead of scrolling inside it,
            and the whole page would scroll sideways on a phone. */}
        <div className="grid gap-8 lg:grid-cols-[220px_1fr] lg:gap-10">
          <aside className="min-w-0 lg:sticky lg:top-24 lg:self-start">
            <p className="mb-3 text-[11px] font-semibold uppercase tracking-[0.18em] text-white/30">
              Filter
            </p>
            {/* horizontal scroller on phones, vertical rail from lg up */}
            <div className="-mx-5 flex gap-2 overflow-x-auto px-5 pb-2 lg:mx-0 lg:flex-col lg:overflow-visible lg:px-0 lg:pb-0">
              <CategoryButton
                active={category === "all"}
                onClick={() => setCategory("all")}
                label="Everything"
                note="The whole catalogue"
                count={FEATURES.length}
              />
              {CATEGORIES.map((c) => (
                <CategoryButton
                  key={c.id}
                  active={category === c.id}
                  onClick={() => setCategory(c.id)}
                  label={c.label}
                  note={c.note}
                  count={counts[c.id]}
                />
              ))}
            </div>
          </aside>

          <div className="min-w-0">
            <AnimatePresence mode="popLayout">
              {results.length > 0 ? (
                <motion.div
                  key="results"
                  layout
                  className="grid gap-4 sm:grid-cols-2 xl:grid-cols-2"
                >
                  {results.map((f) => (
                    <div key={f.id} className={cn(f.wide && !query && "sm:col-span-2")}>
                      <FeatureCard
                        feature={f}
                        query={query}
                        expanded={expanded === f.id}
                        onToggle={() => setExpanded((e) => (e === f.id ? null : f.id))}
                      />
                    </div>
                  ))}
                </motion.div>
              ) : (
                <motion.div
                  key="empty"
                  initial={{ opacity: 0, y: 8 }}
                  animate={{ opacity: 1, y: 0 }}
                  exit={{ opacity: 0 }}
                  className="rounded-2xl border border-dashed border-line-strong p-12 text-center"
                >
                  <Search className="mx-auto h-6 w-6 text-white/20" />
                  <p className="mt-4 text-white/70">
                    Nothing matches “<span className="text-white">{query}</span>”
                    {category !== "all" && <> in {CATEGORIES.find((c) => c.id === category)?.label}</>}.
                  </p>
                  <p className="mt-1.5 text-sm text-white/40">
                    Try a broader term, or clear the filter.
                  </p>
                  <button
                    onClick={() => {
                      setQuery("");
                      setCategory("all");
                    }}
                    className="mt-5 inline-flex items-center gap-2 rounded-lg border border-signal/40 bg-signal/10 px-3.5 py-2 text-sm text-signal-soft transition hover:bg-signal/15"
                  >
                    <CornerDownLeft className="h-3.5 w-3.5" />
                    Reset the explorer
                  </button>
                </motion.div>
              )}
            </AnimatePresence>
          </div>
        </div>
      </section>
    </>
  );
}

function CategoryButton({
  active,
  onClick,
  label,
  note,
  count,
}: {
  active: boolean;
  onClick: () => void;
  label: string;
  note: string;
  count: number;
}) {
  return (
    <button
      onClick={onClick}
      aria-pressed={active}
      className={cn(
        "group relative shrink-0 rounded-xl border px-3.5 py-2.5 text-left transition-all duration-200 lg:w-full",
        active
          ? "border-signal/40 bg-signal/[0.09]"
          : "border-line bg-white/[0.015] hover:border-line-strong hover:bg-white/[0.04]",
      )}
    >
      <span className="flex items-center justify-between gap-3">
        <span className={cn("text-sm font-medium", active ? "text-white" : "text-white/65")}>
          {label}
        </span>
        <span
          className={cn(
            "font-mono text-[10px] tabular",
            active ? "text-signal-soft" : "text-white/25",
          )}
        >
          {count}
        </span>
      </span>
      <span className="mt-0.5 hidden text-[11px] text-white/30 lg:block">{note}</span>
      {active && (
        <motion.span
          layoutId="cat-active"
          transition={{ type: "spring", stiffness: 400, damping: 32 }}
          className="absolute inset-y-2 -left-px hidden w-[2px] rounded-full bg-signal lg:block"
        />
      )}
    </button>
  );
}
