import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { useForm } from "react-hook-form";
import { zodResolver } from "@hookform/resolvers/zod";
import { Lock } from "lucide-react";
import { AuthShell } from "@/components/auth/AuthShell";
import { Card } from "@/components/ui/Card";
import { Button } from "@/components/ui/Button";
import { Input } from "@/components/ui/Input";
import { Field } from "@/components/ui/Field";
import { Logo } from "@/components/Logo";
import { resetSchema, type ResetValues } from "@/lib/validation";
import { PasswordStrength } from "@/components/auth/PasswordStrength";
import { auth } from "@/lib/auth";
import { useToast } from "@/lib/toast";

export default function ResetPassword() {
  const { toast } = useToast();
  const navigate = useNavigate();
  const [submitting, setSubmitting] = useState(false);
  const {
    register,
    handleSubmit,
    watch,
    formState: { errors },
  } = useForm<ResetValues>({ resolver: zodResolver(resetSchema), mode: "onBlur" });

  const pw = watch("password") ?? "";

  const onSubmit = async (values: ResetValues) => {
    setSubmitting(true);
    const res = await auth.updatePassword(values.password);
    setSubmitting(false);
    if (!res.ok) return toast(res.message, "error");
    toast(res.message, "success");
    navigate("/auth/login");
  };

  return (
    <AuthShell>
      <Card className="p-8">
        <div className="mb-6 flex justify-center">
          <Logo />
        </div>
        <h1 className="text-center text-xl font-bold text-white">Set a new password</h1>
        <p className="mt-2 text-center text-sm text-white/55">
          Choose a strong password you don&apos;t use anywhere else.
        </p>

        <form onSubmit={handleSubmit(onSubmit)} className="mt-6 space-y-4" noValidate>
          <Field label="New password" htmlFor="password" error={errors.password?.message}>
            <Input
              id="password"
              type="password"
              autoComplete="new-password"
              placeholder="Create a strong password"
              icon={<Lock className="h-4 w-4" />}
              invalid={!!errors.password}
              {...register("password")}
            />
            <PasswordStrength password={pw} />
          </Field>

          <Field label="Confirm password" htmlFor="confirmPassword" error={errors.confirmPassword?.message}>
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

          <Button type="submit" fullWidth size="lg" loading={submitting}>
            Update password
          </Button>
        </form>
      </Card>
    </AuthShell>
  );
}
