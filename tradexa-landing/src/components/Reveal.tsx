import { motion, useReducedMotion } from "framer-motion";
import { Children, Fragment, type ReactNode } from "react";
import { Link } from "react-router-dom";
import { cn } from "@/lib/utils";

// One easing curve for the whole site. A gentle overshoot-free ease-out: motion
// decelerates into place rather than stopping dead, which is what makes a
// reveal read as settling instead of snapping.
const EASE = [0.22, 1, 0.36, 1] as const;
const DURATION = 0.55;
// Reveal a little BEFORE the element reaches the viewport edge, so the motion
// finishes as it becomes comfortably readable rather than starting there.
const VIEWPORT = { once: true, margin: "-80px" } as const;

interface RevealProps {
  children: ReactNode;
  delay?: number;
  className?: string;
  y?: number;
}

/** Scroll-triggered fade-up. Reusable across every landing section. */
export function Reveal({ children, delay = 0, className, y = 24 }: RevealProps) {
  // MotionConfig at the app root already handles this globally; honouring it
  // here too keeps the travel distance at 0 rather than merely un-animated,
  // so no layout shift is baked into the initial state.
  const reduced = useReducedMotion();
  return (
    <motion.div
      className={className}
      initial={{ opacity: 0, y: reduced ? 0 : y }}
      whileInView={{ opacity: 1, y: 0 }}
      viewport={VIEWPORT}
      transition={{ duration: DURATION, delay, ease: EASE }}
    >
      {children}
    </motion.div>
  );
}

interface RevealGroupProps {
  children: ReactNode;
  className?: string;
  /** Seconds between each child. Kept small — past ~0.1s a grid of cards
   *  stops reading as one group arriving and starts feeling like a queue. */
  stagger?: number;
  delay?: number;
  y?: number;
}

/**
 * Reveals children in sequence rather than all at once.
 *
 * A grid of six feature cards fading in on the same frame reads as a single
 * flat block; the same six offset by 60ms each read as related items settling
 * into place, and the eye is led left-to-right instead of having to pick an
 * entry point. The stagger is orchestrated by the parent's variants so the
 * children need no per-item delay arithmetic.
 */
export function RevealGroup({ children, className, stagger = 0.06, delay = 0,
                              y = 20 }: RevealGroupProps) {
  const reduced = useReducedMotion();
  return (
    <motion.div
      className={className}
      initial="hidden"
      whileInView="shown"
      viewport={VIEWPORT}
      variants={{
        hidden: {},
        // Staggering is motion, so collapse it under the reduced-motion
        // preference too — otherwise the page still ripples, just without
        // the travel.
        shown: { transition: { staggerChildren: reduced ? 0 : stagger, delayChildren: delay } },
      }}
    >
      {Children.map(children, (child, i) => (
        <motion.div
          key={i}
          variants={{
            hidden: { opacity: 0, y: reduced ? 0 : y },
            shown: { opacity: 1, y: 0, transition: { duration: DURATION, ease: EASE } },
          }}
        >
          {child}
        </motion.div>
      ))}
    </motion.div>
  );
}

interface WordRevealProps {
  text: string;
  className?: string;
  /** Extra classes for each word (e.g. a gradient that must sit on the glyphs). */
  wordClassName?: string;
  delay?: number;
  stagger?: number;
  /** "view": when scrolled into view (sections). "mount": immediately (hero). */
  trigger?: "view" | "mount";
}

/**
 * Words rising out of a mask, one after another.
 *
 * Each word sits in its own overflow-hidden box and travels up from just
 * below it, so the line appears to be set in place rather than faded in. The
 * boxes are inline-block, so wrapping, text-balance and the heading's own
 * line-height all behave exactly as they would for plain text, and assistive
 * technology reads the words as the one sentence they are -- they are real
 * text with real spaces between them, nothing is hidden or relabelled.
 */
