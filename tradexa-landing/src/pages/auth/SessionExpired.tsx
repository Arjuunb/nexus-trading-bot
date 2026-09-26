import { Link } from "react-router-dom";
import { motion } from "framer-motion";
import { Clock, LogIn } from "lucide-react";
import { AuthShell } from "@/components/auth/AuthShell";
import { Card } from "@/components/ui/Card";
import { Button } from "@/components/ui/Button";
import { Logo } from "@/components/Logo";

export default function SessionExpired() {
  return (
    <AuthShell>
      <Card className="p-8 text-center">
        <div className="mb-6 flex justify-center">
          <Logo />
        </div>

        <motion.div
          initial={{ scale: 0, rotate: 12 }}
          animate={{ scale: 1, rotate: 0 }}
          transition={{ type: "spring", stiffness: 200, damping: 15 }}
          className="mx-auto mb-6 flex h-16 w-16 items-center justify-center rounded-2xl border border-loss/25 bg-loss/[0.08] text-loss"
        >
          <Clock className="h-7 w-7" />
        </motion.div>

        <h1 className="text-xl font-bold text-white">Your session expired</h1>
        <p className="mt-2 text-sm leading-relaxed text-white/55">
          For your security, you&apos;ve been signed out after a period of inactivity. Sign in again
          to return to your workspace.
        </p>

        <Link to="/auth/login" className="mt-7 block">
          <Button fullWidth size="lg">
            <LogIn className="h-4 w-4" />
            Sign in again
          </Button>
        </Link>

        <Link
          to="/"
          className="mt-4 inline-block text-sm text-white/50 transition hover:text-white"
        >
          Return to homepage
        </Link>
      </Card>
    </AuthShell>
  );
}
