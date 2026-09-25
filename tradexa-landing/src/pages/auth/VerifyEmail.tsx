import { useState } from "react";
import { Link, useLocation } from "react-router-dom";
import { motion } from "framer-motion";
import { MailCheck, ArrowLeft } from "lucide-react";
import { AuthShell } from "@/components/auth/AuthShell";
import { Card } from "@/components/ui/Card";
import { Button } from "@/components/ui/Button";
import { Logo } from "@/components/Logo";
import { auth } from "@/lib/auth";
import { useToast } from "@/lib/toast";

export default function VerifyEmail() {
  const { toast } = useToast();
  const location = useLocation();
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
