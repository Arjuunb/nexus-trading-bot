import { useState } from "react";
import { Link, useLocation, useNavigate } from "react-router-dom";
import { motion } from "framer-motion";
import { MailCheck, ArrowLeft, CheckCircle2 } from "lucide-react";
import { AuthShell } from "@/components/auth/AuthShell";
import { Card } from "@/components/ui/Card";
import { Button } from "@/components/ui/Button";
import { Logo } from "@/components/Logo";
import { auth } from "@/lib/auth";
import { initialAuthRedirect } from "@/lib/supabase";
import { FormAlert } from "@/components/auth/FormAlert";
import { useToast } from "@/lib/toast";

export default function VerifyEmail() {
  const { toast } = useToast();
  const location = useLocation();
  const navigate = useNavigate();
  const email = (location.state as { email?: string } | null)?.email;
  const [resending, setResending] = useState(false);
  const [cooldown, setCooldown] = useState(0);

  const resend = async () => {
    if (!email) return toast("No email on file — please register again.", "error");
    setResending(true);
    const res = await auth.resendVerification(email);
    setResending(false);
    toast(res.message, res.ok ? "success" : "error");
    if (res.ok) {
      setCooldown(30);
      const iv = window.setInterval(() => {
        setCooldown((c) => {
          if (c <= 1) {
            window.clearInterval(iv);
            return 0;
          }
          return c - 1;
        });
      }, 1000);
    }
  };

  // Arrived from the confirmation email itself: say whether it worked.
  const link = initialAuthRedirect;
  if (link && (link.error || (link.hasToken && link.type !== "recovery"))) {
    const failed = Boolean(link.error);
    return (
      <AuthShell>
        <Card className="p-8 text-center">
          <div className="mb-6 flex justify-center">
            <Logo />
          </div>
          {failed ? (
            <>
              <h1 className="text-xl font-bold text-white">That link didn&apos;t work</h1>
              <FormAlert tone="error" className="mt-5 text-left">
                {/expired|invalid/i.test(link.error ?? "")
                  ? "This confirmation link has expired or has already been used."
                  : link.error}{" "}
                Sign in and we&apos;ll offer to send a fresh one.
              </FormAlert>
            </>
          ) : (
            <>
              <motion.div
                initial={{ scale: 0.6, opacity: 0 }}
                animate={{ scale: 1, opacity: 1 }}
                transition={{ type: "spring", stiffness: 220, damping: 18 }}
                className="mx-auto mb-5 flex h-16 w-16 items-center justify-center rounded-2xl border border-emerald/30 bg-emerald/10"
              >
                <CheckCircle2 className="h-8 w-8 text-emerald-soft" aria-hidden="true" />
              </motion.div>
              <h1 className="text-xl font-bold text-white">Email confirmed</h1>
              <p className="mt-2 text-sm leading-relaxed text-white/55" role="status">
                Your account is ready. Sign in to open your workspace.
              </p>
            </>
          )}
          <Button fullWidth size="lg" className="mt-7" onClick={() => navigate("/auth/login")}>Continue to sign in</Button>
        </Card>
      </AuthShell>
    );
  }

  return (
    <AuthShell>
      <Card className="p-8 text-center">
        <div className="mb-6 flex justify-center">
          <Logo />
        </div>

        {/* animated success illustration */}
        <motion.div
          initial={{ scale: 0, rotate: -12 }}
          animate={{ scale: 1, rotate: 0 }}
          transition={{ type: "spring", stiffness: 200, damping: 15 }}
          className="mx-auto mb-6 flex h-20 w-20 items-center justify-center rounded-2xl border border-gold/25 bg-gold/[0.08]"
        >
          <span className="absolute h-20 w-20 animate-pulse-ring rounded-2xl" />
          <MailCheck className="h-9 w-9 text-gold" />
        </motion.div>

        <h1 className="text-xl font-bold text-white">Verify your email</h1>
        <p className="mt-2 text-sm leading-relaxed text-white/55">
          We&apos;ve sent a verification link to{" "}
          <span className="font-medium text-white">{email ?? "your inbox"}</span>. Click the link to
          activate your TradeLogX Nexus account.
        </p>

        <Button
          fullWidth
          size="lg"
          className="mt-7"
          onClick={resend}
          loading={resending}
          disabled={cooldown > 0}
        >
          {cooldown > 0 ? `Resend in ${cooldown}s` : "Resend email"}
        </Button>

        <p className="mt-4 text-xs text-white/40">
          Wrong address or didn&apos;t get it? Check spam, or resend above.
        </p>

        <Link
          to="/auth/login"
          className="mt-6 flex items-center justify-center gap-1.5 text-sm text-white/50 transition hover:text-white"
        >
          <ArrowLeft className="h-4 w-4" />
          Back to sign in
        </Link>
      </Card>
    </AuthShell>
  );
}
