import { useState, type ReactNode } from "react";
import { Link, useLocation } from "react-router-dom";
import { motion } from "framer-motion";
import { Check, Copy, Terminal } from "lucide-react";
import { DocsBackdrop } from "@/components/site/backdrops";
import { prefetchRoute } from "@/site/routes";
import { cn } from "@/lib/utils";

const EASE = [0.22, 1, 0.36, 1] as const;

/**
 * Chrome shared by the five developer pages.
 *
 * They share a layout on purpose. Documentation, an API reference, SDKs, the
 * open-source index and the repository guide are one destination a developer
 * moves around inside — every good developer portal treats them that way, and
 * giving each its own bespoke hero would make moving between them feel like
 * leaving the docs and arriving somewhere else. The *content* of each is
 * unrelated; the furniture is not.
 *
 * Palette: near-black under electric blue and cyan, with gold reserved for
 * code. Distinct from /engine, which is graphite and owns the same blues at a
 * much heavier weight.
 */

const DEV_PAGES = [
  { path: "/docs", label: "Documentation", hint: "Start here" },
  { path: "/api", label: "API reference", hint: "Endpoints" },
  { path: "/sdks", label: "SDKs", hint: "Client libraries" },
  { path: "/open-source", label: "Open source", hint: "All of it, MIT" },
  { path: "/github", label: "GitHub", hint: "The repository" },
];

export function DevShell({
  eyebrow,
  title,
  intro,
  children,
}: {
  eyebrow: string;
  title: ReactNode;
  intro: string;
  children: ReactNode;
}) {
  const { pathname } = useLocation();

  return (
    <>
      <DocsBackdrop />

      <div className="container-x pt-28 sm:pt-32">
        <div className="grid gap-10 lg:grid-cols-[210px_minmax(0,1fr)] lg:gap-14">
          {/* portal rail */}
          {/* min-w-0 is load-bearing: a grid item defaults to min-width:auto,
              so the horizontal rail below would size this column to its full
              scroll width and push the whole page sideways on a phone. */}
          <aside className="min-w-0 lg:sticky lg:top-24 lg:self-start">
            <p className="mb-3 font-mono text-[10px] uppercase tracking-[0.2em] text-electric-soft">
              Developers
            </p>
            {/* horizontal on phones, a rail from lg up */}
            <nav
              aria-label="Developer pages"
              className="-mx-5 flex min-w-0 gap-2 overflow-x-auto px-5 pb-2 lg:mx-0 lg:flex-col lg:gap-1 lg:overflow-visible lg:px-0 lg:pb-0"
            >
              {DEV_PAGES.map((p) => {
                const active = p.path === pathname;
                return (
                  <Link
                    key={p.path}
                    to={p.path}
                    onPointerEnter={() => prefetchRoute(p.path)}
                    onFocus={() => prefetchRoute(p.path)}
                    aria-current={active ? "page" : undefined}
                    className={cn(
                      "group relative shrink-0 rounded-lg px-3 py-2 transition-colors duration-200 lg:w-full",
                      active ? "bg-electric/[0.1]" : "hover:bg-white/[0.04]",
                    )}
                  >
                    <span
                      className={cn(
                        "block text-[13px] transition-colors",
                        active ? "text-white" : "text-white/55 group-hover:text-white/85",
                      )}
                    >
                      {p.label}
                    </span>
                    <span className="hidden font-mono text-[10px] text-white/25 lg:block">
                      {p.hint}
                    </span>
                    {active && (
                      <motion.span
                        layoutId="dev-rail-active"
                        transition={{ type: "spring", stiffness: 420, damping: 34 }}
                        className="absolute inset-y-1.5 -left-px hidden w-[2px] rounded-full bg-electric lg:block"
                      />
                    )}
                  </Link>
                );
              })}
            </nav>
          </aside>

          <div className="min-w-0">
            <motion.header
              initial={{ opacity: 0, y: 14 }}
              animate={{ opacity: 1, y: 0 }}
              transition={{ duration: 0.55, ease: EASE }}
              className="border-b border-white/[0.07] pb-8"
            >
              <span className="inline-flex items-center gap-2 font-mono text-[10px] uppercase tracking-[0.2em] text-aqua-soft">
                <Terminal className="h-3.5 w-3.5" />
                {eyebrow}
              </span>
              <h1 className="mt-4 text-balance text-3xl font-bold tracking-tight text-white sm:text-[2.6rem] sm:leading-[1.08]">
                {title}
              </h1>
              <p className="mt-4 max-w-2xl leading-relaxed text-white/55">{intro}</p>
            </motion.header>

            <div className="pb-24 pt-10">{children}</div>
          </div>
        </div>
      </div>
    </>
  );
}

