import { useState } from "react";
import { Link } from "react-router-dom";
import { useForm } from "react-hook-form";
import { zodResolver } from "@hookform/resolvers/zod";
import { AnimatePresence } from "framer-motion";
import { Check, Lock, Mail } from "lucide-react";
import { SocialButtons } from "@/components/auth/SocialButtons";
import { FormAlert } from "@/components/auth/FormAlert";
import { Button } from "@/components/ui/Button";
import { Input } from "@/components/ui/Input";
import { Field } from "@/components/ui/Field";
import { Checkbox } from "@/components/ui/Checkbox";
import { loginSchema, type LoginValues } from "@/lib/validation";
import { auth } from "@/lib/auth";
import { APP_URL } from "@/lib/utils";

type Phase = "idle" | "submitting" | "success";

const OFFLINE = "Couldn't reach the server. Check your connection and try again.";

/** Sign-in form. Rendered inside AuthSplit, which owns the page frame. */
export default function Login() {
  const [phase, setPhase] = useState<Phase>("idle");
  const [error, setError] = useState<string | null>(null);
  const {
    register,
    handleSubmit,
    formState: { errors },
  } = useForm<LoginValues>({ resolver: zodResolver(loginSchema), mode: "onBlur" });

  const onSubmit = async (values: LoginValues) => {
    setPhase("submitting");
    setError(null);
    let res;
    try {
      res = await auth.signIn(values.email, values.password, values.remember ?? false);
    } catch {
      res = { ok: false, message: OFFLINE };
    }
    if (!res.ok) {
      setPhase("idle");
      setError(res.message);
      return;
    }
    setPhase("success");
    window.location.assign(APP_URL);
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
          <Field label="Email" htmlFor="email" error={errors.email?.message}>
            <Input
              id="email"
              type="email"
              autoComplete="email"
              inputMode="email"
              spellCheck={false}
              placeholder="you@company.com"
              icon={<Mail className="h-4 w-4" />}
              invalid={!!errors.email}
              {...register("email")}
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
