import { z } from "zod";

/** Shared password policy — surfaced to users as they type. */
export const passwordSchema = z
  .string()
  .min(8, "At least 8 characters")
  .regex(/[A-Z]/, "One uppercase letter")
  .regex(/[a-z]/, "One lowercase letter")
  .regex(/[0-9]/, "One number");

export const loginSchema = z.object({
  email: z.string().min(1, "Email is required").email("Enter a valid email"),
  password: z.string().min(1, "Password is required"),
  remember: z.boolean().optional().default(false),
});
export type LoginValues = z.infer<typeof loginSchema>;

export const registerSchema = z
  .object({
    firstName: z.string().min(1, "First name is required"),
    lastName: z.string().min(1, "Last name is required"),
    email: z.string().min(1, "Email is required").email("Enter a valid email"),
    password: passwordSchema,
    confirmPassword: z.string().min(1, "Confirm your password"),
    country: z.string().min(1, "Select your country"),
    acceptTerms: z.literal(true, {
      errorMap: () => ({ message: "You must accept the terms" }),
    }),
  })
  .refine((v) => v.password === v.confirmPassword, {
    message: "Passwords do not match",
    path: ["confirmPassword"],
  });
export type RegisterValues = z.infer<typeof registerSchema>;

export const forgotSchema = z.object({
  email: z.string().min(1, "Email is required").email("Enter a valid email"),
});
export type ForgotValues = z.infer<typeof forgotSchema>;

export const resetSchema = z
  .object({
    password: passwordSchema,
    confirmPassword: z.string().min(1, "Confirm your password"),
  })
  .refine((v) => v.password === v.confirmPassword, {
    message: "Passwords do not match",
    path: ["confirmPassword"],
  });
export type ResetValues = z.infer<typeof resetSchema>;

/**
 * The rules the sign-up checklist shows. The first three are exactly what
 * `passwordSchema` enforces; the last is advice, never a requirement.
 */
export const PASSWORD_RULES: { key: string; label: string; required: boolean; test: (pw: string) => boolean }[] = [
  { key: "length", label: "At least 8 characters", required: true, test: (pw) => pw.length >= 8 },
  { key: "case", label: "Upper- and lower-case letters", required: true, test: (pw) => /[A-Z]/.test(pw) && /[a-z]/.test(pw) },
  { key: "number", label: "At least one number", required: true, test: (pw) => /[0-9]/.test(pw) },
  { key: "long", label: "Recommended: 12+ characters with a symbol", required: false,
    test: (pw) => pw.length >= 12 && /[^A-Za-z0-9]/.test(pw) },
];

/** Live password-strength meter (0–4) from the same rules Zod enforces. */
export function passwordStrength(pw: string): { score: number; label: string } {
  const score = PASSWORD_RULES.filter((rule) => rule.test(pw)).length;
  const label = ["Too short", "Weak", "Fair", "Strong", "Excellent"][score] ?? "Weak";
  return { score, label };
}
