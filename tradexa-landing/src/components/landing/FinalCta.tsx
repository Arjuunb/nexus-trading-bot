import { ArrowRight, ShieldCheck, Eye, Ban } from "lucide-react";
import { GridTexture } from "@/components/site/backdrops";
import { Reveal } from "@/components/Reveal";
import { Button } from "@/components/ui/Button";
import { Magnetic } from "@/components/motion/Magnetic";
import { BrainScanner } from "./BrainScanner";
import { APP_URL, SIGNUP_URL } from "@/lib/utils";

const ASSURANCES = [
  { icon: Eye, text: "Every decision is scored, explained and journaled" },
  { icon: ShieldCheck, text: "Risk guards are enforced — never bypassed" },
  { icon: Ban, text: "Trade-only API keys · withdrawals impossible" },
];

/** Closing section: the Decision Engine scanner (the page's third signature
 *  animation) beside the final call to action. */
export function FinalCta() {
  return (
    <section id="cta" className="section relative">
      {/* closes the page on the same texture it opened with */}
      <GridTexture />
      <div className="container-x">
        {/* A slow light orbits the card's 1px border: the frame is a conic
            gradient turning behind an opaque card inset by one pixel. */}
        <div className="relative overflow-hidden rounded-2xl p-px shadow-[0_40px_120px_-40px_rgba(200,169,75,0.25)]">
          <div aria-hidden className="absolute inset-0 rounded-2xl bg-line" />
          <div
            aria-hidden
            className="pointer-events-none absolute left-1/2 top-1/2 aspect-square w-[180%] -translate-x-1/2 -translate-y-1/2 motion-safe:animate-orbit"
            style={{ background: "conic-gradient(from 0deg, transparent 0deg 250deg, rgba(231,206,134,0.65) 300deg, transparent 340deg)" }}
          />
        <div className="relative overflow-hidden rounded-[calc(1.25rem-1px)] bg-ink-800 px-6 py-14 sm:px-12">
          <div className="pointer-events-none absolute inset-0 bg-radial-fade" />
          <div className="relative grid items-center gap-12 lg:grid-cols-[1fr_1.05fr]">
            <Reveal>
              <span className="eyebrow">Watch it think</span>
              <h2 className="mt-4 text-balance text-3xl font-bold tracking-tight text-white sm:text-4xl">
                A brain that says <span className="text-emerald-soft">yes</span> —{" "}
                and knows when to say <span className="text-loss-soft">no</span>.
              </h2>
              <p className="mt-4 max-w-md text-white/55">
                The Decision Engine scores every setup before a cent moves. Weak setups are
                skipped, strong ones execute — and either way it’s remembered, so the next
                decision starts smarter.
              </p>

              <ul className="mt-6 space-y-2.5">
                {ASSURANCES.map((a) => (
                  <li key={a.text} className="flex items-center gap-2.5 text-sm text-white/65">
                    <a.icon className="h-4 w-4 shrink-0 text-gold" />
                    {a.text}
                  </li>
                ))}
              </ul>

              <div className="mt-8 flex flex-col gap-3 sm:flex-row sm:items-center">
                <Magnetic>
                  <a href={APP_URL} className="w-full sm:w-auto">
                    <Button size="lg" className="group w-full sm:w-auto">
                      Launch Platform
                      <ArrowRight className="h-4 w-4 transition-transform duration-300 group-hover:translate-x-1" />
                    </Button>
                  </a>
                </Magnetic>
                <a href={SIGNUP_URL} className="w-full sm:w-auto">
                  <Button size="lg" variant="secondary" className="w-full sm:w-auto">
                    Create free account
                  </Button>
                </a>
              </div>
            </Reveal>

            <Reveal delay={0.15}>
              <BrainScanner />
              <p className="mt-3 px-1 font-mono text-[11px] leading-relaxed text-white/35">
                // looping demo of the evaluation pipeline · not live market data
              </p>
            </Reveal>
          </div>
        </div>
        </div>
      </div>
    </section>
  );
}
