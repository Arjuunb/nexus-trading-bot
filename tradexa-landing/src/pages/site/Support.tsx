import { useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { AnimatePresence, motion } from "framer-motion";
import { ChevronDown, LifeBuoy, MessageSquare, Search, ShieldAlert, Timer } from "lucide-react";
import { SupportBackdrop } from "@/components/site/backdrops";
import { useRouteMeta } from "@/site/seo";
import { routeFor, prefetchRoute } from "@/site/routes";
import { REPO_URL } from "@/site/platform";
import { cn } from "@/lib/utils";

const EASE = [0.22, 1, 0.36, 1] as const;

interface Answer {
  q: string;
  a: string;
  topic: string;
  keywords: string[];
}

const ANSWERS: Answer[] = [
  {
    topic: "Connections",
    q: "My exchange key was refused when I saved it",
    a: "Before a key is stored, the exchange is asked what the key may do. A key that can withdraw funds or transfer them to another account — or whose permissions the exchange will not confirm — is refused and nothing is saved. Create a new key with trading enabled and withdrawals and transfers disabled. A key that later gains one of those permissions is revoked the next time it is checked.",
    keywords: ["api key", "rejected", "refused", "withdrawal", "transfer", "permission", "connect", "binance"],
  },
  {
    topic: "Data",
    q: "The dashboard says STALE_CANDLES and nothing is trading",
    a: "New entries pause while the market data is older than its timeframe allows, rather than trading on a gap. It clears by itself when fresh closed candles arrive; after a reconnect, missed candles are replayed in order first. Whether the feed is up is on the status page. Exits and protective stops on open paper positions are not blocked by it.",
    keywords: ["stale", "stale_candles", "data", "feed", "websocket", "not trading", "paused"],
  },
  {
    topic: "Risk",
    q: "The system stopped taking trades and I did not stop it",
    a: "Several limits halt new entries on their own: the drawdown circuit breaker, the daily and weekly loss limits, the cooldown after consecutive losses, the trades-per-day cap, the trading-session and trading-day windows, economic-event blackouts, and stale market data. Each rejection carries a code such as DAILY_LOSS_LIMIT or EVENT_BLACKOUT. Exits are never blocked by any of them.",
    keywords: ["halted", "stopped", "not trading", "drawdown", "daily loss", "weekly loss", "cooldown", "blackout", "paused"],
  },
  {
    topic: "Risk",
    q: "A trade I expected was rejected",
    a: "Every decision is recorded, rejected ones included, with the rule that blocked it and the reason it gave — for example INSUFFICIENT_RR, CORRELATED_EXPOSURE or PORTFOLIO_EXPOSURE. Look the decision up in the dashboard, or through GET /v1/decisions with verdict=rejected. For several of these rules the platform also follows the blocked trade as a virtual one, so you can see what the rule cost or saved.",
    keywords: ["rejected trade", "blocked", "no trade", "correlation", "exposure", "rr", "why", "veto"],
  },
  {
    topic: "Data",
    q: "My backtest results changed after an update",
    a: "Backtests run the code that is deployed now, so a change to a strategy or to the cost model changes historical results too. Results are not pinned to a code version: to compare, note the commit that produced each run, and check the repository history for what changed between them.",
    keywords: ["backtest", "changed", "different", "results", "version", "update"],
  },
  {
    topic: "Data",
    q: "How do I export my data?",
    a: "From the profile menu: your account data as JSON and your trade history as CSV. For everything, Settings → Security → Backups takes a snapshot of every database once a day and on demand — encrypted when the vault master key is set — and can check that the latest one actually restores. The audit log downloads from the same page.",
    keywords: ["export", "download", "data", "backup", "csv", "json", "leave"],
  },
  {
    topic: "Account",
    q: "What does it cost?",
    a: "The software is MIT-licensed and free to run yourself; the whole platform is in the public repository. Exchange fees, funding and spread are charged by the venue — and live order routing is locked today, so every strategy trades on a paper account.",
    keywords: ["billing", "price", "cost", "fee", "free", "subscription", "licence"],
  },
  {
    topic: "Account",
    q: "I have lost access to my two-factor device",
    a: "Sign in with one of the recovery codes you were shown when you turned two-factor on. If those are gone too, only whoever runs the server can restore access — there is no shortcut, because this account can hold exchange credentials. Revoke your exchange keys at the venue in the meantime.",
    keywords: ["2fa", "two factor", "locked out", "recovery", "totp", "login"],
  },
];

const CHANNELS = [
  {
    icon: MessageSquare,
    title: "GitHub issues",
    detail: "Bugs, questions and proposals, in public, on the repository. The issue form asks for what is needed to reproduce the problem. Leave keys and account identifiers out.",
    meta: "Public · answered by the maintainer",
    href: `${REPO_URL}/issues/new/choose`,
  },
  {
    icon: ShieldAlert,
    title: "Security report",
    detail: "Privately, through GitHub's Report a vulnerability form. Only the maintainer and you can see it. Never open a public issue for a vulnerability.",
    meta: "Private",
    href: `${REPO_URL}/security/advisories/new`,
  },
  {
    icon: LifeBuoy,
    title: "Status page",
    detail: "Whether the API, the workers, market data and the database are up right now, and every incident in the last 90 days — measured every minute, not written by hand.",
    meta: "Live",
    href: "/status",
  },
];

const ROUTES = [
  ["Positions need to stop now", "Pause trading from the dashboard, then check the status page", "border-loss/40 text-loss-soft"],
  ["A vulnerability", "Private security report — never a public issue", "border-loss/40 text-loss-soft"],
  ["Something is broken", "A GitHub issue, with the steps to reproduce it", "border-gold/40 text-gold-soft"],
  ["A question or an idea", "A GitHub issue; ideas use the proposal form", "border-white/15 text-white/60"],
];

export default function SupportPage() {
  const route = routeFor("/support")!;
  useRouteMeta(route);

  const [query, setQuery] = useState("");
  const [open, setOpen] = useState<string | null>(ANSWERS[0].q);

  const results = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return ANSWERS;
    const terms = q.split(/\s+/);
    return ANSWERS.filter((entry) => {
      const hay = [entry.q, entry.a, entry.topic, ...entry.keywords].join(" ").toLowerCase();
      return terms.every((t) => hay.includes(t));
    });
  }, [query]);

  return (
    <>
      <SupportBackdrop />

      <section className="container-x pt-32 sm:pt-40">
        <motion.div
          initial={{ opacity: 0, y: 16 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ duration: 0.6, ease: EASE }}
          className="max-w-2xl"
        >
          <span className="inline-flex items-center gap-2 font-mono text-[11px] uppercase tracking-[0.22em] text-gold/80">
            <LifeBuoy className="h-3.5 w-3.5" />
            Support center
          </span>
          <h1 className="mt-5 text-balance text-4xl font-extrabold leading-[1.05] tracking-tight text-white sm:text-5xl">
            Answers first, then the issue tracker
          </h1>
          <p className="mt-6 text-[17px] leading-relaxed text-white/55">
            The answers below describe what the code actually does, not what a settings page
            says. If yours is not here, the channels underneath reach the maintainer — in public
            for bugs and questions, privately for anything security-related.
          </p>
        </motion.div>

        {/* search */}
        <motion.div
          initial={{ opacity: 0, y: 16 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ duration: 0.6, delay: 0.1, ease: EASE }}
          className="mt-9 max-w-2xl"
        >
          <div className="flex items-center gap-3 rounded-xl border border-white/[0.1] bg-black/40 px-4 backdrop-blur-xl transition-colors focus-within:border-gold/40">
            <Search className="h-4.5 w-4.5 shrink-0 text-white/30" />
            <input
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              type="search"
              aria-label="Search support answers"
              placeholder="Search — “key rejected”, “halted”, “export”…"
              className="h-13 w-full min-w-0 bg-transparent py-3.5 text-[15px] text-white outline-none placeholder:text-white/25"
            />
          </div>
          <div className="sr-only" role="status" aria-live="polite">
            {query
              ? `${results.length} of ${ANSWERS.length} answers match ${query}`
              : `Showing all ${ANSWERS.length} answers`}
          </div>
        </motion.div>
      </section>

      {/* answers */}
      <section className="container-x mt-12">
        <AnimatePresence mode="popLayout">
          {results.length > 0 ? (
            <motion.ul key="list" layout className="max-w-3xl divide-y divide-white/[0.06] overflow-hidden rounded-2xl border border-white/[0.08] bg-black/30 backdrop-blur-sm">
              {results.map((entry) => {
                const isOpen = open === entry.q;
                return (
                  <li key={entry.q}>
                    <button
                      onClick={() => setOpen(isOpen ? null : entry.q)}
                      aria-expanded={isOpen}
                      className="flex w-full items-start gap-4 p-5 text-left transition-colors hover:bg-white/[0.03]"
                    >
                      <span className="mt-0.5 shrink-0 rounded border border-white/[0.1] px-2 py-0.5 font-mono text-[10px] text-white/40">
                        {entry.topic}
                      </span>
                      <span className="min-w-0 flex-1 text-[15px] text-white/85">{entry.q}</span>
                      <ChevronDown
                        className={cn(
                          "mt-0.5 h-4 w-4 shrink-0 text-white/25 transition-transform duration-300",
                          isOpen && "rotate-180 text-gold",
                        )}
                      />
                    </button>
                    <div
                      className="grid transition-[grid-template-rows] duration-400 ease-out motion-reduce:transition-none"
                      style={{ gridTemplateRows: isOpen ? "1fr" : "0fr" }}
                    >
                      <div className="overflow-hidden">
                        <p className="px-5 pb-5 text-[15px] leading-relaxed text-white/55">
                          {entry.a}
                        </p>
                      </div>
                    </div>
                  </li>
                );
              })}
            </motion.ul>
          ) : (
            <motion.div
              key="empty"
              initial={{ opacity: 0, y: 8 }}
              animate={{ opacity: 1, y: 0 }}
              exit={{ opacity: 0 }}
              className="max-w-3xl rounded-2xl border border-dashed border-white/[0.12] p-10 text-center"
            >
              <p className="text-white/70">
                Nothing matches “<span className="text-white">{query}</span>”.
              </p>
              <p className="mt-2 text-sm text-white/40">
                That is worth knowing — open a GitHub issue and the answer can end up on this
                page.
              </p>
              <button
                onClick={() => setQuery("")}
                className="mt-5 rounded-lg border border-gold/40 bg-gold/10 px-3.5 py-2 text-sm text-gold-soft transition hover:bg-gold/15"
              >
                Clear the search
              </button>
            </motion.div>
          )}
        </AnimatePresence>
      </section>

      {/* channels */}
      <section className="container-x mt-16">
        <h2 className="text-2xl font-bold tracking-tight text-white">Reaching the maintainer</h2>
        <div className="mt-6 grid gap-3 sm:grid-cols-3">
          {CHANNELS.map((c) => {
            const external = c.href.startsWith("http");
            const body = (
              <>
                <span className="flex h-9 w-9 items-center justify-center rounded-lg border border-gold/25 bg-gold/[0.08] text-gold-soft transition-transform duration-300 group-hover:scale-110">
                  <c.icon className="h-4 w-4" />
                </span>
                <h3 className="mt-4 text-[15px] font-semibold text-white">{c.title}</h3>
                <p className="mt-2 text-sm leading-relaxed text-white/50">{c.detail}</p>
                <p className="mt-3 font-mono text-[10px] uppercase tracking-[0.12em] text-white/25">
                  {c.meta}
                </p>
              </>
            );
            const cls =
              "group block rounded-2xl border border-white/[0.08] bg-white/[0.02] p-5 transition-all duration-300 hover:-translate-y-0.5 hover:border-gold/25 hover:bg-white/[0.04]";
            return external ? (
              <a key={c.title} href={c.href} target="_blank" rel="noreferrer" className={cls}>
                {body}
              </a>
            ) : (
              <Link key={c.title} to={c.href} onPointerEnter={() => prefetchRoute(c.href)} className={cls}>
                {body}
              </Link>
            );
          })}
        </div>
      </section>

      {/* severity */}
      <section className="container-x mt-8 pb-24">
        <div className="rounded-2xl border border-white/[0.08] bg-black/30 p-5 backdrop-blur-sm sm:p-7">
          <h2 className="flex items-center gap-2 text-lg font-semibold text-white">
            <Timer className="h-4 w-4 text-gold-soft" />
            Where to take it
          </h2>
          <p className="mt-2 max-w-2xl text-sm leading-relaxed text-white/45">
            There is no staffed support desk and no response-time guarantee: the platform is
            maintained in the open, and issues are answered by the maintainer as they arrive.
            What is guaranteed is in the code — limits that halt trading on their own, and a
            status page that is measured rather than written.
          </p>

          <ul className="mt-5 space-y-2">
            {ROUTES.map(([what, where, tone]) => (
              <li
                key={what}
                className="flex flex-wrap items-center gap-x-4 gap-y-1 rounded-lg border border-white/[0.06] bg-white/[0.015] px-4 py-3"
              >
                <span
                  className={cn(
                    "shrink-0 rounded border px-2 py-0.5 font-mono text-[11px] font-semibold",
                    tone,
                  )}
                >
                  {what}
                </span>
                <span className="min-w-0 flex-1 text-right text-[14px] text-white/60">{where}</span>
              </li>
            ))}
          </ul>

          <p className="mt-6 border-t border-white/[0.07] pt-5 text-sm leading-relaxed text-white/40">
            If trading is impaired right now, check the{" "}
            <Link
              to="/status"
              onPointerEnter={() => prefetchRoute("/status")}
              className="text-gold-soft underline-offset-2 hover:underline"
            >
              status page
            </Link>{" "}
            first — an incident already being worked on is faster to read about than to report.
            For anything about how a decision was made, the{" "}
            <Link
              to="/docs"
              onPointerEnter={() => prefetchRoute("/docs")}
              className="text-gold-soft underline-offset-2 hover:underline"
            >
              documentation
            </Link>{" "}
            covers the vocabulary the answers use.
          </p>
        </div>
      </section>
    </>
  );
}
