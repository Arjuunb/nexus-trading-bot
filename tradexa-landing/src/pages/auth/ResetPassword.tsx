import { useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { useForm } from "react-hook-form";
import { zodResolver } from "@hookform/resolvers/zod";
import { AnimatePresence } from "framer-motion";
import { ArrowLeft, Lock } from "lucide-react";
import { AuthShell } from "@/components/auth/AuthShell";
import { FormAlert } from "@/components/auth/FormAlert";
import { PasswordMatch, PasswordStrength } from "@/components/auth/PasswordStrength";
import { Card } from "@/components/ui/Card";
import { Button } from "@/components/ui/Button";
import { Input } from "@/components/ui/Input";
import { Field } from "@/components/ui/Field";
import { Logo } from "@/components/Logo";
import { resetSchema, type ResetValues } from "@/lib/validation";
import { auth } from "@/lib/auth";
import { OFFLINE, friendlyAuthError } from "@/lib/authHelp";
import { initialAuthRedirect } from "@/lib/supabase";
import { useToast } from "@/lib/toast";

const LINK_DEAD = "This reset link has expired or has already been used. Request a new one; links work once and expire after a short time.";

/** "Auth session missing" means the page was opened without a working link. */
function resetError(message: string): { text: string; dead: boolean } {
  if (/session missing|session not found|jwt|expired/i.test(message)) return { text: LINK_DEAD, dead: true };
  return { text: friendlyAuthError(message), dead: false };
}

export default function ResetPassword() {
  const { toast } = useToast();
  const navigate = useNavigate();
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<{ text: string; dead: boolean } | null>(null);
  const linkError = initialAuthRedirect?.error ?? null;
  const {
    register,
    handleSubmit,
    watch,
    formState: { errors },
  } = useForm<ResetValues>({ resolver: zodResolver(resetSchema), mode: "onBlur", reValidateMode: "onChange" });

  const pw = watch("password") ?? "";
  const confirm = watch("confirmPassword") ?? "";

  const onSubmit = async (values: ResetValues) => {
    setSubmitting(true);
    setError(null);
    let res;
    try {
      res = await auth.updatePassword(values.password);
    } catch {
      res = { ok: false, message: OFFLINE };
    }
    setSubmitting(false);
    if (!res.ok) {
      setError(resetError(res.message));
      return;
    }
    toast(res.message, "success");
    navigate("/auth/login");
  };

  const requestNew = (
    <Link to="/auth/forgot-password" className="rounded font-semibold text-gold underline underline-offset-2 hover:text-gold-soft focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-gold/60">
      Send a new reset link
    </Link>
  );

  return (
    <AuthShell>
      <Card className="p-8">
        <div className="mb-6 flex justify-center">
          <Logo />
        </div>
        <h1 className="text-center text-xl font-bold text-white">Set a new password</h1>

        {linkError ? (
          <FormAlert tone="error" className="mt-6">
            <p>{/expired|invalid/i.test(linkError) ? LINK_DEAD : linkError}</p>
            <p className="mt-2">{requestNew}</p>
          </FormAlert>
        ) : (
          <>
            <p className="mt-2 text-center text-sm text-white/55">
              Choose a strong password you don&apos;t use anywhere else.
            </p>
            <form onSubmit={handleSubmit(onSubmit)} className="mt-6" noValidate aria-busy={submitting}>
              <fieldset disabled={submitting} className="space-y-4">
                <Field label="New password" htmlFor="password" error={errors.password?.message}
                  description={<PasswordStrength password={pw} />}>
                  <Input
                    id="password"
                    type="password"
                    autoComplete="new-password"
                    placeholder="Create a strong password"
                    icon={<Lock className="h-4 w-4" />}
                    invalid={!!errors.password}
                    {...register("password")}
                  />
                </Field>

                <Field label="Confirm password" htmlFor="confirmPassword" error={errors.confirmPassword?.message}
                  description={errors.confirmPassword ? undefined : <PasswordMatch password={pw} confirm={confirm} />}>
                  <Input
                    id="confirmPassword"
                    type="password"
                    autoComplete="new-password"
                    placeholder="Re-enter your password"
                    icon={<Lock className="h-4 w-4" />}
                    invalid={!!errors.confirmPassword}
                    {...register("confirmPassword")}
                  />
                </Field>

                <AnimatePresence initial={false}>
                  {error && (
                    <FormAlert key="error" tone="error">
                      <p>{error.text}</p>
                      {error.dead ? <p className="mt-2">{requestNew}</p> : null}
                    </FormAlert>
                  )}
                </AnimatePresence>

                <Button type="submit" fullWidth size="lg" loading={submitting}>
                  {submitting ? "Updating password…" : "Update password"}
                </Button>
              </fieldset>
            </form>
          </>
        )}

        <Link
          to="/auth/login"
          className="mt-6 flex items-center justify-center gap-1.5 rounded text-sm text-white/50 transition hover:text-white focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-gold/60"
        >
          <ArrowLeft className="h-4 w-4" aria-hidden="true" />
          Back to sign in
        </Link>
      </Card>
    </AuthShell>
  );
}
