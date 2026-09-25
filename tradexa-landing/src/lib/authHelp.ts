// Plain-language help for the sign-in and sign-up forms. Nothing here talks
// to a server: it rewords known auth errors and spots likely email typos.

const OFFLINE = "Couldn't reach the server. Check your connection and try again.";

/** A clearer wording for the auth errors people actually hit; anything
 *  unrecognised is shown exactly as the server sent it. */
export function friendlyAuthError(message: string): string {
  const m = message.toLowerCase();
  if (m.includes("invalid login credentials")) {
    return "That email and password don't match an account. Check both, or reset your password.";
  }
  if (m.includes("user already registered") || m.includes("already been registered")) {
    return "An account with this email already exists. Sign in instead, or reset the password.";
  }
  if (m.includes("rate limit") || m.includes("too many requests") || m.includes("for security purposes")) {
    return "Too many attempts in a short time. Wait a minute, then try again.";
  }
  if (m.includes("failed to fetch") || m.includes("networkerror") || m.includes("load failed")) return OFFLINE;
  return message;
}

export { OFFLINE };

const DOMAINS = [
  "gmail.com", "googlemail.com", "yahoo.com", "yahoo.co.uk", "hotmail.com", "hotmail.co.uk", "outlook.com",
  "live.com", "msn.com", "icloud.com", "me.com", "aol.com", "proton.me", "protonmail.com", "gmx.com",
];

function distance(a: string, b: string): number {
  const row = Array.from({ length: b.length + 1 }, (_, i) => i);
  for (let i = 1; i <= a.length; i++) {
    let prev = row[0];
    row[0] = i;
    for (let j = 1; j <= b.length; j++) {
      const temp = row[j];
      row[j] = Math.min(row[j] + 1, row[j - 1] + 1, prev + (a[i - 1] === b[j - 1] ? 0 : 1));
      prev = temp;
    }
  }
  return row[b.length];
}

/** "alex@gmial.com" -> "alex@gmail.com"; null when the address looks fine
 *  or is not close to a common provider. A suggestion, never a correction. */
export function suggestEmail(email: string): string | null {
  const at = email.lastIndexOf("@");
  if (at < 1) return null;
  const local = email.slice(0, at);
  const domain = email.slice(at + 1).trim().toLowerCase();
  if (!domain || DOMAINS.includes(domain)) return null;
  let best: string | null = null;
  let bestScore = Infinity;
  for (const candidate of DOMAINS) {
    const d = distance(domain, candidate);
    if (d < bestScore) { best = candidate; bestScore = d; }
  }
  if (best && bestScore > 0 && bestScore <= 2) return `${local}@${best}`;
  const tld = domain.replace(/\.(con|cmo|ocm|cm|comm|om|vom|xom)$/, ".com");
  return tld !== domain ? `${local}@${tld}` : null;
}
