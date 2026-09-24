import { Link } from "react-router-dom";
import { motion } from "framer-motion";
import {
  GitMerge,
  GitPullRequest,
  Heart,
  MessagesSquare,
  Scale,
  Sparkles,
  Users,
} from "lucide-react";
import { CommunityBackdrop } from "@/components/site/backdrops";
import { useRouteMeta } from "@/site/seo";
import { routeFor, prefetchRoute } from "@/site/routes";
import { REPO_URL } from "@/site/platform";

const EASE = [0.22, 1, 0.36, 1] as const;

const CHANNELS = [
  {
    icon: MessagesSquare,
    name: "Issues",
    what: "Bugs, configuration questions and post-mortems on trades that went wrong, on the public repository. The most useful threads are the ones about losses.",
    tone: "Public. Answered by the maintainer; not a support queue with a guarantee.",
    planned: false,
  },
  {
    icon: GitPullRequest,
    name: "Proposals",
    what: "Changes to the product, argued in a proposal issue before they are built. The form asks for evidence — for anything that touches trading decisions, the backtest or forward-test record.",
    tone: "Where roadmap decisions happen.",
    planned: false,
  },
  {
    icon: Sparkles,
    name: "Strategy exchange",
    what: "Not built yet. The plan: published strategies with their full parameter history and out-of-sample record attached, and no listing without the record.",
    tone: "Planned — not available today.",
    planned: true,
  },
];

const FLOW = [
  ["Proposal", "An issue on the proposal form: the problem, the change, the evidence."],
  ["Argument", "Comments on the issue, in public, until the approach is agreed or declined."],
  ["Pull request", "The change, with a test that fails before it and passes after, linked to the issue."],
  ["Checks", "CI runs every test suite, both front-end builds and all four SDK suites."],
  ["Merge", "Reviewed in public by the maintainer; the issue closes with the pull request."],
];

const CONDUCT = [
  "No signal-selling, no referral links, no performance claims without the record attached. A screenshot of a P&L is not evidence of anything.",
  "Disagree with the argument. Reviews and threads are public and permanent, and a technical disagreement is not a personal one.",
  "No financial advice, given or requested. Discuss method and mechanism; leave 'should I buy this' to a licensed adviser who knows your situation.",
  "Assume the person asking a basic question has been trading for a week. Everyone here was.",
];

