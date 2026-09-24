import { useState } from "react";
import { Check, Minus } from "lucide-react";
import { DevShell, DevSection, Code, Callout } from "@/components/site/dev/DevShell";
import { useRouteMeta } from "@/site/seo";
import { routeFor } from "@/site/routes";
import { cn } from "@/lib/utils";

interface Sdk {
  id: string;
  name: string;
  runtime: string;
  install: string;
  installLang: string;
  sample: string;
  sampleLang: string;
}

const REPO = "https://github.com/Arjuunb/nexus-trading-bot";

const SDKS: Sdk[] = [
  {
    id: "python",
    name: "Python",
    runtime: "3.9+, standard library only",
    install: `pip install "git+${REPO}#subdirectory=sdks/python"`,
    installLang: "bash",
    sampleLang: "python",
    sample: `from tradelogx_nexus import Client

client = Client()  # reads NEXUS_API_KEY

for d in client.decisions.list(verdict="rejected", limit=20):
    print(d["symbol"], d["quality_score"], d["blocked_by"])
    print(" ", d["reason"])`,
  },
  {
    id: "typescript",
    name: "TypeScript",
    runtime: "Node 18+, Deno, Bun; no dependencies",
    install: `git clone ${REPO}
cd nexus-trading-bot/sdks/typescript
npm install && npm pack        # builds tradelogx-nexus-0.1.0.tgz

# then, in your project
npm install /path/to/tradelogx-nexus-0.1.0.tgz`,
    installLang: "bash",
    sampleLang: "typescript",
    sample: `import { Nexus } from "@tradelogx/nexus";

const nexus = new Nexus(); // reads NEXUS_API_KEY

for await (const d of nexus.decisions.list({ verdict: "rejected", limit: 20 })) {
  console.log(d.symbol, d.quality_score, d.blocked_by);
  console.log(" ", d.reason);
}`,
  },
  {
    id: "go",
    name: "Go",
    runtime: "1.22+, standard library only",
    install: "go get github.com/Arjuunb/nexus-trading-bot/sdks/go",
    installLang: "bash",
    sampleLang: "go",
    sample: `client, err := nexus.New() // reads NEXUS_API_KEY
if err != nil {
    log.Fatal(err)
}

it := client.Decisions.List(ctx, &nexus.DecisionQuery{Verdict: "rejected", Limit: 20})
for it.Next() {
    d := it.Value()
    if d.BlockedBy != nil { // optional fields are pointers
        fmt.Println(d.Symbol, "blocked by", *d.BlockedBy)
    }
}
if err := it.Err(); err != nil {
    log.Fatal(err)
}`,
  },
  {
    id: "rust",
    name: "Rust",
    runtime: "2021 edition, blocking (ureq)",
    install: `cargo add tradelogx-nexus --git ${REPO}`,
    installLang: "bash",
    sampleLang: "rust",
    sample: `use tradelogx_nexus::{Client, DecisionQuery};

let client = Client::from_env()?; // reads NEXUS_API_KEY

let query = DecisionQuery { verdict: Some("rejected".into()), limit: Some(20), ..Default::default() };
for d in client.decisions(query) {
    let d = d?;
    println!("{} {:?} {:?}", d.symbol, d.quality_score, d.blocked_by);
}`,
  },
];

const PARITY: [string, boolean[]][] = [
  ["Every /v1 endpoint", [true, true, true, true]],
  ["Automatic cursor pagination", [true, true, true, true]],
  ["Retry with backoff, honouring Retry-After", [true, true, true, true]],
  ["Webhook signature verification", [true, true, true, true]],
  ["Queue-and-wait backtest helper", [true, true, true, true]],
  ["Typed response models", [false, true, true, true]],
  ["Async I/O", [false, true, false, false]],
  ["Streaming decision feed", [false, false, false, false]],
];

