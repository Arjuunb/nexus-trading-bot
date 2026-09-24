import { motion, useReducedMotion, useScroll, useSpring } from "framer-motion";

/** A gold hairline across the top of the page that fills as you read. */
export function ScrollProgress() {
  const reduced = useReducedMotion();
  const { scrollYProgress } = useScroll();
  const eased = useSpring(scrollYProgress, { stiffness: 140, damping: 26, restDelta: 0.001 });
  return (
    <motion.div
      aria-hidden
      className="pointer-events-none fixed inset-x-0 top-0 z-[70] h-[2px] origin-left bg-gold-sheen shadow-[0_0_12px_rgba(200,169,75,0.55)]"
      style={{ scaleX: reduced ? scrollYProgress : eased }}
    />
  );
}
