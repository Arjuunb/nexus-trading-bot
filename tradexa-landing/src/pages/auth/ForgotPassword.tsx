import { useState } from "react";
import { Link } from "react-router-dom";
import { useForm } from "react-hook-form";
import { zodResolver } from "@hookform/resolvers/zod";
import { Mail, ArrowLeft, MailCheck } from "lucide-react";
import { motion } from "framer-motion";
import { AuthShell } from "@/components/auth/AuthShell";
import { Card } from "@/components/ui/Card";
import { Button } from "@/components/ui/Button";
import { Input } from "@/components/ui/Input";
import { Field } from "@/components/ui/Field";
import { Logo } from "@/components/Logo";
import { forgotSchema, type ForgotValues } from "@/lib/validation";
import { auth } from "@/lib/auth";
import { useToast } from "@/lib/toast";

export default function ForgotPassword() {
  const { toast } = useToast();
  const [sent, setSent] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const {
    register,
    handleSubmit,
    formState: { errors },
  } = useForm<ForgotValues>({ resolver: zodResolver(forgotSchema), mode: "onBlur" });

  const onSubmit = async (values: ForgotValues) => {
    setSubmitting(true);
    const res = await auth.forgotPassword(values.email);
    setSubmitting(false);
    if (!res.ok) return toast(res.message, "error");
    toast(res.message, res.demo ? "info" : "success");
    setSent(values.email);
  };

  return (
    <AuthShell>
      <Card className="p-8">
        <div className="mb-6 flex justify-center">
          <Logo />
        </div>

        {sent ? (
          <motion.div
            initial={{ opacity: 0, scale: 0.96 }}
            animate={{ opacity: 1, scale: 1 }}
            className="text-center"
          >
            <div className="mx-auto mb-4 flex h-14 w-14 items-center justify-center rounded-full border border-emerald/30 bg-emerald/10 text-emerald">
              <MailCheck className="h-6 w-6" />
            </div>
            <h1 className="text-xl font-bold text-white">Check your email</h1>
            <p className="mt-2 text-sm text-white/55">
              If an account exists for <span className="text-white">{sent}</span>, a reset link is on
              its way.
            </p>
            <Button variant="secondary" fullWidth className="mt-6" onClick={() => setSent(null)}>
              Use a different email
            </Button>
          </motion.div>
        ) : (
          <>
            <h1 className="text-center text-xl font-bold text-white">Forgot your password?</h1>
            <p className="mt-2 text-center text-sm text-white/55">
              Enter your email and we&apos;ll send you a link to reset it.
            </p>
            <form onSubmit={handleSubmit(onSubmit)} className="mt-6 space-y-4" noValidate>
              <Field label="Email" htmlFor="email" error={errors.email?.message}>
                <Input
                  id="email"
                  type="email"
                  autoComplete="email"
                  placeholder="you@company.com"
                  icon={<Mail className="h-4 w-4" />}
                  invalid={!!errors.email}
                  {...register("email")}
                />
              </Field>
              <Button type="submit" fullWidth size="lg" loading={submitting}>
                Send reset link
              </Button>
            </form>
          </>
        )}

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
