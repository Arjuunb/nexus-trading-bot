import { Link } from "react-router-dom";
import { ArrowRight, BookOpen, Compass, GitBranch, Layers, ShieldCheck } from "lucide-react";
import { DevShell, DevSection, Code, Callout } from "@/components/site/dev/DevShell";
import { useRouteMeta } from "@/site/seo";
import { routeFor, prefetchRoute } from "@/site/routes";

const GUIDES = [
  {
    icon: Compass,
    title: "Attach an exchange key",
    body: "In Settings → Security, paste a Binance key. Binance is asked what the key may do first: a key that can withdraw or transfer funds is refused before it is stored. Bind it to the server's IP on Binance. Live routing is locked, so the key is kept encrypted for later.",
    time: "5 min",
  },
  {
    icon: Layers,
    title: "Run your first backtest",
    body: "Queue one through the API or run it in the dashboard's Backtesting page. Results come back twice — gross, and net of modelled fees, spread and slippage — so a strategy whose edge is only its costs shows as exactly that.",
    time: "10 min",
  },
  {
    icon: ShieldCheck,
    title: "Set your risk limits",
    body: "Daily and weekly loss limits, per-position and total exposure caps, correlated-position limits, session windows and a drawdown halt, in Settings. An order that fails any check is never created.",
    time: "8 min",
  },
  {
    icon: GitBranch,
    title: "Paper trading and the live lock",
    body: "Every strategy runs in paper mode against the live Binance feed, with modelled costs and a full journal. Live order routing is locked; asking the API to promote a strategy to live answers live_routing_locked.",
    time: "3 min",
  },
];

const CONCEPTS = [
  ["Quality score", "The Decision Brain's 0–100 score from eight weighted factors — higher-timeframe alignment, regime, reward : risk, momentum, stop safety, volatility, structure and volume. 60 is the default minimum; some setups are blocked whatever they score."],
  ["Regime", "Trending, ranging, or high, low or extreme volatility. Regime fit is part of the score, and a choppy regime blocks trades that are not reversals."],
  ["Decision", "Every evaluation — accepted or rejected — is stored with its scores, the rules it passed and failed, and the reason. The full market inputs are not stored yet, so a decision cannot be re-run."],
  ["Risk checks", "Twenty checks on the one path from a signal to an order: loss limits, exposure, correlation, event blackouts, sessions, venue lot rules and more. If a check cannot run, nothing trades."],
  ["Trade memory", "Every closed trade with its context, searchable by similarity. It is not consulted when a decision is made; repeated losing patterns reduce trade size instead."],
  ["Order intent", "A paper order waiting for a Binance quote to fill it. A limit entry that is never reached expires rather than being chased."],
];