export default function SdksPage() {
  const route = routeFor("/sdks")!;
  useRouteMeta(route);
  const [active, setActive] = useState(SDKS[0].id);
  const sdk = SDKS.find((s) => s.id === active)!;

  return (
    <DevShell
      eyebrow="SDKs"
      title="Four clients for one API"
      intro="Python, TypeScript, Go and Rust clients for the /v1 API, written by hand and kept in the same repository as the API they call — generators in Python, async iterators in TypeScript, an errors-last iterator in Go, an iterator of results in Rust. The parity table below says where one is behind."
    >
      <DevSection id="install" title="Install and make a first call">
        {/* language switcher */}
        <div className="flex flex-wrap gap-1.5">
          {SDKS.map((s) => (
            <button
              key={s.id}
              onClick={() => setActive(s.id)}
              aria-pressed={s.id === active}
              className={cn(
                "group rounded-lg border px-3.5 py-2 text-left transition-all duration-200",
                s.id === active
                  ? "border-electric/45 bg-electric/[0.1]"
                  : "border-white/[0.08] hover:border-white/20 hover:bg-white/[0.03]",
              )}
            >
              <span className="flex items-center gap-2">
                <span
                  className={cn(
                    "text-[13px] font-medium",
                    s.id === active ? "text-white" : "text-white/60",
                  )}
                >
                  {s.name}
                </span>
              </span>
              <span className="mt-0.5 block font-mono text-[10px] text-white/25">{s.runtime}</span>
            </button>
          ))}
        </div>

        <div className="mt-5 space-y-3">
          <Code lang={sdk.installLang} label="install" code={sdk.install} />
          <Code
            lang={sdk.sampleLang}
            label={`${sdk.name.toLowerCase()} · list rejected decisions`}
            code={sdk.sample}
          />
        </div>

        <p className="mt-4 max-w-2xl text-sm leading-relaxed text-white/45">
          Every client reads <code className="font-mono text-white/70">NEXUS_API_KEY</code> (and,
          optionally, <code className="font-mono text-white/70">NEXUS_API_BASE</code>) from the
          environment by default, so a key never has to appear in your source. Create keys in
          the dashboard under Settings → Security → API keys.
        </p>
        <div className="mt-4">
          <Callout tone="warn" title="Not on the package registries yet">
            The clients are not yet published to PyPI, npm or crates.io, so the commands above
            install them straight from the public repository.
          </Callout>
        </div>
      </DevSection>

      <DevSection
        id="parity"
        title="Feature parity"
        lead="Where a client is behind, it says so here rather than in a changelog you would have to go looking for."
      >
        <div className="overflow-x-auto rounded-xl border border-white/[0.08]">
          <table className="w-full min-w-[560px] border-collapse text-left">
            <thead>
              <tr className="border-b border-white/[0.08] bg-white/[0.02]">
                <th className="px-4 py-2.5 font-mono text-[10px] uppercase tracking-wider text-white/30">
                  Capability
                </th>
                {SDKS.map((s) => (
                  <th
                    key={s.id}
                    className="px-3 py-2.5 text-center font-mono text-[10px] uppercase tracking-wider text-white/30"
                  >
                    {s.name}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {PARITY.map(([capability, flags]) => (
                <tr key={capability} className="border-b border-white/[0.05] last:border-0">
                  <td className="px-4 py-2.5 text-[13px] text-white/60">{capability}</td>
                  {flags.map((ok, i) => (
                    <td key={i} className="px-3 py-2.5 text-center">
                      {ok ? (
                        <Check className="mx-auto h-3.5 w-3.5 text-emerald-soft" />
                      ) : (
                        <Minus className="mx-auto h-3.5 w-3.5 text-white/20" />
                      )}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </DevSection>

      <DevSection id="versioning" title="Versioning and support">
        <div className="grid gap-3 sm:grid-cols-2">
          <Callout title="Pre-1.0">
            All four clients are at 0.1.0. Until 1.0 a minor release may still change a
            signature, so pin the commit you install from.
          </Callout>
          <Callout title="Pinned response shapes">
            Clients send the <code className="font-mono text-white/70">Nexus-Version</code> header
            they were built against, so upgrading the platform cannot change what your code
            parses. Upgrading the client is the deliberate act.
          </Callout>
        </div>

        <p className="mt-5 max-w-2xl text-sm leading-relaxed text-white/45">
          The clients live in <code className="font-mono text-white/70">sdks/</code> of the same
          MIT-licensed repository as the platform, each with its own tests, and CI runs all four
          suites on every push — the details are on the open-source page.
        </p>
      </DevSection>
    </DevShell>
  );
}
