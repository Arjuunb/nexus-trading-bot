import { useState } from "react";
import { Button } from "@/components/ui/Button";
import { auth, type OAuthProvider } from "@/lib/auth";
import { useToast } from "@/lib/toast";

function GoogleIcon() {
  return <svg viewBox="0 0 24 24" className="h-4 w-4" aria-hidden="true"><path fill="#EA4335" d="M12 10.2v3.9h5.5c-.24 1.4-1.6 4.1-5.5 4.1-3.3 0-6-2.7-6-6.1s2.7-6.1 6-6.1c1.9 0 3.1.8 3.9 1.5l2.7-2.6C16.9 3.5 14.7 2.5 12 2.5 6.9 2.5 2.8 6.6 2.8 12S6.9 21.5 12 21.5c5.6 0 9.3-3.9 9.3-9.5 0-.6-.06-1-.16-1.8H12z" /></svg>;
}

function AppleIcon() {
  return <svg viewBox="0 0 24 24" className="h-4 w-4 fill-white" aria-hidden="true"><path d="M17.1 12.7c0-2 1.6-3 1.7-3.1-.9-1.4-2.4-1.6-2.9-1.6-1.2-.1-2.4.7-3 .7-.6 0-1.5-.7-2.5-.7-1.3 0-2.5.8-3.2 1.9-1.4 2.5-.4 6.2 1 8.3.7 1 1.5 2 2.5 1.9 1-.1 1.3-.6 2.5-.6s1.5.6 2.5.6c1 .0 1.7-.9 2.3-1.9.8-1.1 1.1-2.2 1.1-2.3-.1 0-2.1-.8-2.1-3.2zM15.1 6.7c.5-.7.9-1.6.8-2.5-.8 0-1.8.6-2.4 1.3-.5.6-.9 1.5-.8 2.4.9.1 1.8-.5 2.4-1.2z" /></svg>;
}

/** Providers are opt-in: a button is never displayed for an unconfigured OAuth
 *  provider, and neither is the "or continue with" divider that introduces them. */
export function SocialButtons({ divider = "or continue with" }: { divider?: string }) {
  const { toast } = useToast();
  const [busy, setBusy] = useState<OAuthProvider | null>(null);
  const providers = (["google", "apple"] as OAuthProvider[]).filter((provider) => auth.providerEnabled(provider));
  if (!providers.length) return null;
  const go = async (provider: OAuthProvider) => {
    setBusy(provider);
    const result = await auth.oauth(provider);
    if (!result.ok) toast(result.message, "error");
    setBusy(null);
  };
  return <>
    <div className="my-6 flex items-center gap-3">
      <span className="h-px flex-1 bg-line" />
      <span className="text-xs text-white/35">{divider}</span>
      <span className="h-px flex-1 bg-line" />
    </div>
    <div className={`grid gap-3 ${providers.length === 2 ? "grid-cols-2" : "grid-cols-1"}`}>
    {providers.map((provider) => <Button key={provider} variant="outline" onClick={() => go(provider)} loading={busy === provider} type="button">
      {busy !== provider && (provider === "google" ? <GoogleIcon /> : <AppleIcon />)}
      Continue with {provider === "google" ? "Google" : "Apple"}
    </Button>)}
    </div>
  </>;
}
