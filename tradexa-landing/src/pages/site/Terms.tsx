import { LegalShell, type LegalSection } from "@/components/site/legal/LegalShell";
import { useRouteMeta } from "@/site/seo";
import { routeFor } from "@/site/routes";

/**
 * Terms of service.
 *
 * States what the software does and does not do, what is expected of the
 * person using it, and how the arrangement ends. It deliberately does not name
 * a governing jurisdiction, a legal entity or a dispute forum — those are
 * company facts rather than product facts, and a specific one written here
 * would be an invention rather than a term.
 */
const SECTIONS: LegalSection[] = [
  {
    id: "agreement",
    heading: "The agreement",
    body: [
      "These terms govern your use of TradeLogX Nexus — the web application, the API, the dashboard and the client libraries. Creating an account means accepting them.",
      "They are written to be read. Where a clause below is unusual or costs you something, it says so rather than being buried in a list.",
    ],
  },
  {
    id: "what-it-is",
    heading: "What the service is",
    body: [
      "TradeLogX Nexus is trading software. It analyses market data, scores potential trades, enforces the risk limits you configure, and fills the resulting orders on a paper account against live market data. Live order routing to an exchange is locked today; if it is enabled, orders will be placed only on exchanges you have connected, using credentials you supply.",
      "It is a tool you operate. You choose the strategies, the risk budget, the symbols and the paper capital. The system executes within those bounds and records what it did.",
    ],
  },
  {
    id: "what-it-isnt",
    heading: "What the service is not",
    body: [
      "This is the most important section on the page, so it is stated plainly rather than in the negative space of the one above.",
      [
        "It is not investment advice, and nothing it outputs is a recommendation to buy or sell anything.",
        "It is not a broker, an exchange or a custodian. It never holds your funds; your capital stays at venues you control.",
        "It is not a managed service or a discretionary manager. Nobody here trades on your behalf or reviews your positions.",
        "It is not a guarantee of profit, and no figure shown anywhere on this site is a promise about your results.",
      ],
      "If you need advice about whether trading is appropriate for you, that comes from a licensed adviser who knows your circumstances, not from software.",
    ],
  },
  {
    id: "eligibility",
    heading: "Eligibility and your account",
    body: [
      "You must be of legal age to enter a contract where you live, and permitted to trade the instruments you intend to trade there. Checking that is your responsibility; we cannot check it for you.",
      "You are responsible for your account credentials and for anything done through your account. Enable two-factor authentication. If you believe your account has been accessed by someone else, tell us immediately through the support center.",
      "One person or entity per account. Sharing credentials means sharing every consequence of what is done with them.",
    ],
  },
  {
    id: "your-responsibilities",
    heading: "Your responsibilities",
    body: [
      [
        "Supplying exchange keys with the correct scope — trading enabled, withdrawal not. Keys carrying withdrawal permission are refused.",
        "Setting a risk budget you can actually afford to lose, and reviewing it as your circumstances change.",
        "Understanding the strategies you enable before you enable them with real capital. Paper mode exists precisely so this costs nothing.",
        "Monitoring your account. Automation reduces the work; it does not remove the responsibility.",
        "Complying with the rules of the venues you connect and the law where you are.",
      ],
    ],
  },
  {
    id: "acceptable-use",
    heading: "Acceptable use",
    body: [
      "Do not use the platform to manipulate markets, to trade on information you should not have, to launder funds, or to evade sanctions or the rules of a venue.",
      "Do not attempt to access accounts or data that are not yours, disrupt the service for others, or circumvent rate limits and authentication. Security research is welcome — report findings through the support center rather than testing against other people's accounts.",
      "We may suspend an account immediately where we reasonably believe this section is being breached, and will explain why unless legally prevented.",
    ],
  },
  {
    id: "availability",
    heading: "Availability, and what happens when it breaks",
    body: [
      "We aim for continuous availability and publish real operational status, including incidents, on the status page. We do not promise uninterrupted service, because no honest operator can.",
      "The system is designed to fail closed: if the risk service is unreachable, trading stops rather than continuing unchecked. A halt is the intended behaviour of a failure, not a symptom of one.",
      "Exchanges have their own outages, rate limits and maintenance windows. When a venue is degraded, orders to it may be delayed, rejected or partially filled, and that is outside anything we control.",
    ],
  },
  {
    id: "fees",
    heading: "Fees and billing",
    body: [
      "Subscription fees, the billing period and anything included in your plan are shown at the point of purchase and in your billing settings. Changes to pricing apply from your next billing period, never retroactively.",
      "Exchange fees, funding costs and spread are charged by the venue and are not ours to set, waive or refund.",
      "Cancel at any time from billing settings. Access continues to the end of the period you have paid for.",
    ],
  },
  {
    id: "liability",
    heading: "Liability",
    body: [
      "Trading losses are yours. That is not a disclaimer designed to be skipped — it is the fundamental shape of the arrangement, and if it is not acceptable then this is not the right product.",
      "We are responsible for operating the software as described, protecting your credentials as described in the privacy policy, and telling you promptly when something goes wrong.",
      "We are not liable for market outcomes, for the acts or outages of exchanges, or for losses arising from configuration you chose. Nothing in this section limits liability that cannot lawfully be limited.",
    ],
  },
  {
    id: "ip",
    heading: "Intellectual property",
    body: [
      "The platform, its interface and its documentation remain ours. Components we publish as open source are governed by their own licences, which are named on the open-source page and always take precedence for that code.",
      "Your strategies, configuration and trading history are yours. We claim no ownership over them and do not use them to train models that serve other accounts.",
    ],
  },
  {
    id: "termination",
    heading: "Ending the agreement",
    body: [
      "You can close your account at any time. Doing so revokes exchange keys immediately and starts the deletion schedule in the privacy policy. Export anything you want to keep first.",
      "We may end the agreement for breach of the acceptable-use section, for non-payment after notice, or if we stop operating the service — in which case you get reasonable notice and time to export.",
      "Closing an account does not close positions on your exchanges. Those are yours and remain open until you close them.",
    ],
  },
  {
    id: "changes",
    heading: "Changes to these terms",
    body: [
      "Material changes are announced in the product and by email before taking effect, described in plain language rather than only as a diff. Continuing to use the platform after they take effect means accepting them; if you would rather not, close the account and the previous terms govern everything up to that point.",
    ],
  },
];

export default function TermsPage() {
  const route = routeFor("/terms")!;
  useRouteMeta(route);

  return (
    <LegalShell
      title="Terms of service"
      summary="What the software does, what it explicitly does not do, what is expected of you, and how the arrangement ends. The section on what this is not is the one worth reading twice."
      updated="2026-07-30"
      sections={SECTIONS}
    />
  );
}
