import { Link } from "react-router-dom";
import { ArrowUpRight, Bug, GitBranch, Tag } from "lucide-react";
import { DevShell, DevSection, Code, Callout } from "@/components/site/dev/DevShell";
import { useRouteMeta } from "@/site/seo";
import { routeFor, prefetchRoute } from "@/site/routes";
import { REPO_URL } from "@/site/platform";

interface Area {
  path: string;
  what: string;
  language: string;
}

const AREAS: Area[] = [
  {
    path: "automation-hub/",
    what: "The platform: strategies, risk gates, paper execution, backtests, the /v1 API and webhooks.",
    language: "Python",
  },
  {
    path: "automation-hub-dashboard/",
    what: "The operator dashboard, including Settings → Security for keys, webhooks, backups and the audit log.",
    language: "TypeScript · React",
  },
  {
    path: "tradexa-landing/",
    what: "This website. What it claims can be checked against the code in the same repository.",
    language: "TypeScript · React",
  },
  {
    path: "sdks/",
    what: "Python, TypeScript, Go and Rust clients for the /v1 API.",
    language: "Multi",
  },
];

export default function GitHubPage() {
  const route = routeFor("/github")!;
  useRouteMeta(route);

  return (
    <DevShell
      eyebrow="GitHub"
      title="Where the code lives, and how to move it"
      intro="One public repository, one MIT licence, and a strong preference for small changes that arrive with a failing test. This page is the map; the repository itself is the territory."
    >
      <DevSection
        id="repos"
        title="The repository"
        lead="github.com/Arjuunb/nexus-trading-bot, default branch main. The areas you are most likely to want:"
      >
        <ul className="space-y-2.5">
          {AREAS.map((r) => (
            <li key={r.path}>
              <a
                href={REPO_URL}
                target="_blank"
                rel="noreferrer"
                className="group flex flex-wrap items-start gap-x-4 gap-y-2 rounded-xl border border-white/[0.08] bg-white/[0.02] p-5 transition-all duration-300 hover:-translate-y-0.5 hover:border-electric/30 hover:bg-white/[0.04]"
              >
                <span className="min-w-0 flex-1">
                  <span className="flex items-center gap-2">
                    <code className="font-mono text-[14px] text-white">{r.path}</code>
                    <ArrowUpRight className="h-3.5 w-3.5 text-electric-soft opacity-0 transition-all duration-300 group-hover:translate-x-0.5 group-hover:-translate-y-0.5 group-hover:opacity-100" />
                  </span>
                  <span className="mt-2 block text-sm leading-relaxed text-white/50">{r.what}</span>
                </span>
                <span className="flex shrink-0 gap-4 font-mono text-[10px] text-white/30">
                  <span className="inline-flex items-center gap-1.5">
                    <GitBranch className="h-3 w-3" />
                    main
                  </span>
                  <span>{r.language}</span>
                </span>
              </a>
            </li>
          ))}
        </ul>
      </DevSection>

      <DevSection
        id="issues"
        title="Filing an issue that gets fixed"
        lead="Maintainer time goes to issues that can be reproduced. Everything below exists to make that possible in one round trip rather than four."
      >
        <div className="grid gap-3 sm:grid-cols-2">
          <Callout title="What to include">
            The commit or client version, the exact call or configuration, what you expected,
            what happened, and a decision or backtest id if one is involved — the decision record
            holds the rule that fired and the reason it gave. The issue form asks for each.
          </Callout>
          <Callout tone="warn" title="What to leave out">
            API keys, exchange credentials, account identifiers and full log dumps. Redact before
            pasting. A public issue is public immediately and permanently.
          </Callout>
        </div>

        <div className="mt-4">
          <Code
            lang="markdown"
            label="a good issue"
            code={`### What happened
\`POST /v1/backtests\` for \`donchian\` on ETHUSDT 1h returns 202,
then the job sits in \`queued\` indefinitely.

### Expected
The job moves to \`running\` within a few seconds, as it does
for BTCUSDT 15m.

### Reproduce
- client: tradelogx-nexus 0.1.0, Python 3.12
- backtest id: bt_2f77a1c09e4d
- body: { "strategy": "donchian", "symbol": "ETHUSDT", "timeframe": "1h", "bars": 800 }

### Notes
Same result from curl, so it looks server-side.`}
          />
        </div>

        <div className="mt-5 flex items-start gap-2.5 rounded-xl border border-loss/25 bg-loss/[0.05] p-4">
          <Bug className="mt-0.5 h-4 w-4 shrink-0 text-loss-soft" />
          <p className="text-sm leading-relaxed text-white/60">
            <span className="font-medium text-white">Security issues never go in a public
            issue.</span>{" "}
            A vulnerability in a trading system with connected exchange keys is not something to
            disclose in a tracker while it is unpatched. Report it privately through
            GitHub's{" "}
            <a
              href={`${REPO_URL}/security/advisories/new`}
              target="_blank"
              rel="noreferrer"
              className="text-loss-soft underline-offset-2 hover:underline"
            >
              Report a vulnerability
            </a>{" "}
            form on the repository; SECURITY.md has the details.
          </p>
        </div>
      </DevSection>

      <DevSection
        id="pull-requests"
        title="Pull requests"
        lead="One change per pull request. A branch that fixes a bug and also renames three files is two reviews wearing one hat."
      >
        <ol className="space-y-2.5">
          {[
            "Open an issue first for anything larger than a fix — agreeing on the approach costs a comment and saves a rewrite.",
            "Add a test that fails before the change and passes after. For anything that can block or place a trade this is not optional.",
            "Keep the diff readable. Formatting changes belong in their own commit, and preferably their own pull request.",
            "CI must be green: the engine and platform suites, both front-end builds and all four SDK suites run on every push.",
            "The maintainer reviews every pull request in public. There is no CLA; contributions are under the repository's MIT licence.",
          ].map((step, i) => (
            <li key={step} className="flex gap-3.5">
              <span className="mt-0.5 flex h-6 w-6 shrink-0 items-center justify-center rounded-md border border-electric/25 bg-electric/[0.08] font-mono text-[11px] text-electric-soft">
                {i + 1}
              </span>
              <span className="text-sm leading-relaxed text-white/55">{step}</span>
            </li>
          ))}
        </ol>
      </DevSection>

      <DevSection id="releases" title="Releases">
        <div className="rounded-xl border border-white/[0.08] bg-white/[0.015] p-5">
          <div className="flex items-center gap-2">
            <Tag className="h-4 w-4 text-aqua-soft" />
            <h3 className="text-[14px] font-semibold text-white">How changes ship</h3>
          </div>
          <p className="mt-3 max-w-2xl text-sm leading-relaxed text-white/55">
            The platform deploys from the repository with{" "}
            <code className="font-mono text-white/70">scripts/deploy.sh</code>. There are no
            tagged releases yet, and the SDKs are not published to package registries — they
            install from the repository at the commit you choose.
          </p>
          <p className="mt-3 max-w-2xl text-sm leading-relaxed text-white/45">
            A platform deploy does not change what an existing client parses: response shapes are
            pinned by the{" "}
            <code className="font-mono text-white/70">Nexus-Version</code> header, described on
            the{" "}
            <Link
              to="/api"
              onPointerEnter={() => prefetchRoute("/api")}
              className="text-electric-soft underline-offset-2 hover:underline"
            >
              API reference
            </Link>
            .
          </p>
        </div>

        <a
          href={REPO_URL}
          target="_blank"
          rel="noreferrer"
          className="group mt-5 inline-flex items-center gap-2 rounded-xl border border-electric/35 bg-electric/[0.08] px-4 py-2.5 text-sm text-electric-soft transition-all duration-200 hover:border-electric/60 hover:bg-electric/[0.14]"
        >
          Open the repository on GitHub
          <ArrowUpRight className="h-4 w-4 transition-transform duration-200 group-hover:translate-x-0.5 group-hover:-translate-y-0.5" />
        </a>
      </DevSection>
    </DevShell>
  );
}
