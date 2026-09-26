import { useEffect, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { useForm } from "react-hook-form";
import { zodResolver } from "@hookform/resolvers/zod";
import { AnimatePresence } from "framer-motion";
import { Lock, Mail, User } from "lucide-react";
import { SocialButtons } from "@/components/auth/SocialButtons";
import { FormAlert } from "@/components/auth/FormAlert";
import { PasswordMatch, PasswordStrength } from "@/components/auth/PasswordStrength";
import { EmailSuggestion } from "@/components/auth/EmailSuggestion";
import { Button } from "@/components/ui/Button";
import { Input } from "@/components/ui/Input";
import { Field } from "@/components/ui/Field";
import { Checkbox } from "@/components/ui/Checkbox";
import { registerSchema, type RegisterValues } from "@/lib/validation";
import { auth } from "@/lib/auth";
import { OFFLINE, friendlyAuthError, suggestEmail } from "@/lib/authHelp";
import { useToast } from "@/lib/toast";
import { cn } from "@/lib/utils";

const COUNTRIES = [
  "United States", "United Kingdom", "Canada", "Australia", "Germany", "France",
  "Netherlands", "Singapore", "United Arab Emirates", "India", "Japan", "Brazil", "Other",
];

/** Create-account form. Rendered inside AuthSplit, which owns the page frame. */
export default function Register({ email = "", onEmailChange }: { email?: string; onEmailChange?: (email: string) => void }) {
  const { toast } = useToast();
  const navigate = useNavigate();
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<{ text: string; existing: boolean } | null>(null);
  const [emailTouched, setEmailTouched] = useState(false);
  const {
    register,
    handleSubmit,
    watch,
    setValue,
    formState: { errors },
  } = useForm<RegisterValues>({
    resolver: zodResolver(registerSchema), mode: "onBlur", reValidateMode: "onChange", defaultValues: { email },
  });

  const pw = watch("password") ?? "";
  const confirm = watch("confirmPassword") ?? "";
  const emailValue = watch("email") ?? "";
  const suggestion = emailTouched && !errors.email ? suggestEmail(emailValue) : null;
  useEffect(() => { onEmailChange?.(emailValue); }, [emailValue, onEmailChange]);

  const onSubmit = async (values: RegisterValues) => {
    setSubmitting(true);
    setError(null);
    let res;
    try {
      res = await auth.signUp(values);
    } catch {
      res = { ok: false, message: OFFLINE };
    }
    setSubmitting(false);
    if (!res.ok) {
      const text = friendlyAuthError(res.message);
      setError({ text, existing: text.startsWith("An account with this email already exists") });
      return;
    }
    toast(res.message, "success");
    navigate("/auth/verify-email", { state: { email: values.email } });
  };

  return (
    <>
      <h1 className="text-2xl font-bold tracking-tight text-white">Create your account</h1>
      <p className="mt-1.5 text-sm text-white/50">
        Your workspace starts on a paper account. No card required.
      </p>
      {!auth.configured && (
        <FormAlert tone="error" className="mt-4">Registration is not configured on this deployment.</FormAlert>
      )}

      <form onSubmit={handleSubmit(onSubmit)} className="mt-7" noValidate aria-busy={submitting}>
        <fieldset disabled={submitting} className="space-y-4">
          <div className="grid grid-cols-1 gap-4 min-[420px]:grid-cols-2 min-[420px]:gap-3">
            <Field label="First name" htmlFor="firstName" error={errors.firstName?.message}>
              <Input
                id="firstName"
                autoComplete="given-name"
                placeholder="Alex"
                icon={<User className="h-4 w-4" />}
                invalid={!!errors.firstName}
                {...register("firstName")}
              />
            </Field>
            <Field label="Last name" htmlFor="lastName" error={errors.lastName?.message}>
              <Input
                id="lastName"
                autoComplete="family-name"
                placeholder="Morgan"
                invalid={!!errors.lastName}
                {...register("lastName")}
              />
            </Field>
          </div>

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
            description={<PasswordStrength password={pw} />}
          >
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

          <Field
            label="Confirm password"
            htmlFor="confirmPassword"
            error={errors.confirmPassword?.message}
            description={errors.confirmPassword ? undefined : <PasswordMatch password={pw} confirm={confirm} />}
          >
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

          <Field label="Country" htmlFor="country" error={errors.country?.message}>
            <select
              id="country"
              defaultValue=""
              autoComplete="country-name"
              aria-invalid={!!errors.country || undefined}
              aria-describedby={errors.country ? "country-error" : undefined}
              className={cn(
                "h-11 w-full rounded-xl border bg-ink-700/60 px-3.5 text-sm text-white outline-none",
                "transition-[border-color,background-color,box-shadow] duration-200",
                "focus:border-gold/50 focus:bg-ink-700/90 focus:ring-4 focus:ring-gold/10",
                "disabled:cursor-not-allowed disabled:opacity-60",
                errors.country ? "border-loss/60" : "border-line hover:border-line-strong",
              )}
              {...register("country")}
            >
              <option value="" disabled className="bg-ink-700">
                Select your country
              </option>
              {COUNTRIES.map((c) => (
                <option key={c} value={c} className="bg-ink-700">
                  {c}
                </option>
              ))}
            </select>
          </Field>

          <div>
            <Checkbox
              id="acceptTerms"
              aria-invalid={!!errors.acceptTerms || undefined}
              aria-describedby={errors.acceptTerms ? "acceptTerms-error" : undefined}
              label="I agree to the Terms of Service and Privacy Policy"
              {...register("acceptTerms")}
            />
            <p className="mt-1.5 pl-[28px] text-xs text-white/40">
              Read the{" "}
              <a href="/terms" target="_blank" rel="noopener" className="rounded text-gold/80 underline-offset-2 hover:text-gold hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-gold/60">Terms</a>
              {" "}and{" "}
              <a href="/privacy" target="_blank" rel="noopener" className="rounded text-gold/80 underline-offset-2 hover:text-gold hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-gold/60">Privacy Policy</a>
              {" "}(opens in a new tab).
            </p>
            <div aria-live="polite">
              {errors.acceptTerms && (
                <p id="acceptTerms-error" className="mt-1.5 text-xs text-loss">{errors.acceptTerms.message}</p>
              )}
            </div>
          </div>

          <AnimatePresence initial={false}>
            {error && (
              <FormAlert key="error" tone="error">
                {error.text}
                {error.existing ? (
                  <>
                    {" "}
                    <Link to="/auth/login" className="rounded font-semibold text-gold underline underline-offset-2 hover:text-gold-soft focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-gold/60">
                      Go to sign in
                    </Link>
                  </>
                ) : null}
              </FormAlert>
            )}
          </AnimatePresence>

          <Button type="submit" fullWidth size="lg" loading={submitting} disabled={!auth.configured}>
            {submitting ? "Creating account…" : "Create account"}
          </Button>
        </fieldset>
      </form>

      <SocialButtons divider="or sign up with" />

      <p className="mt-8 text-center text-sm text-white/50">
        Already have an account?{" "}
        <Link to="/auth/login" className="rounded font-medium text-gold transition-colors hover:text-gold-soft focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-gold/60">
          Sign in
        </Link>
      </p>
    </>
  );
}
