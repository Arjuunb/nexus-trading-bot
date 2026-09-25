import { useRef } from "react";
import { GridTexture } from "@/components/site/backdrops";
import {
  motion,
  useMotionValue,
  useReducedMotion,
  useScroll,
  useSpring,
  useTransform,
} from "framer-motion";
import { ArrowRight, BookOpen, CheckCircle2, ShieldCheck } from "lucide-react";
import { Link } from "react-router-dom";
import { Button } from "@/components/ui/Button";
import { WordReveal } from "@/components/Reveal";
import { Magnetic } from "@/components/motion/Magnetic";
import { DashboardPreview } from "./DashboardPreview";
import { APP_URL } from "@/lib/utils";

const container = {
  hidden: {},
  show: { transition: { staggerChildren: 0.08, delayChildren: 0.1 } },
};
const item = {
  hidden: { opacity: 0, y: 18 },
  show: { opacity: 1, y: 0, transition: { duration: 0.6, ease: [0.22, 1, 0.36, 1] } },
};

// Statements the product actually keeps; nothing here is a result or a figure.
const ASSURANCES = ["Paper trading first", "Risk gates on every order", "Every decision journaled"];

export function Hero() {
  const reduced = useReducedMotion() ?? false;
  const ref = useRef<HTMLElement | null>(null);

  // scroll-driven depth: as the hero scrolls away, layers travel at slightly
  // different rates. Nothing fades out — a hero that dissolves while it is
  // still on screen leaves an empty band above the next section.
  const { scrollYProgress } = useScroll({ target: ref, offset: ["start start", "end start"] });
  const copyY = useTransform(scrollYProgress, [0, 1], [0, -24]);
  const previewY = useTransform(scrollYProgress, [0, 1], [0, -56]);
  const ambientY = useTransform(scrollYProgress, [0, 1], [0, 90]);

  // pointer-driven 3D tilt on the preview (subtle, spring-smoothed).
  const px = useMotionValue(0); // -0.5 .. 0.5
  const py = useMotionValue(0);
  const rotX = useSpring(useTransform(py, [-0.5, 0.5], [6, -6]), { stiffness: 120, damping: 18 });
  const rotY = useSpring(useTransform(px, [-0.5, 0.5], [-7, 7]), { stiffness: 120, damping: 18 });

  const onMove = (e: React.MouseEvent<HTMLDivElement>) => {
    if (reduced) return;
    const r = e.currentTarget.getBoundingClientRect();
    px.set((e.clientX - r.left) / r.width - 0.5);
    py.set((e.clientY - r.top) / r.height - 0.5);
  };
  const onLeave = () => { px.set(0); py.set(0); };

  // A soft light under the pointer, written straight to CSS variables so it
  // follows the mouse without re-rendering the hero.
  const onHeroMove = (e: React.MouseEvent<HTMLElement>) => {
    if (reduced) return;
    const r = e.currentTarget.getBoundingClientRect();
    e.currentTarget.style.setProperty("--hx", `${e.clientX - r.left}px`);
    e.currentTarget.style.setProperty("--hy", `${e.clientY - r.top}px`);
  };

  return (
    <section ref={ref} onMouseMove={onHeroMove} className="group/hero relative pt-32 sm:pt-40">
      {/* the hero is where the grid earns its keep */}
      <GridTexture />
      {!reduced && (
        <div
          aria-hidden
          className="pointer-events-none absolute inset-0 -z-10 opacity-0 transition-opacity duration-700 group-hover/hero:opacity-100"
          style={{ background: "radial-gradient(520px circle at var(--hx, 50%) var(--hy, 30%), rgba(200,169,75,0.075), transparent 60%)" }}
        />
      )}
      {/* hero-local ambient bloom — parallaxes independently of the page backdrop */}
      {!reduced && (
        <motion.div aria-hidden style={{ y: ambientY }} className="pointer-events-none absolute inset-0 -z-10 overflow-hidden">
          <div className="absolute -top-24 right-1/4 h-[26rem] w-[34rem] rounded-full bg-gold/[0.06] blur-[120px]" />
          <div className="absolute top-1/3 left-[-6rem] h-[22rem] w-[28rem] rounded-full bg-emerald-deep/[0.05] blur-[130px]" />
        </motion.div>
      )}

      <div className="container-x grid items-center gap-16 lg:grid-cols-[1.05fr_1fr]">
        {/* left copy */}
        <motion.div
          variants={container}
          initial="hidden"
          animate="show"
          style={reduced ? undefined : { y: copyY }}
        >
          <motion.div variants={item}>
            <span className="eyebrow">
              <ShieldCheck className="h-3.5 w-3.5" />
              AI Trading Intelligence System
            </span>
          </motion.div>

          {/* The headline sets itself word by word; the gold line carries light
              moving slowly through it. */}
          <h1 className="mt-6 text-balance text-5xl font-extrabold leading-[1.03] tracking-tight text-white sm:text-6xl lg:text-[4.25rem]">
            <WordReveal trigger="mount" text="AI-Powered Trading" delay={0.15} stagger={0.08} />
            <br />
            <WordReveal trigger="mount" text="Intelligence System" delay={0.35} stagger={0.08}
                        wordClassName="text-gold-shimmer" />
          </h1>

          <motion.p variants={item} className="mt-6 max-w-xl text-lg leading-relaxed text-white/60">
            Analyze markets, test strategies, and automate intelligent trading decisions through a
            powerful AI-driven platform built for modern traders.
          </motion.p>

          <motion.div variants={item} className="mt-9 flex flex-col gap-3 sm:flex-row sm:items-center">
            <Magnetic>
              <a href={APP_URL} className="w-full sm:w-auto">
                <Button size="lg" className="group w-full sm:w-auto">
                  Launch Platform
                  <ArrowRight className="h-4 w-4 transition-transform duration-300 group-hover:translate-x-1" />
                </Button>
              </a>
            </Magnetic>
            <Link to="/docs" className="w-full sm:w-auto">
              <Button size="lg" variant="secondary" className="w-full sm:w-auto">
                <BookOpen className="h-4 w-4" />
                View Documentation
              </Button>
            </Link>
          </motion.div>

          <motion.ul variants={item} className="mt-10 flex flex-wrap items-center gap-x-5 gap-y-2.5">
            {ASSURANCES.map((text, i) => (
              <motion.li
                key={text}
                initial={{ opacity: 0, y: reduced ? 0 : 6 }}
                animate={{ opacity: 1, y: 0 }}
                transition={{ delay: 0.9 + i * 0.1, duration: 0.5, ease: [0.22, 1, 0.36, 1] }}
                className="flex items-center gap-1.5 text-[13px] text-white/55"
              >
                <CheckCircle2 className="h-3.5 w-3.5 text-emerald-soft/80" />
                {text}
              </motion.li>
            ))}
          </motion.ul>
        </motion.div>

        {/* right preview — parallax depth + pointer tilt */}
        <motion.div
          style={reduced ? undefined : { y: previewY, perspective: 1200 }}
          onMouseMove={onMove}
          onMouseLeave={onLeave}
        >
          {reduced ? (
            <DashboardPreview />
          ) : (
            <motion.div style={{ rotateX: rotX, rotateY: rotY, transformStyle: "preserve-3d" }}>
              <DashboardPreview />
            </motion.div>
          )}
        </motion.div>
      </div>

    </section>
  );
}