/* ── Building blocks the developer pages share ───────────────────────── */

/** A section with an anchor, so any heading in the portal is linkable. */
export function DevSection({
  id,
  title,
  lead,
  children,
}: {
  id: string;
  title: string;
  lead?: string;
  children: ReactNode;
}) {
  return (
    <section id={id} className="scroll-mt-24 pt-12 first:pt-0">
      <h2 className="group flex items-center gap-2 text-xl font-semibold tracking-tight text-white">
        <a href={`#${id}`} className="no-underline">
          {title}
          <span
            aria-hidden
            className="ml-2 text-electric-soft/0 transition-colors group-hover:text-electric-soft/60"
          >
            #
          </span>
        </a>
      </h2>
      {lead && <p className="mt-3 max-w-2xl leading-relaxed text-white/55">{lead}</p>}
      <div className="mt-5">{children}</div>
    </section>
  );
}

/**
 * A code block with a copy button.
 *
 * The button copies from the `code` prop rather than reading the DOM, so what
 * lands on the clipboard is the source string — not whatever the syntax
 * highlighting or a line-number gutter would have contributed.
 */
export function Code({
  code,
  lang = "bash",
  label,
}: {
  code: string;
  lang?: string;
  label?: string;
}) {
  const [copied, setCopied] = useState(false);

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(code);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1600);
    } catch {
      // Clipboard is permission-gated and blocked outright in some embeds.
      // The code is selectable either way, so failing quietly is correct —
      // an error toast here would be about us, not the reader.
    }
  };

  return (
    <div className="group relative overflow-hidden rounded-xl border border-white/[0.08] bg-black/50">
      <div className="flex items-center justify-between border-b border-white/[0.06] px-3 py-1.5">
        <span className="font-mono text-[10px] uppercase tracking-[0.14em] text-white/30">
          {label ?? lang}
        </span>
        <button
          onClick={copy}
          className="inline-flex items-center gap-1.5 rounded px-2 py-1 font-mono text-[10px] text-white/35 transition-colors hover:bg-white/[0.06] hover:text-white/80"
          aria-label={copied ? "Copied" : "Copy code"}
        >
          {copied ? <Check className="h-3 w-3 text-emerald-soft" /> : <Copy className="h-3 w-3" />}
          {copied ? "copied" : "copy"}
        </button>
      </div>
      <pre className="overflow-x-auto p-4 font-mono text-[12.5px] leading-relaxed text-white/75">
        <code>{code}</code>
      </pre>
    </div>
  );
}

/** HTTP method chip — coloured the way every API reference colours them. */
export function Method({ method }: { method: "GET" | "POST" | "PATCH" | "DELETE" }) {
  const tone = {
    GET: "border-electric/40 bg-electric/10 text-electric-soft",
    POST: "border-emerald/40 bg-emerald/10 text-emerald-soft",
    PATCH: "border-gold/40 bg-gold/10 text-gold-soft",
    DELETE: "border-loss/40 bg-loss/10 text-loss-soft",
  }[method];
  return (
    <span
      className={cn(
        "inline-flex w-[58px] shrink-0 justify-center rounded border px-1.5 py-0.5 font-mono text-[10px] font-semibold",
        tone,
      )}
    >
      {method}
    </span>
  );
}

/** A bordered note — used for the caveats that matter. */
export function Callout({
  tone = "info",
  title,
  children,
}: {
  tone?: "info" | "warn";
  title: string;
  children: ReactNode;
}) {
  return (
    <div
      className={cn(
        "rounded-xl border p-4",
        tone === "warn"
          ? "border-gold/25 bg-gold/[0.05]"
          : "border-electric/25 bg-electric/[0.05]",
      )}
    >
      <p
        className={cn(
          "font-mono text-[10px] uppercase tracking-[0.16em]",
          tone === "warn" ? "text-gold-soft" : "text-electric-soft",
        )}
      >
        {title}
      </p>
      <div className="mt-2 text-sm leading-relaxed text-white/60">{children}</div>
    </div>
  );
}