export default function CommunityPage() {
  const route = routeFor("/community")!;
  useRouteMeta(route);

  return (
    <>
      <CommunityBackdrop />

      <section className="container-x pt-32 sm:pt-40">
        <motion.div
          initial={{ opacity: 0, y: 16 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ duration: 0.6, ease: EASE }}
          className="max-w-2xl"
        >
          <span className="inline-flex items-center gap-2 font-mono text-[11px] uppercase tracking-[0.22em] text-gold/80">
            <Users className="h-3.5 w-3.5" />
            Community
          </span>
          <h1 className="mt-5 text-balance text-4xl font-extrabold leading-[1.05] tracking-tight text-white sm:text-5xl">
            Where the arguments happen
          </h1>
          <p className="mt-6 text-[17px] leading-relaxed text-white/55">
            Trading communities usually optimise for confidence. This one is set up for the
            opposite: it lives on the public repository, product proposals are argued before they
            are built, every review happens in the open, and the threads worth reading are the
            ones about trades that lost.
          </p>
        </motion.div>
      </section>

      {/* channels */}
      <section className="container-x mt-14">
        <div className="grid gap-3 lg:grid-cols-3">
          {CHANNELS.map((c, i) => (
            <motion.div
              key={c.name}
              initial={{ opacity: 0, y: 14 }}
              whileInView={{ opacity: 1, y: 0 }}
              viewport={{ once: true, margin: "-60px" }}
              transition={{ duration: 0.5, delay: i * 0.07, ease: EASE }}
              className="group relative overflow-hidden rounded-2xl border border-white/[0.08] bg-white/[0.02] p-6 transition-all duration-300 hover:-translate-y-0.5 hover:border-gold/25 hover:bg-white/[0.04]"
            >
              <span className="flex h-10 w-10 items-center justify-center rounded-xl border border-gold/25 bg-gold/[0.08] text-gold-soft transition-transform duration-300 group-hover:scale-110">
                <c.icon className="h-4.5 w-4.5" />
              </span>
              <h2 className="mt-5 flex items-center gap-2 text-lg font-semibold text-white">
                {c.name}
                {c.planned && (
                  <span className="rounded border border-white/15 px-1.5 py-0.5 font-mono text-[9px] font-normal uppercase tracking-wider text-white/45">
                    planned
                  </span>
                )}
              </h2>
              <p className="mt-2.5 text-sm leading-relaxed text-white/50">{c.what}</p>
              <p className="mt-4 border-t border-white/[0.07] pt-3 font-mono text-[10px] leading-relaxed text-white/30">
                {c.tone}
              </p>
              <span
                aria-hidden
                className="pointer-events-none absolute inset-x-0 bottom-0 h-px origin-left scale-x-0 bg-gradient-to-r from-transparent via-gold/50 to-transparent transition-transform duration-500 group-hover:scale-x-100"
              />
            </motion.div>
          ))}
        </div>

        <p className="mt-4 text-sm leading-relaxed text-white/35">
          Reading needs nothing; opening an issue or a proposal needs a GitHub account.{" "}
          <a
            href={`${REPO_URL}/issues`}
            target="_blank"
            rel="noreferrer"
            className="text-gold-soft underline-offset-2 hover:underline"
          >
            Open the issue tracker
          </a>
          .
        </p>
      </section>

      {/* proposal flow */}
      <section className="container-x mt-16">
        <div className="grid gap-8 rounded-2xl border border-white/[0.08] bg-black/30 p-6 backdrop-blur-sm sm:p-8 lg:grid-cols-[1fr_1fr] lg:gap-14">
          <div>
            <h2 className="flex items-center gap-2 text-2xl font-bold tracking-tight text-white">
              <GitMerge className="h-5 w-5 text-gold-soft" />
              From proposal to change
            </h2>
            <p className="mt-4 leading-relaxed text-white/55">
              Every change to the product can be traced back through the same five steps, and
              every step is public — so the reason something was built, or declined, is on
              record rather than in someone's memory.
            </p>
            <p className="mt-4 leading-relaxed text-white/40">
              A proposal that touches trading decisions needs evidence, and an improvement is a
              hypothesis until a backtest and a forward test say otherwise.
            </p>
          </div>

          <ul className="space-y-3">
            {FLOW.map(([k, v]) => (
              <li
                key={k}
                className="flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1 border-b border-white/[0.06] pb-3 last:border-0"
              >
                <span className="font-mono text-[10px] uppercase tracking-[0.14em] text-white/30">
                  {k}
                </span>
                <span className="text-[14px] text-white/65 sm:max-w-[70%] sm:text-right">{v}</span>
              </li>
            ))}
          </ul>
        </div>
      </section>

      {/* conduct */}
      <section className="container-x mt-8 pb-24">
        <div className="grid gap-8 lg:grid-cols-[0.9fr_1.1fr] lg:gap-14">
          <div>
            <h2 className="flex items-center gap-2 text-2xl font-bold tracking-tight text-white">
              <Scale className="h-5 w-5 text-gold-soft" />
              Code of conduct
            </h2>
            <p className="mt-4 leading-relaxed text-white/55">
              Four rules, enforced. They exist because a community around money attracts a
              specific kind of noise, and a policy nobody applies is decoration.
            </p>
            <p className="mt-4 flex items-start gap-2 text-sm leading-relaxed text-white/35">
              <Heart className="mt-0.5 h-4 w-4 shrink-0 text-white/25" />
              Breaches are raised privately first. Repeated ones lead to comments being removed
              and, if needed, the account being blocked from the repository. The rules are in
              CODE_OF_CONDUCT.md.
            </p>
          </div>

          <ul className="space-y-3">
            {CONDUCT.map((rule, i) => (
              <motion.li
                key={rule}
                initial={{ opacity: 0, x: -8 }}
                whileInView={{ opacity: 1, x: 0 }}
                viewport={{ once: true, margin: "-50px" }}
                transition={{ duration: 0.45, delay: i * 0.06, ease: EASE }}
                className="flex gap-4 rounded-xl border border-white/[0.07] bg-white/[0.015] p-4"
              >
                <span className="font-mono text-[11px] text-gold/50">
                  {String(i + 1).padStart(2, "0")}
                </span>
                <span className="text-[14px] leading-relaxed text-white/60">{rule}</span>
              </motion.li>
            ))}
          </ul>
        </div>

        <p className="mt-12 border-t border-white/[0.07] pt-6 text-sm leading-relaxed text-white/40">
          If you would rather contribute code than conversation, the{" "}
          <Link
            to="/open-source"
            onPointerEnter={() => prefetchRoute("/open-source")}
            className="text-gold-soft underline-offset-2 hover:underline"
          >
            open-source page
          </Link>{" "}
          covers what is public and how contributions are reviewed. Security findings go through
          the private disclosure route on the{" "}
          <Link to="/security" className="text-gold-soft underline-offset-2 hover:underline">
            security page
          </Link>
          , never a public channel.
        </p>
      </section>
    </>
  );
}
