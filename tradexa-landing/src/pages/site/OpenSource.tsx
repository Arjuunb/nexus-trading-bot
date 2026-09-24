import { Link } from "react-router-dom";
import { Scale, Package, GitPullRequest, FolderGit2 } from "lucide-react";
import { DevShell, DevSection, Code, Callout } from "@/components/site/dev/DevShell";
import { useRouteMeta } from "@/site/seo";
import { routeFor, prefetchRoute } from "@/site/routes";
import { REPO_URL } from "@/site/platform";

interface Part {
  path: string;
  summary: string;
  why: string;
}

const PARTS: Part[] = [
  {
    path: "automation-hub/",
    summary: "The platform itself: strategies, the pre-trade risk gates, paper execution, the backtest harness with its cost model, the /v1 API, webhooks, the audit log and the key vault.",
    why: "A risk system you cannot inspect is one you take on faith. Every rule that can block a trade, and every cost a backtest charges, is in this directory with the tests that pin it down.",
  },
  {
    path: "sdks/",
    summary: "The Python, TypeScript, Go and Rust clients, each with its own tests.",
    why: "Client libraries live in your codebase, so they should be readable, forkable and patchable without waiting for anyone.",
  },
  {
    path: "automation-hub-dashboard/ · tradexa-landing/",
    summary: "The operator dashboard and this website.",
    why: "What the site says the platform does can be checked against the code that does it, in the same commit.",
  },
  {
    path: "nginx/ · scripts/ · compose.yaml",
    summary: "The deployment: reverse proxy, TLS, containers, backups and the deploy script.",
    why: "Self-hosting is a real option, not a sales conversation.",
  },
];

export default function OpenSourcePage() {
  const route = routeFor("/open-source")!;
  useRouteMeta(route);

  return (
    <DevShell
      eyebrow="Open source"
      title="All of it is public"
      intro="The whole platform — strategies, risk gates, paper execution, backtests, the API, the dashboard, this site and the deployment — lives in one public repository under the MIT licence. Nothing on this site describes a closed component."
    >
      <DevSection
        id="projects"
        title="What is in the repository"
        lead="One repository, one licence. The directories that matter, and why each is worth reading."
      >
        <a
          href={REPO_URL}
          target="_blank"
          rel="noreferrer"
          className="mb-4 inline-flex items-center gap-2 font-mono text-[13px] text-electric-soft underline-offset-2 hover:underline"
        >
          <FolderGit2 className="h-4 w-4" />
          github.com/Arjuunb/nexus-trading-bot
          <span className="rounded border border-white/[0.1] px-2 py-0.5 text-[10px] text-white/45">MIT</span>
        </a>
        <ul className="space-y-3">
          {PARTS.map((p) => (
            <li
              key={p.path}
              className="group rounded-xl border border-white/[0.08] bg-white/[0.02] p-5 transition-all duration-300 hover:-translate-y-0.5 hover:border-electric/30"
            >
              <div className="flex flex-wrap items-center gap-3">
                <Package className="h-4 w-4 shrink-0 text-electric-soft" />
                <code className="font-mono text-[14px] text-white">{p.path}</code>
              </div>
              <p className="mt-3 text-sm leading-relaxed text-white/60">{p.summary}</p>
              <p className="mt-2 text-sm leading-relaxed text-white/40">{p.why}</p>
            </li>
          ))}
        </ul>
      </DevSection>

      <DevSection id="not-open" title="What is not in it">
        <div className="grid gap-3 sm:grid-cols-2">
          <Callout tone="warn" title="Your credentials and data">
            Exchange keys, API keys, the vault master key, databases and backups live on the
            server that runs the platform, never in the repository. The code that encrypts them
            is public; the secrets are not.
          </Callout>
          <Callout tone="warn" title="Published packages">
            The SDKs are not on PyPI, npm or crates.io yet. Until they are, install them from
            the repository as the SDK page shows.
          </Callout>
        </div>
      </DevSection>

      <DevSection
        id="contributing"
        title="Contributing"
        lead="Small and specific beats large and speculative. A failing test that reproduces a bug is the most useful thing you can send."
      >
        <Code
          lang="bash"
          label="get set up"
          code={`git clone https://github.com/Arjuunb/nexus-trading-bot
cd nexus-trading-bot
python -m pip install -e ".[dev]" -r automation-hub/requirements.txt
(cd automation-hub && python -m pytest -q)   # the platform's test suite`}
        />

        <div className="mt-5 grid gap-3 sm:grid-cols-3">
          {[
            [Scale, "Licence", "Contributions are made under the repository's MIT licence. There is no CLA."],
            [GitPullRequest, "Review", "The maintainer reviews every pull request, in public, and CI runs every test suite on every push."],
            [Package, "Rules", "CONTRIBUTING.md in the repository has the setup, the checks, and the changes that are not accepted — live order routing among them."],
          ].map(([Icon, title, body]) => {
            const I = Icon as typeof Scale;
            return (
              <div key={title as string} className="rounded-xl border border-white/[0.08] bg-white/[0.015] p-4">
                <I className="h-4 w-4 text-aqua-soft" />
                <h3 className="mt-3 text-[13px] font-semibold text-white">{title as string}</h3>
                <p className="mt-1.5 text-xs leading-relaxed text-white/45">{body as string}</p>
              </div>
            );
          })}
        </div>

        <p className="mt-6 max-w-2xl text-sm leading-relaxed text-white/45">
          Where the code lives and what a good issue looks like is covered on the{" "}
          <Link
            to="/github"
            onPointerEnter={() => prefetchRoute("/github")}
            className="text-electric-soft underline-offset-2 hover:underline"
          >
            GitHub page
          </Link>
          . Security issues are the exception to all of this — never open a public issue for one;
          the disclosure route is on the{" "}
          <Link to="/security" className="text-electric-soft underline-offset-2 hover:underline">
            security page
          </Link>
          .
        </p>
      </DevSection>
    </DevShell>
  );
}
