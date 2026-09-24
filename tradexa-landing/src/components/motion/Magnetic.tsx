import { motion, useMotionValue, useReducedMotion, useSpring } from "framer-motion";
import { useEffect, useState, type ReactNode } from "react";

/**
 * Leans its child toward the pointer while the pointer is over it, then
 * springs back. Only for a fine pointer (a mouse): on touch there is no hover
 * to respond to, and a button that moves under a thumb is a mis-tap.
 */
export function Magnetic({ children, strength = 0.28 }: { children: ReactNode; strength?: number }) {
  const reduced = useReducedMotion();
  const [fine, setFine] = useState(false);
  const x = useSpring(useMotionValue(0), { stiffness: 220, damping: 18, mass: 0.4 });
  const y = useSpring(useMotionValue(0), { stiffness: 220, damping: 18, mass: 0.4 });

  useEffect(() => {
    const query = window.matchMedia?.("(pointer: fine)");
    setFine(Boolean(query?.matches));
  }, []);

  if (reduced || !fine) return <>{children}</>;
  return (
    <motion.div
      className="inline-flex"
      style={{ x, y }}
      onMouseMove={(event) => {
        const box = event.currentTarget.getBoundingClientRect();
        x.set((event.clientX - (box.left + box.width / 2)) * strength);
        y.set((event.clientY - (box.top + box.height / 2)) * strength * 1.4);
      }}
      onMouseLeave={() => { x.set(0); y.set(0); }}
    >
      {children}
    </motion.div>
  );
}
