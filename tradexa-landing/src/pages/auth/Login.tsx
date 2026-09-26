import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { useForm } from "react-hook-form";
import { zodResolver } from "@hookform/resolvers/zod";
import { AnimatePresence } from "framer-motion";
import { Check, Lock, Mail } from "lucide-react";
import { SocialButtons } from "@/components/auth/SocialButtons";
import { FormAlert } from "@/components/auth/FormAlert";
import { EmailSuggestion } from "@/components/auth/EmailSuggestion";
import { Button } from "@/components/ui/Button";
import { Input } from "@/components/ui/Input";
import { Field } from "@/components/ui/Field";
import { Checkbox } from "@/components/ui/Checkbox";
import { loginSchema, type LoginValues } from "@/lib/validation";
import { auth } from "@/lib/auth";
import { OFFLINE, friendlyAuthError, suggestEmail } from "@/lib/authHelp";
import { APP_URL } from "@/lib/utils";

type Phase = "idle" | "submitting" | "success";

// A verification email can be asked for again after this many seconds; the
// auth provider rate-limits resends, so the button says when it can be used.
const RESEND_COOLDOWN_S = 45;

/** Sign-in form. Rendered inside AuthSplit, which owns the page frame. */
export default function Login({ email = "", onEmailChange }: { email?: string; onEmailChange?: (email: string) => void }) {
  const [phase, setPhase] = useState<Phase>("idle");
  const [error, setError] = useState<string | null>(null);
  const [unverified, setUnverified] = useState<string | null>(null);
  const [resend, setResend] = useState<{ state: "idle" | "sending" | "sent" | "failed"; message?: string }>({ state: "idle" });
  const [cooldown, setCooldown] = useState(0);
  const [emailTouched, setEmailTouched] = useState(false);
  const {
    register,
    handleSubmit,
    watch,
    setValue,
    formState: { errors },
  } = useForm<LoginValues>({ resolver: zodResolver(loginSchema), mode: "onBlur", defaultValues: { email } });

  const emailValue = watch("email") ?? "";
  const suggestion = emailTouched && !errors.email ? suggestEmail(emailValue) : null;
  useEffect(() => { onEmailChange?.(emailValue); }, [emailValue, onEmailChange]);

  useEffect(() => {
    if (cooldown <= 0) return;
    const t = window.setTimeout(() => setCooldown((c) => c - 1), 1000);
    return () => window.clearTimeout(t);
  }, [cooldown]);

  const onSubmit = async (values: LoginValues) => {
    setPhase("submitting");
    setError(null);
    setUnverified(null);
    setResend({ state: "idle" });
    let res;
    try {
      res = await auth.signIn(values.email, values.password, values.remember ?? false);
    } catch {
      res = { ok: false, message: OFFLINE, code: undefined };
    }
    if (!res.ok) {
      setPhase("idle");
      if (res.code === "email_not_verified") setUnverified(values.email);
      else setError(friendlyAuthError(res.message));
      return;
    }
    setPhase("success");
    window.location.assign(APP_URL);
  };

  const resendVerification = async () => {
    if (!unverified) return;
    setResend({ state: "sending" });
    let res;
    try {
      res = await auth.resendVerification(unverified);
    } catch {
      res = { ok: false, message: OFFLINE };
    }
    setResend(res.ok
      ? { state: "sent", message: `Sent to ${unverified}. Open the link in it, then sign in. Check spam if it isn't there.` }
      : { state: "failed", message: friendlyAuthError(res.message) });
    setCooldown(RESEND_COOLDOWN_S);
  };

  const busy = phase !== "idle";

  return (
    <>
      <h1 className="text-2xl font-bold tracking-tight text-white">Welcome back</h1>
      <p className="mt-1.5 text-sm text-white/50">Sign in to your TradeLogX Nexus workspace.</p>
      {!auth.configured && (
        <FormAlert tone="error" className="mt-4">
          Authentication is not configured on this deployment. Contact the administrator.
        </FormAlert>
      )}

      <form onSubmit={handleSubmit(onSubmit)} className="mt-7" noValidate aria-busy={busy}>
        <fieldset disabled={busy} className="space-y-4">
          <Field
            label="Email"
            htmlFor="email"
            error={errors.email?.message}
            description={suggestion ? (
              <EmailSuggestion suggestion={suggestion} onApply={() => setValue("email", suggestion, { shouldValidate: true })} />
            ) : undefined}
          >
            <Input
              id="email"
              type="email"
              autoComplete="email"
              inputMode="email"
              spellCheck={false}
              placeholder="you@company.com"
              icon={<Mail className="h-4 w-4" />}
              invalid={!!errors.email}
              {...register("email", { onBlur: () => setEmailTouched(true) })}
            />
          </Field>

          <Field
            label="Password"
            htmlFor="password"
            error={errors.password?.message}
            hint={
              <Link to="/auth/forgot-password" className="rounded text-xs text-gold/80 transition-colors hover:text-gold focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-gold/60">
                Forgot password?
              </Link>
            }
          >
            <Input
              id="password"
              type="password"
              autoComplete="current-password"
              placeholder="Your password"
              icon={<Lock className="h-4 w-4" />}
              invalid={!!errors.password}
              {...register("password")}
            />
          </Field>

          <Checkbox id="remember" label="Keep me signed in for 30 days" {...register("remember")} />

          <AnimatePresence initial={false}>
            {error && <FormAlert key="error" tone="error">{error}</FormAlert>}
            {unverified && (
              <FormAlert key="unverified" tone="info">
                <p>Confirm your email first: open the link we sent to <b className="font-semibold">{unverified}</b>.</p>
                <div className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1">
                  <button
                    type="button"
                    onClick={() => void resendVerification()}
                    disabled={resend.state === "sending" || cooldown > 0}
                    className="rounded font-semibold text-gold underline decoration-gold/40 underline-offset-2 transition-colors hover:text-gold-soft focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-gold/60 disabled:cursor-not-allowed disabled:no-underline disabled:opacity-60"
                  >
                    {resend.state === "sending" ? "Sending…" : cooldown > 0 ? `Resend available in ${cooldown}s` : "Resend verification email"}
                  </button>
                </div>
                {resend.message ? (
                  <p className={resend.state === "failed" ? "mt-2 text-loss-soft" : "mt-2 text-emerald-soft"} role="status">
                    {resend.message}
                  </p>
                ) : null}
              </FormAlert>
            )}
            {phase === "success" && <FormAlert key="ok" tone="success">Signed in. Opening your dashboard…</FormAlert>}
          </AnimatePresence>

          <Button type="submit" fullWidth size="lg" loading={phase === "submitting"} disabled={!auth.configured}>
            {phase === "success" ? <><Check className="h-4 w-4" aria-hidden="true" /> Signed in</>
              : phase === "submitting" ? "Signing in…" : "Sign in"}
          </Button>
        </fieldset>
      </form>

      <SocialButtons />

      <p className="mt-8 text-center text-sm text-white/50">
        New to TradeLogX Nexus?{" "}
        <Link to="/auth/register" className="rounded font-medium text-gold transition-colors hover:text-gold-soft focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-gold/60">
          Create an account
        </Link>
      </p>
    </>
  );
}
