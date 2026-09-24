import { LegalShell, type LegalSection } from "@/components/site/legal/LegalShell";
import { useRouteMeta } from "@/site/seo";
import { routeFor } from "@/site/routes";

/**
 * Privacy policy.
 *
 * Written to describe what the product actually does — the data it needs in
 * order to place an order and journal the result — rather than to enumerate
 * every permission a lawyer could imagine wanting later. It deliberately makes
 * no claim about a legal entity, jurisdiction or regulator, because those are
 * facts about the company rather than the software, and inventing them here
 * would be worse than leaving them to be added.
 */
const SECTIONS: LegalSection[] = [
  {
    id: "scope",
    heading: "What this covers",
    body: [
      "This policy describes the information TradeLogX Nexus collects when you use the platform, why each piece is collected, how long it is kept and who else ever sees it. It applies to the web application, the API and the dashboard.",
      "It does not cover the exchanges you connect. When you place an order, that order and the account it belongs to are governed by the venue's own terms and privacy policy, which we cannot alter on your behalf.",
    ],
  },
  {
    id: "what-we-collect",
    heading: "What we collect",
    body: [
      "Three categories, and nothing outside them:",
      [
        "Account data — the email address you sign up with, a password hash, and the two-factor secret if you enable it. We never store your password.",
        "Connection data — exchange API keys, encrypted before storage and decrypted only in memory when needed; the venue, the permissions the exchange reports for the key, and whether it is restricted to trusted IP addresses.",
        "Operating data — the decisions the engine made for your account, the orders and fills that resulted, the journal entries and lessons attached to them, your configuration, and the audit log of changes you made.",
      ],
      "We also record ordinary technical logs — request timestamps, source addresses, user agents and error traces — because an incident that cannot be reconstructed cannot be fixed.",
    ],
  },
  {
    id: "what-we-dont",
    heading: "What we do not collect",
    body: [
      "We do not collect the contents of accounts you have not connected, and we do not read balances or positions on venues you have not authorised.",
      "We do not sell data, and we do not share it with advertisers or data brokers. There is no advertising business here to fund with it.",
      "We do not run third-party analytics that follow you across other sites. Usage measurement is first-party and aggregate.",
    ],
  },
  {
    id: "keys",
    heading: "Exchange keys, specifically",
    body: [
      "An exchange key is the most sensitive thing you give us, so it is worth being exact. It is sent from the form to the key vault over TLS and encrypted with AES-256-GCM before the response returns. It is never placed in a session and never shown again after you enter it.",
      "It is decrypted only in memory, when it is needed — today, to ask the exchange what the key may do, because live order routing is locked. The master key that protects it lives only in the server's environment, never in a database or a backup. Secrets are redacted in the response filter and the logger rather than at each call site, so a new endpoint cannot leak one by omission.",
      "Before a key is stored, the exchange is asked what it may do, and a key that can withdraw or transfer funds is refused. This is a structural limit rather than a policy: the worst case of a compromise is unwanted trading, not a drained account.",
    ],
  },
  {
    id: "why",
    heading: "Why each piece is collected",
    body: [
      "Account data exists so you can sign in and so we can reach you about your account. Connection data exists because an order cannot be placed without it. Operating data exists because the product's entire proposition is that decisions are explainable and outcomes are remembered — a journal you cannot keep is not a journal.",
      "Technical logs exist for security and debugging, and are the basis of the audit trail you can inspect yourself.",
    ],
  },
  {
    id: "retention",
    heading: "How long we keep it",
    body: [
      [
        "Account data — for as long as the account exists, then deleted within 30 days of closure.",
        "Exchange keys — until you revoke them, or immediately on account closure. Revocation is immediate and irreversible.",
        "Operating data — for the life of the account, because its value is cumulative. You can export or delete it at any time.",
        "Technical logs — 90 days, then discarded. Audit-log entries are retained for the life of the account because their purpose is to be consultable after the fact.",
      ],
      "Backups are encrypted with the key vault's master key and kept for a week; deleted data disappears from backups as those rotate rather than instantly.",
    ],
  },
  {
    id: "sharing",
    heading: "Who else sees it",
    body: [
      "Infrastructure providers who host the platform and process payments see what they must in order to do that, under contract, and nothing further. They cannot decrypt exchange keys.",
      "We will disclose data if compelled by valid legal process. Where we are permitted to tell you that this has happened, we will.",
      "Nobody else. There is no partner programme, no data-sharing arrangement and no analytics vendor with a copy.",
    ],
  },
  {
    id: "rights",
    heading: "Your rights over it",
    body: [
      [
        "Export — take your full operating history in a machine-readable format, at any time, without asking.",
        "Correction — change anything about your account from the settings pages.",
        "Deletion — close the account and have its data removed on the schedule above.",
        "Objection — decline non-essential processing without losing access to the product.",
      ],
      "These are available in the product rather than by request. A right you have to email someone to exercise is a right with friction attached.",
    ],
  },
  {
    id: "security",
    heading: "How it is protected",
    body: [
      "Envelope encryption with per-tenant data keys, TLS in transit (1.3 only on the API host), authentication on every request whatever address it comes from, and an append-only, hash-chained audit log that no product code path can amend. The security page describes the architecture in detail, including what each boundary actually re-checks.",
    ],
  },
  {
    id: "changes",
    heading: "Changes to this policy",
    body: [
      "Material changes are announced in the product and by email before they take effect, with the change described in plain language rather than only as a new version of the document. The date at the top of this page always reflects the current version.",
    ],
  },
];

export default function PrivacyPage() {
  const route = routeFor("/privacy")!;
  useRouteMeta(route);

  return (
    <LegalShell
      title="Privacy policy"
      summary="What we collect, why we need it, how long it stays and who else ever sees it. Exchange keys get their own section, because they are the most sensitive thing you hand over and deserve more than a sentence."
      updated="2026-09-24"
      sections={SECTIONS}
    />
  );
}
