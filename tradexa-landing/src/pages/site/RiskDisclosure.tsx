import { LegalShell, type LegalSection } from "@/components/site/legal/LegalShell";
import { useRouteMeta } from "@/site/seo";
import { routeFor } from "@/site/routes";

/**
 * Risk disclosure.
 *
 * The one page on this site with no persuasive job to do. Everything else
 * argues that the system is careful; this states what happens when careful is
 * not enough, including the failure modes that belong specifically to
 * automation and that a generic disclosure would not mention.
 */
const SECTIONS: LegalSection[] = [
  {
    id: "summary",
    heading: "The short version",
    body: [
      "Trading carries a substantial risk of loss. Most people who trade actively lose money, and automation does not change that statistic — it changes how quickly and how consistently your decisions are executed, which cuts in both directions.",
      "Only trade capital you can lose entirely without it affecting your life. If losing it would change your housing, your health or your obligations to other people, it is not capital for this.",
    ],
  },
  {
    id: "what-automation-changes",
    heading: "What automation actually changes",
    body: [
      "It removes hesitation, fatigue and the temptation to abandon a plan mid-trade. It enforces position sizing and loss limits without negotiation. It keeps a complete record you can learn from.",
      "It does not create an edge that was not there. A strategy with negative expectancy loses money faster when automated, not slower — the same flawed decision, made more often, with less friction between it and the market.",
    ],
  },
  {
    id: "what-it-doesnt",
    heading: "What automation does not change",
    body: [
      [
        "Market risk. Prices move against positions, sometimes far and fast, and no system prevents that.",
        "Gap risk. A stop is an instruction, not a guarantee. A market that gaps through your stop fills you on the other side of it, and the loss can exceed what you sized for.",
        "Liquidity risk. Thin books mean worse fills than the model assumed, and in stress the book you sized against may not be the book you trade into.",
        "Leverage. Borrowing amplifies both directions. Liquidation is a real outcome, not a theoretical one, and it can happen faster than any human or system reaction time.",
        "Counterparty risk. Exchanges have failed, frozen withdrawals and been hacked. Capital at a venue is exposed to that venue.",
      ],
    ],
  },
  {
    id: "automation-specific",
    heading: "Failure modes that belong to automation",
    body: [
      "These are the risks a generic disclosure will not mention, and they are the ones worth understanding before you rely on any result.",
      [
        "Overfitting. A strategy tuned until it looks excellent on history has usually been tuned to that history. The Strategy Lab can run the same rules across every symbol and timeframe, and validation judges a strategy on data held back from its design, because a result that holds in only one place is more likely luck than edge.",
        "Regime change. Conditions that produced an edge stop. A system does not notice it has become obsolete; it keeps executing with the same confidence.",
        "Correlated positions. Several trades that look independent can be one position wearing different tickers, and discover it simultaneously.",
        "Connectivity and venue failure. A dropped connection mid-position, a rejected order, an exchange in maintenance. Stops and targets are managed by the engine rather than held at an exchange, so while the engine is disconnected or stopped, an open position is not being managed — and a venue outage is still an outage.",
        "Misconfiguration. A risk budget entered as 5% instead of 0.5% behaves exactly as instructed. The system enforces the limits you set, including the wrong ones.",
        "Silent degradation. A data feed with gaps, a strategy trading conditions it was never meant to see. Gap detection and regime allowlists exist to catch this, and they are mitigations rather than guarantees.",
      ],
    ],
  },
  {
    id: "figures",
    heading: "About the figures on this site",
    body: [
      "Backtests are hypothetical. They benefit from knowing the period they run over, and no backtest reproduces the experience of holding a losing position with real money in it.",
      "Simulated results shown across this site — the terminal, the charts, the sample decisions — are generated locally for illustration. They represent no account and are labelled where they appear.",
      "Past performance, whether real or simulated, does not indicate future results. That sentence is a cliché because it keeps being true.",
    ],
  },
  {
    id: "your-responsibility",
    heading: "What remains yours",
    body: [
      "You choose the strategies, the risk budget, the venues and the capital. The system executes within those bounds — it does not decide whether those bounds were wise.",
      "Automation reduces the work of trading. It does not transfer the responsibility for it. Positions opened by software are your positions, and losses taken by software are your losses.",
      "Start in paper mode. It uses the live feed and the full decision path with simulated fills, and it costs nothing to find out that a strategy you believed in does not work.",
    ],
  },
  {
    id: "not-advice",
    heading: "This is not advice",
    body: [
      "Nothing on this site or produced by this software is investment, financial, legal or tax advice, and nothing is a recommendation to buy or sell any instrument.",
      "Whether trading is appropriate for you depends on your finances, your obligations and your tolerance for loss. That assessment comes from a licensed professional who knows your circumstances. It does not come from a piece of software, and it does not come from us.",
    ],
  },
  {
    id: "if-in-doubt",
    heading: "If you are not sure",
    body: [
      "Run a strategy in paper mode for long enough to see it in more than one kind of market. Live order routing is locked today, so paper is how every strategy runs: it trades live Binance market data with simulated fills and costs, and every decision is written down — you can see how a strategy behaves across a full month of real conditions without a dollar at risk.",
      "If trading is affecting your sleep, your finances or your relationships, that is a signal to stop rather than to optimise. Support for problem trading and gambling exists in most countries and is worth using.",
    ],
  },
];

export default function RiskDisclosurePage() {
  const route = routeFor("/risk-disclosure")!;
  useRouteMeta(route);

  return (
    <LegalShell
      title="Risk disclosure"
      summary="Trading carries a real risk of loss, and automation changes the shape of that risk without reducing it. This page states what can go wrong — including the failure modes that belong specifically to automated systems and that a generic disclosure would not mention."
      updated="2026-07-30"
      sections={SECTIONS}
    />
  );
}
