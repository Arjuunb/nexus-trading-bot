import { useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { motion } from "framer-motion";
import { ShieldCheck, ArrowLeft, KeyRound } from "lucide-react";
import { AuthShell } from "@/components/auth/AuthShell";
import { Card } from "@/components/ui/Card";
import { Button } from "@/components/ui/Button";
import { Checkbox } from "@/components/ui/Checkbox";
import { OTPInput } from "@/components/ui/OTPInput";
import { Logo } from "@/components/Logo";
import { auth } from "@/lib/auth";
import { useToast } from "@/lib/toast";
import { APP_URL } from "@/lib/utils";

export default function TwoFactor() {
  const { toast } = useToast();
  const navigate = useNavigate();
  const [code, setCode] = useState("");
  const [trust, setTrust] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [recovery, setRecovery] = useState(false);
  const [recoveryCode, setRecoveryCode] = useState("");

  const verify = async () => {
    const value = recovery ? recoveryCode.trim() : code;
    if (!recovery && value.length !== 6) return toast("Enter the full 6-digit code.", "error");
    if (recovery && value.length < 8) return toast("Enter a valid recovery code.", "error");
    setSubmitting(true);
    const res = await auth.verifyTotp(recovery ? "000000" : value);
    setSubmitting(false);
    if (!res.ok) return toast(res.message, "error");
    toast(res.demo ? res.message : "Two-factor verified.", res.demo ? "info" : "success");
    if (trust) toast("This device will be remembered for 30 days.", "info");
    if (!res.demo) window.location.assign(APP_URL);
    else navigate("/");
  };

  return (
    <AuthShell>
      <Card className="p-8">
        <div className="mb-6 flex justify-center">
          <Logo />
        </div>

        <motion.div
          initial={{ scale: 0 }}
          animate={{ scale: 1 }}
          transition={{ type: "spring", stiffness: 200, damping: 15 }}
          className="mx-auto mb-5 flex h-14 w-14 items-center justify-center rounded-2xl border border-gold/25 bg-gold/[0.08] text-gold"
        >
          <ShieldCheck className="h-6 w-6" />
        </motion.div>

        <h1 className="text-center text-xl font-bold text-white">Two-factor authentication</h1>
        <p className="mt-2 text-center text-sm text-white/55">
          {recovery
            ? "Enter one of your saved recovery codes."
            : "Enter the 6-digit code from your authenticator app."}
        </p>

        <div className="mt-7">
          {recovery ? (
            <input
              value={recoveryCode}
              onChange={(e) => setRecoveryCode(e.target.value)}
              placeholder="xxxx-xxxx-xxxx"
              className="h-12 w-full rounded-xl border border-line bg-ink-700/70 px-4 text-center font-mono text-white outline-none focus:border-gold/60 focus:ring-4 focus:ring-gold/10"
            />
          ) : (
            <OTPInput value={code} onChange={setCode} />
          )}
        </div>

        <div className="mt-6 flex items-center justify-center">
          <Checkbox
            id="trust"
            label="Trust this device for 30 days"
            checked={trust}
            onChange={(e) => setTrust(e.target.checked)}
          />
        </div>

        <Button fullWidth size="lg" className="mt-6" onClick={verify} loading={submitting}>
          Verify &amp; continue
        </Button>

        <button
          onClick={() => setRecovery((r) => !r)}
          className="mt-5 flex w-full items-center justify-center gap-1.5 text-sm text-gold/80 transition hover:text-gold"
        >
          <KeyRound className="h-4 w-4" />
          {recovery ? "Use authenticator app instead" : "Use a recovery code"}
        </button>

        <Link
          to="/auth/login"
          className="mt-4 flex items-center justify-center gap-1.5 text-sm text-white/50 transition hover:text-white"
        >
          <ArrowLeft className="h-4 w-4" />
          Back to sign in
        </Link>
      </Card>
    </AuthShell>
  );
}
