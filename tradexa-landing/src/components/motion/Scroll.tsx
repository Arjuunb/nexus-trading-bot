import { useEffect, useRef, useState, type ReactNode } from "react";
import {
  motion,
  useMotionValueEvent,
  useReducedMotion,
  useScroll,
  useSpring,
  useTransform,
  type MotionValue,
} from "framer-motion";

// Scroll-linked motion for the landing page. Everything here follows the
// scroll position (no timers), eases through a spring so a flick of the wheel
// does not jolt, and switches off entirely under prefers-reduced-motion: the
// element renders in its final state and nothing moves.

type Offset = NonNullable<Parameters<typeof useScroll>[0]>["offset"];

/** 0 → 1 as `ref` travels through the viewport between the two offsets. */
export function useSectionProgress(offset: Offset = ["start 80%", "end 40%"]) {
  const ref = useRef<HTMLDivElement | null>(null);
  const { scrollYProgress } = useScroll({ target: ref, offset });
  const smooth = useSpring(scrollYProgress, { stiffness: 140, damping: 26, mass: 0.4 });
  return { ref, progress: smooth };
}

/** Which of `count` steps the scroll has reached (0-based), for step-by-step
 *  storytelling. Under reduced motion every step counts as reached. */
export function useActiveStep(progress: MotionValue<number>, count: number) {
  const reduced = useReducedMotion();
  const toStep = (p: number) => (p <= 0 ? -1 : Math.min(count - 1, Math.floor(p * count)));
  const [active, setActive] = useState(() => toStep(progress.get()));
  // A section already on screen when it mounts (a deep link, a refresh
  // mid-page) has to show its state before the first scroll event.
  useEffect(() => { setActive(toStep(progress.get())); }, []);  // eslint-disable-line react-hooks/exhaustive-deps
  useMotionValueEvent(progress, "change", (p) => {
    const next = toStep(p);
    setActive((current) => (current === next ? current : next));
  });
  return reduced ? count - 1 : active;
}

/** Drifts its content by ±`distance` px as it crosses the viewport, so large
 *  panels settle at a different pace from the text around them. */
export function Parallax({ children, distance = 28, className }: {
  children: ReactNode; distance?: number; className?: string;
}) {
  const reduced = useReducedMotion();
  const ref = useRef<HTMLDivElement | null>(null);
  const { scrollYProgress } = useScroll({ target: ref, offset: ["start end", "end start"] });
  const y = useTransform(useSpring(scrollYProgress, { stiffness: 120, damping: 24 }), [0, 1], [distance, -distance]);
  return (
    <motion.div ref={ref} className={className} style={reduced ? undefined : { y }}>
      {children}
    </motion.div>
  );
}

/** Grows from slightly smaller and fainter to full size as it scrolls in,
 *  finishing once its top is 60% up the viewport. */
export function ScrollScale({ children, className, from = 0.94 }: {
  children: ReactNode; className?: string; from?: number;
}) {
  const reduced = useReducedMotion();
  const ref = useRef<HTMLDivElement | null>(null);
  const { scrollYProgress } = useScroll({ target: ref, offset: ["start end", "start 60%"] });
  const p = useSpring(scrollYProgress, { stiffness: 150, damping: 28 });
  const scale = useTransform(p, [0, 1], [from, 1]);
  const opacity = useTransform(p, [0, 1], [0.35, 1]);
  return (
    <motion.div ref={ref} className={className} style={reduced ? undefined : { scale, opacity }}>
      {children}
    </motion.div>
  );
}
