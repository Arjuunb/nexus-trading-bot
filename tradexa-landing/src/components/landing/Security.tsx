import { Lock, KeyRound, Ban, ScrollText, ShieldCheck, type LucideIcon } from "lucide-react";
import { Link } from "react-router-dom";
import { Reveal, RevealGroup } from "@/components/Reveal";
import { Card } from "@/components/ui/Card";

interface Item {
  icon: LucideIcon;
  title: string;
  body: string;
}

const ITEMS: Item[] = [
  { icon: Lock, title: "Encrypted where it counts", body: "Exchange keys, webhook secrets and backups are sealed with AES-256-GCM under a master key kept out of the database. Everything travels over TLS." },
  { icon: KeyRound, title: "Withdrawal keys refused", body: "Before a key is stored, the exchange is asked what it can do. A key that can withdraw or transfer funds is refused." },
  { icon: ScrollText, title: "Tamper-evident audit log", body: "Every state-changing request lands in an append-only, hash-chained log you can verify and copy off the server." },
  { icon: ShieldCheck, title: "Hardened, and checkable", body: "A strict Content-Security-Policy on every page, and a security checkup in the dashboard that lists what is on and what to fix." },
];

export function Security() {
  return (
    <section id="security" className="section">
      <div className="container-x">
        <div className="grid items-center gap-14 lg:grid-cols-[1fr_1.1fr]">
          <div>
            <Reveal>
              <Link to="/security" className="group inline-block no-underline">
                <span className="eyebrow transition-colors group-hover:text-gold">Security</span>
                <h2 className="mt-4 text-balance text-3xl font-bold tracking-tight text-white sm:text-4xl">
                  Built to be trusted with{" "}
                  <span className="text-gold-gradient">your capital</span>
                  <span aria-hidden className="ml-2 hidden text-2xl font-bold text-gold/0 transition-colors group-hover:text-gold/60 sm:inline">→</span>
                </h2>
              </Link>
            </Reveal>
            <Reveal delay={0.1}>
              <p className="mt-4 max-w-md text-white/55">
                Automated trading only earns its place when its security can be checked. TradeLogX Nexus
                is built so a key it holds can trade — and nothing more.
              </p>
              <div className="mt-6 inline-flex items-center gap-2 rounded-full border border-emerald/25 bg-emerald/[0.07] px-4 py-2 text-sm text-emerald-soft">
                <Ban className="h-4 w-4" />
                Trade-only keys · withdrawals impossible
              </div>
            </Reveal>
          </div>

          {/* Grouped stagger: the parent triggers once, so the cards arrive in
              order regardless of how fast the section is scrolled past. */}
          <RevealGroup className="grid gap-4 sm:grid-cols-2" stagger={0.07}>
            {ITEMS.map((it) => (
              <div key={it.title} className="h-full">
                <Card interactive className="h-full p-5">
                  <div className="mb-4 inline-flex h-10 w-10 items-center justify-center rounded-lg border border-line-strong bg-white/[0.04] text-gold">
                    <it.icon className="h-5 w-5" />
                  </div>
                  <h3 className="text-[15px] font-semibold text-white">{it.title}</h3>
                  <p className="mt-1.5 text-sm leading-relaxed text-white/55">{it.body}</p>
                </Card>
              </div>
            ))}
          </RevealGroup>
        </div>
      </div>
    </section>
  );
}