export default function DocsPage() {
  const route = routeFor("/docs")!;
  useRouteMeta(route);

  return (
    <DevShell
      eyebrow="Documentation"
      title={
        <>
          Everything you need to run it,
          <br className="hidden sm:block" /> in the order you need it
        </>
      }
      intro="Start with the quickstart, which gets a strategy backtesting through the API in about ten minutes. The concepts section explains the vocabulary the rest of the platform uses, and the guides cover the first things people set up."
    >
      <DevSection
        id="quickstart"
        title="Quickstart"
        lead="Install the Python client, create a key, and run a backtest. This does not touch an exchange and cannot place an order."
      >
        <div className="space-y-3">
          <Code lang="bash" label="1 · install" code={`pip install "git+https://github.com/Arjuunb/nexus-trading-bot#subdirectory=sdks/python"`} />
          <Code
            lang="bash"
            label="2 · authenticate"
            code={`export NEXUS_API_KEY="nxs_..."   # Settings → Security → API keys (control scope to queue backtests)`}
          />
          <Code
            lang="python"
            label="3 · backtest"
            code={`from tradelogx_nexus import Client

client = Client()               # reads NEXUS_API_KEY

job = client.backtests.run(
    "brain",                    # an id from client.strategies.list()
    symbol="BTCUSDT",
    timeframe="15m",
    bars=1000,
)

gross, net = job["result"]["gross"], job["result"]["net"]
print(gross["expectancy_r"], net["expectancy_r"])   # before and after costs
print(job["result"]["costs"]["net_r_drag"])         # what the costs took, in R`}
          />
        </div>

        <div className="mt-5">
          <Callout tone="warn" title="Before you trust a backtest">
            A backtest that looks good is a hypothesis, not a result. Run the strategy in paper
            mode against the live feed for a full month and compare — it costs nothing, and live
            routing stays locked until a strategy has earned it. The{" "}
            <Link to="/risk-disclosure" className="text-gold-soft underline-offset-2 hover:underline">
              risk disclosure
            </Link>{" "}
            covers why this matters more than it sounds.
          </Callout>
        </div>
      </DevSection>

      <DevSection
        id="guides"
        title="Guides"
        lead="Task-shaped, not feature-shaped. Each one ends with something working."
      >
        <div className="grid gap-3 sm:grid-cols-2">
          {GUIDES.map((g) => (
            <article
              key={g.title}
              className="group rounded-xl border border-white/[0.08] bg-white/[0.02] p-5 transition-all duration-300 hover:-translate-y-0.5 hover:border-electric/30 hover:bg-white/[0.04]"
            >
              <div className="flex items-start justify-between gap-3">
                <span className="flex h-9 w-9 items-center justify-center rounded-lg border border-electric/25 bg-electric/[0.08] text-electric-soft transition-transform duration-300 group-hover:scale-110">
                  <g.icon className="h-4 w-4" />
                </span>
                <span className="font-mono text-[10px] text-white/25">{g.time}</span>
              </div>
              <h3 className="mt-4 text-[15px] font-semibold text-white">{g.title}</h3>
              <p className="mt-2 text-sm leading-relaxed text-white/50">{g.body}</p>
            </article>
          ))}
        </div>
      </DevSection>

      <DevSection
        id="concepts"
        title="Concepts"
        lead="The vocabulary the API, the dashboard and the journal all assume. Worth ten minutes before the reference."
      >
        <dl className="divide-y divide-white/[0.06] rounded-xl border border-white/[0.08] bg-white/[0.015]">
          {CONCEPTS.map(([term, def]) => (
            <div key={term} className="grid gap-1 p-4 sm:grid-cols-[190px_1fr] sm:gap-5">
              <dt className="font-mono text-[13px] text-aqua-soft">{term}</dt>
              <dd className="text-sm leading-relaxed text-white/55">{def}</dd>
            </div>
          ))}
        </dl>
      </DevSection>

      <DevSection
        id="next"
        title="Where to go next"
        lead="The reference is exhaustive; these three pages are the ones people need first."
      >
        <div className="grid gap-3 sm:grid-cols-3">
          {[
            ["/api", "API reference", "Every endpoint, with request and response shapes"],
            ["/sdks", "SDKs", "Python, TypeScript, Go and Rust clients"],
            ["/how-it-works", "How it works", "The seven stages a trade passes through"],
          ].map(([path, label, blurb]) => (
            <Link
              key={path}
              to={path}
              onPointerEnter={() => prefetchRoute(path)}
              className="group flex flex-col rounded-xl border border-white/[0.08] bg-white/[0.02] p-4 transition-all duration-300 hover:-translate-y-0.5 hover:border-electric/30"
            >
              <span className="flex items-center gap-1.5 text-[14px] font-medium text-white">
                {label}
                <ArrowRight className="h-3.5 w-3.5 text-electric-soft opacity-0 transition-all duration-300 group-hover:translate-x-0.5 group-hover:opacity-100" />
              </span>
              <span className="mt-1 text-xs leading-relaxed text-white/45">{blurb}</span>
            </Link>
          ))}
        </div>

        <p className="mt-6 flex items-start gap-2 text-sm leading-relaxed text-white/40">
          <BookOpen className="mt-0.5 h-4 w-4 shrink-0 text-white/25" />
          Something missing or wrong here is a documentation bug and worth reporting the same way
          as any other — through the{" "}
          <Link to="/support" className="text-electric-soft underline-offset-2 hover:underline">
            support center
          </Link>
          .
        </p>
      </DevSection>
    </DevShell>
  );
}