export function WordReveal({ text, className, wordClassName, delay = 0, stagger = 0.06,
                             trigger = "view" }: WordRevealProps) {
  const reduced = useReducedMotion();
  const words = text.split(" ");
  const play = trigger === "mount" ? { animate: "shown" } : { whileInView: "shown", viewport: VIEWPORT };
  return (
    <motion.span
      className={className}
      initial="hidden"
      {...play}
      variants={{ hidden: {}, shown: { transition: { staggerChildren: reduced ? 0 : stagger, delayChildren: delay } } }}
    >
      {words.map((word, i) => (
        // The space sits between the boxes: inside an inline-block a trailing
        // space is trimmed and the words would run together.
        <Fragment key={`${word}-${i}`}>
          <span className="inline-block overflow-hidden pb-[0.12em] -mb-[0.12em] align-bottom">
            <motion.span
              className={cn("inline-block", wordClassName)}
              variants={{
                hidden: reduced ? { opacity: 0 } : { y: "105%", opacity: 0, filter: "blur(6px)" },
                shown: { y: "0%", opacity: 1, filter: "blur(0px)", transition: { duration: 0.7, ease: EASE } },
              }}
            >
              {word}
            </motion.span>
          </span>
          {i < words.length - 1 ? " " : null}
        </Fragment>
      ))}
    </motion.span>
  );
}

interface SectionHeadingProps {
  eyebrow: string;
  title: ReactNode;
  subtitle?: string;
  className?: string;
  /**
   * Where this section's heading leads.
   *
   * A "#anchor" makes the heading a deep link to itself, with a gold "#" on
   * hover. A route path ("/engine") instead sends the reader to that
   * section's dedicated page, and the affordance becomes an arrow — because
   * the two do genuinely different things and a single glyph for both would
   * be a lie about one of them.
   */
  link?: string;
}

/** The eyebrow label, flanked by two hairlines that draw outward on reveal. */
function Eyebrow({ children, hover }: { children: ReactNode; hover?: boolean }) {
  const reduced = useReducedMotion();
  const line = (side: "left" | "right") => (
    <motion.span
      aria-hidden
      className={cn("h-px w-6 sm:w-8",
        side === "left" ? "origin-right bg-gradient-to-r from-transparent to-gold/60"
                        : "origin-left bg-gradient-to-l from-transparent to-gold/60")}
      initial={{ scaleX: reduced ? 1 : 0 }}
      whileInView={{ scaleX: 1 }}
      viewport={VIEWPORT}
      transition={{ duration: 0.8, delay: 0.15, ease: EASE }}
    />
  );
  return (
    <span className={cn("eyebrow", hover && "transition-colors group-hover:text-gold")}>
      {line("left")}
      {children}
      {line("right")}
    </span>
  );
}

/** Consistent centered section header. */
export function SectionHeading({ eyebrow, title, subtitle, className, link }: SectionHeadingProps) {
  const isRoute = !!link && link.startsWith("/");
  const heading = (
    <h2 className="mt-4 text-balance text-3xl font-bold tracking-tight text-white sm:text-4xl">
      {typeof title === "string" ? <WordReveal text={title} delay={0.05} /> : title}
    </h2>
  );
  const inner = (
    <>
      <Eyebrow hover>{eyebrow}</Eyebrow>
      <span className="relative block">
        {heading}
        <span
          aria-hidden
          className="absolute -right-7 top-1/2 hidden -translate-y-1/4 text-2xl font-bold text-gold/0 transition-all duration-300 group-hover:text-gold/60 sm:inline"
        >
          {isRoute ? "→" : "#"}
        </span>
      </span>
    </>
  );

  return (
    <Reveal className={className} y={14}>
      <div className="mx-auto max-w-2xl text-center">
        {link ? (
          isRoute ? (
            <Link to={link} className="group inline-block no-underline" aria-label={`Read more about ${eyebrow}`}>
              {inner}
            </Link>
          ) : (
            <a href={link} className="group inline-block no-underline" aria-label="Link to this section">
              {inner}
            </a>
          )
        ) : (
          <>
            <Eyebrow>{eyebrow}</Eyebrow>
            {heading}
          </>
        )}
        {subtitle && <p className="mt-4 text-white/55">{subtitle}</p>}
      </div>
    </Reveal>
  );
}
