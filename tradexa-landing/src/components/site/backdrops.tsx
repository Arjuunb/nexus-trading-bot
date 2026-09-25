import { motion, useReducedMotion } from "framer-motion";
import { Ambient } from "@/components/site/Ambient";

/**
 * Every page backdrop on the site, side by side.
 *
 * They live in one file for a reason. The site had drifted into using the same
 * 1px grid everywhere — the app rendered it globally behind the landing page,
 * auth and settings, and seven product pages then re-applied a tinted copy of
 * it. The result was that /engine, /security and /live-trade read as the
 * landing page with a different hue rather than as separate products, and the
 * only way to notice was to open them side by side.
 *
 * So they are side by side here. Adding a page means adding a backdrop to this
 * file, where "is this just the grid again?" is a question you cannot avoid
 * answering.
 *
 * Rules that apply to all of them:
 *
 *  - Each renders its own opaque base. There is no global backdrop underneath
 *    any more, so a page that forgets one gets the body's flat ink rather than
 *    silently inheriting somebody else's texture.
 *  - `pointer-events-none` and `-z-10`, handled by `Ambient`.
 *  - Motion is gated on the reduced-motion preference. A backdrop is the
 *    least important thing on a page and should be the first to go still.
 */

/* ── Landing ──────────────────────────────────────────────────────────── */

/**
 * The landing page's ambient light. Deliberately *without* the grid — the grid
 * is now a texture the hero and a couple of sections opt into, rather than a
 * layer the whole application sits on.
 */
export function LandingAmbient() {
  return (
    <Ambient base="bg-ink bg-page-depth">
      <div className="absolute -top-48 left-1/2 h-[34rem] w-[46rem] -translate-x-1/2 rounded-full bg-gold/[0.05] blur-[130px] motion-safe:animate-bloom" />
      <div className="absolute -bottom-56 right-[-12rem] h-[30rem] w-[40rem] rounded-full bg-gold/[0.05] blur-[150px] motion-safe:animate-bloom-slow" />
      <div className="absolute bottom-1/4 left-[-14rem] h-[24rem] w-[30rem] rounded-full bg-gold-deep/[0.035] blur-[140px]" />
      <div className="absolute inset-0 bg-[radial-gradient(ellipse_at_center,transparent_58%,rgba(0,0,0,0.6))]" />
      {/* fine grain over the whole backdrop: the big soft blooms read as
          light in a room instead of as gradient banding */}
      <div className="grain absolute inset-0 opacity-[0.035] mix-blend-overlay" />
    </Ambient>
  );
}

/**
 * The grid, as a section-scoped texture.
 *
 * `absolute`, not `fixed` — it belongs to the section that renders it and
 * scrolls away with it, which is the whole difference between "this section is
 * technical" and "this entire application is a landing page".
 */
export function GridTexture({ className = "" }: { className?: string }) {
  return (
    <div aria-hidden className={`pointer-events-none absolute inset-0 -z-10 overflow-hidden ${className}`}>
      <div className="absolute inset-0 bg-grid-lines [background-size:28px_28px] opacity-50 mask-fade-b motion-safe:animate-grid-pan" />
      <div className="absolute inset-0 bg-grid-lines [background-size:140px_140px] opacity-30 mask-fade-b" />
    </div>
  );
}

/* ── Application surfaces (auth, settings, 404) ───────────────────────── */

/**
 * The quiet backdrop behind sign-in and settings.
 *
 * These are working surfaces rather than marketing pages, and they used to
 * inherit the landing page's animated grid for no reason other than that it
 * was global. One soft source and nothing else.
 */
export function AppSurface() {
  return (
    <Ambient base="bg-ink bg-page-depth">
      <div className="absolute -top-40 left-1/2 h-[30rem] w-[42rem] -translate-x-1/2 rounded-full bg-gold/[0.04] blur-[140px]" />
      <div className="absolute inset-0 bg-[radial-gradient(ellipse_at_center,transparent_62%,rgba(0,0,0,0.55))]" />
    </Ambient>
  );
}

/* ── /features — dot matrix ───────────────────────────────────────────── */

export function FeaturesBackdrop() {
  return (
    <Ambient base="bg-[#070708]">
      <div className="absolute -left-40 -top-40 h-[38rem] w-[46rem] rounded-full bg-signal/[0.07] blur-[150px]" />
      <div className="absolute right-[-16rem] top-1/3 h-[30rem] w-[38rem] rounded-full bg-gold/[0.035] blur-[160px]" />
      <div
        className="absolute inset-0 opacity-50 mask-fade-b"
        style={{
          backgroundImage: "radial-gradient(rgba(255,255,255,0.075) 1px, transparent 1px)",
          backgroundSize: "22px 22px",
        }}
      />
    </Ambient>
  );
}

/* ── /engine — AI node field ──────────────────────────────────────────── */

/**
 * The mesh, on a jittered grid across a 160×100 viewBox.
 *
 * A jittered grid rather than free random placement: pure randomness clumps,
 * leaving bald patches and knots, and a mesh with either reads as a scribble.
 * One node per cell with an offset inside it keeps the density even while
 * still looking unplanned.
 *
 * Node count matters more than it sounds. An earlier version had twenty-three
 * across the whole viewport, which at render scale meant links two hundred
 * pixels long — three enormous diagonals rather than a field.
 */
const COLS = 11;
const ROWS = 7;
const CELL_W = 160 / COLS;
const CELL_H = 100 / ROWS;
/** Link cells that touch, including diagonally, and nothing further. */
const LINK_DISTANCE = Math.hypot(CELL_W, CELL_H) * 1.05;

function buildMesh() {
  // Deterministic, so the constellation is identical for every visitor.
  let a = 0x9e3779b9;
  const rand = () => {
    a = (a + 0x6d2b79f5) >>> 0;
    let t = Math.imul(a ^ (a >>> 15), 1 | a);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };

  const nodes: [number, number][] = [];
  for (let r = 0; r < ROWS; r++) {
    for (let c = 0; c < COLS; c++) {
      nodes.push([
        (c + 0.15 + rand() * 0.7) * CELL_W,
        (r + 0.15 + rand() * 0.7) * CELL_H,
      ]);
    }
  }

  const edges: [number, number][] = [];
  for (let i = 0; i < nodes.length; i++) {
    for (let j = i + 1; j < nodes.length; j++) {
      const d = Math.hypot(nodes[i][0] - nodes[j][0], nodes[i][1] - nodes[j][1]);
      if (d < LINK_DISTANCE) edges.push([i, j]);
    }
  }
  return { nodes, edges };
}

const MESH = buildMesh();

/**
 * Graphite under a slow neural mesh — nodes that breathe and links that carry
 * an occasional pulse. It is the same claim the page makes in words, made
 * quietly behind them.
 */
export function EngineBackdrop() {
  const reduced = useReducedMotion() ?? false;
  const { nodes, edges } = MESH;

  return (
    <Ambient base="bg-graphite">
      <div className="absolute left-1/2 top-[-18rem] h-[40rem] w-[60rem] -translate-x-1/2 rounded-full bg-electric/[0.08] blur-[170px]" />
      <div className="absolute bottom-[-14rem] left-[-10rem] h-[30rem] w-[36rem] rounded-full bg-aqua/[0.05] blur-[150px]" />

      {/*
        A 16:10 viewBox, not a square one. With `slice` a square viewBox scales
        to cover the wider of the two axes — on a 1280×760 window that is 12.8×,
        which pushed most of the mesh off the bottom of the screen and left
        three faint lines in one corner. Matching the viewBox to a typical
        window keeps the whole field on screen at a sane scale.
      */}
      <svg
        viewBox="0 0 160 100"
        preserveAspectRatio="xMidYMid slice"
        className="absolute inset-0 h-full w-full"
      >
        {edges.map(([a, b], i) => (
          <line
            key={i}
            x1={nodes[a][0]}
            y1={nodes[a][1]}
            x2={nodes[b][0]}
            y2={nodes[b][1]}
            stroke="#EAB54F"
            strokeOpacity="0.2"
            strokeWidth="0.12"
          />
        ))}
        {nodes.map(([x, y], i) => (
          <motion.circle
            key={i}
            cx={x}
            cy={y}
            r="0.34"
            fill="#F2C766"
            initial={{ opacity: 0.35 }}
            animate={reduced ? { opacity: 0.35 } : { opacity: [0.18, 0.62, 0.18] }}
            transition={{ duration: 5 + (i % 6), repeat: Infinity, ease: "easeInOut", delay: (i % 17) * 0.4 }}
          />
        ))}
        {/* three signals travelling the mesh */}
        {!reduced &&
          [0, 1, 2].map((k) => {
            const [a, b] = edges[(k * 37 + 11) % edges.length];
            return (
              <motion.circle
                key={`p${k}`}
                r="0.45"
                fill="#B0B8C4"
                initial={{ cx: nodes[a][0], cy: nodes[a][1], opacity: 0 }}
                animate={{
                  cx: [nodes[a][0], nodes[b][0]],
                  cy: [nodes[a][1], nodes[b][1]],
                  opacity: [0, 0.95, 0],
                }}
                transition={{ duration: 3.2, repeat: Infinity, delay: k * 1.4, ease: "linear" }}
              />
            );
          })}
      </svg>

      {/* The mesh recedes towards the edges rather than being cut off. Kept
          deliberately gentle — at `transparent 35%` this covered everything but
          the dead centre and the field may as well not have been drawn. */}
      <div className="absolute inset-0 bg-[radial-gradient(ellipse_at_50%_45%,transparent_60%,rgba(11,14,18,0.8)_100%)]" />

      <div className="absolute inset-x-0 top-[62vh] h-px bg-gradient-to-r from-transparent via-electric/20 to-transparent" />
    </Ambient>
  );
}

/* ── /live-trade — terminal phosphor ──────────────────────────────────── */

/**
 * A CRT, not a grid. Scanlines, a phosphor wash from the centre, and a heavy
 * vignette so the corners fall away the way they do on a real trading monitor.
 */
export function TerminalBackdrop() {
  const reduced = useReducedMotion() ?? false;
  return (
    <Ambient base="bg-term">
      <div className="absolute left-1/2 top-0 h-[26rem] w-[52rem] -translate-x-1/2 rounded-full bg-gold/[0.05] blur-[160px]" />
      <div className="absolute bottom-0 right-0 h-[24rem] w-[34rem] rounded-full bg-loss/[0.035] blur-[150px]" />
      {/* phosphor scanlines — the only texture, at the pitch a CRT actually had */}
      <div className="absolute inset-0 bg-scanlines opacity-[0.55]" />
      {/* a slow refresh band drifting down the screen */}
      {!reduced && (
        <div className="absolute inset-x-0 top-0 h-40 bg-gradient-to-b from-transparent via-gold/[0.035] to-transparent motion-safe:animate-scan-down" />
      )}
      <div className="absolute inset-0 bg-[radial-gradient(ellipse_at_center,transparent_45%,rgba(0,0,0,0.75))]" />
    </Ambient>
  );
}

/* ── /selectivity — vertical gold rules ───────────────────────────────── */

export function SelectivityBackdrop() {
  return (
    <Ambient base="bg-obsidian">
      <div className="absolute left-1/2 top-[-24rem] h-[46rem] w-[46rem] -translate-x-1/2 rounded-full bg-gold/[0.055] blur-[180px]" />
      <div className="absolute bottom-[-20rem] left-1/2 h-[34rem] w-[54rem] -translate-x-1/2 rounded-full bg-gold-deep/[0.04] blur-[170px]" />
      <div
        className="absolute inset-0 opacity-40 mask-fade-b"
        style={{
          backgroundImage: "linear-gradient(to right, rgba(234,181,79,0.05) 1px, transparent 1px)",
          backgroundSize: "112px 100%",
        }}
      />
    </Ambient>
  );
}

/* ── /how-it-works — stage horizon bands ──────────────────────────────── */

/**
 * Wide horizontal bands rather than a grid, tinted by whichever stage the
 * reader is on. Horizontal because the page is a sequence, and the backdrop
 * should imply travelling along one.
 */
export function JourneyBackdrop({ color }: { color: string }) {
  return (
    <Ambient base="bg-[#060607]">
      <motion.div
        className="absolute left-1/2 top-[-20rem] h-[46rem] w-[62rem] -translate-x-1/2 rounded-full blur-[170px]"
        animate={{ backgroundColor: `${color}14` }}
        transition={{ duration: 1.1, ease: [0.22, 1, 0.36, 1] }}
      />
      <motion.div
        className="absolute bottom-[-16rem] right-[-12rem] h-[32rem] w-[42rem] rounded-full blur-[160px]"
        animate={{ backgroundColor: `${color}0d` }}
        transition={{ duration: 1.1, ease: [0.22, 1, 0.36, 1] }}
      />
      <motion.div
        className="absolute inset-0 opacity-60 mask-fade-b"
        animate={{
          backgroundImage: `repeating-linear-gradient(to bottom, ${color}0f 0px, ${color}0f 1px, transparent 1px, transparent 96px)`,
        }}
        transition={{ duration: 1.1 }}
      />
    </Ambient>
  );
}

/* ── /security — hexagon lattice ──────────────────────────────────────── */

/**
 * A hexagonal lattice: the shape security diagrams have used for cells and
 * boundaries for decades, and — unlike a square grid — one that reads as
 * tessellation rather than as graph paper.
 */
export function SecurityBackdrop() {
  return (
    <Ambient base="bg-navy">
      <div className="absolute right-[-14rem] top-[-16rem] h-[40rem] w-[48rem] rounded-full bg-gold/[0.06] blur-[170px]" />
      <div className="absolute bottom-[-16rem] left-[-12rem] h-[34rem] w-[42rem] rounded-full bg-signal/[0.05] blur-[160px]" />

      <svg className="absolute inset-0 h-full w-full opacity-[0.5] mask-fade-b" aria-hidden>
        <defs>
          <pattern id="nx-hex" width="56" height="97" patternUnits="userSpaceOnUse">
            {/* two offset hexagons tile seamlessly at this ratio */}
            <path
              d="M28 0 L56 16 L56 48 L28 64 L0 48 L0 16 Z M28 64 L56 80 L56 112 M28 64 L0 80 L0 112"
              fill="none"
              stroke="#22C55E"
              strokeOpacity="0.16"
              strokeWidth="1"
            />
          </pattern>
        </defs>
        <rect width="100%" height="100%" fill="url(#nx-hex)" />
      </svg>
    </Ambient>
  );
}

/* ── /performance — carbon fibre ──────────────────────────────────────── */

/**
 * A carbon-fibre twill, built from two opposed 45° gradients offset by half a
 * tile. Dense, matte and directional — it reads as instrument housing, which
 * is the right register for a page of measurements.
 */
export function PerformanceBackdrop() {
  const weave =
    "repeating-linear-gradient(45deg, rgba(255,255,255,0.028) 0 3px, transparent 3px 6px)," +
    "repeating-linear-gradient(-45deg, rgba(0,0,0,0.5) 0 3px, transparent 3px 6px)";

  return (
    <Ambient base="bg-[#070708]">
      <div className="absolute left-1/2 top-[-20rem] h-[38rem] w-[54rem] -translate-x-1/2 rounded-full bg-gold/[0.055] blur-[170px]" />
      <div className="absolute bottom-[-14rem] left-[-10rem] h-[28rem] w-[34rem] rounded-full bg-loss/[0.03] blur-[150px]" />
      <div
        className="absolute inset-0 opacity-[0.7] mask-fade-b"
        style={{ backgroundImage: weave, backgroundSize: "6px 6px" }}
      />
      {/* a single sheen across the weave, so it catches light like a panel */}
      <div className="absolute inset-0 bg-[linear-gradient(115deg,transparent_35%,rgba(255,255,255,0.022)_50%,transparent_65%)]" />
    </Ambient>
  );
}

/* ── /dashboard — brushed panel ───────────────────────────────────────── */

/**
 * Fine vertical brushing with widely spaced registration ticks — the surface a
 * rack-mounted instrument is machined from, rather than paper it is drawn on.
 */
export function DashboardBackdrop() {
  return (
    <Ambient base="bg-[#070708]">
      <div className="absolute right-[-14rem] top-[-16rem] h-[36rem] w-[46rem] rounded-full bg-aqua/[0.06] blur-[165px]" />
      <div className="absolute bottom-[-14rem] left-[-12rem] h-[30rem] w-[38rem] rounded-full bg-signal/[0.05] blur-[155px]" />
      <div
        className="absolute inset-0 opacity-[0.55] mask-fade-b"
        style={{
          backgroundImage:
            "repeating-linear-gradient(90deg, rgba(255,255,255,0.02) 0 1px, transparent 1px 4px)",
        }}
      />
      <div
        className="absolute inset-0 opacity-40 mask-fade-b"
        style={{
          backgroundImage:
            "linear-gradient(to right, rgba(176,184,196,0.08) 1px, transparent 1px), linear-gradient(to bottom, rgba(176,184,196,0.08) 1px, transparent 1px)",
          backgroundSize: "240px 240px",
        }}
      />
    </Ambient>
  );
}

/* ── developer portal — deliberately nothing ──────────────────────────── */

/**
 * No pattern at all.
 *
 * Documentation is read for long stretches, often while comparing it against
 * a terminal on the other half of the screen. Texture behind prose is noise
 * competing with the sentence you are on, and every decorative layer here was
 * removed rather than softened. One flat surface and a single cool gradient at
 * the top edge so the header has something to sit against.
 */
export function DocsBackdrop() {
  return (
    <Ambient base="bg-[#070708]">
      <div className="absolute inset-x-0 top-0 h-[26rem] bg-[linear-gradient(to_bottom,rgba(234,181,79,0.055),transparent)]" />
    </Ambient>
  );
}

/* ── /support — concentric rings ──────────────────────────────────────── */

/** Slow concentric rings — a help desk, radiating. */
export function SupportBackdrop() {
  return (
    <Ambient base="bg-[#070708]">
      <div className="absolute left-1/2 top-[-22rem] h-[38rem] w-[50rem] -translate-x-1/2 rounded-full bg-gold/[0.05] blur-[170px]" />
      <div
        className="absolute left-1/2 top-[-30rem] h-[80rem] w-[80rem] -translate-x-1/2 opacity-[0.45] mask-fade-b"
        style={{
          backgroundImage:
            "repeating-radial-gradient(circle at 50% 50%, rgba(234,181,79,0.06) 0 1px, transparent 1px 88px)",
        }}
      />
    </Ambient>
  );
}

/* ── /community — light only ──────────────────────────────────────────── */

export function CommunityBackdrop() {
  return (
    <Ambient base="bg-[#070708]">
      <div className="absolute right-[-12rem] top-[-18rem] h-[36rem] w-[46rem] rounded-full bg-gold/[0.05] blur-[165px]" />
      <div className="absolute bottom-[-14rem] left-[-12rem] h-[30rem] w-[38rem] rounded-full bg-signal/[0.04] blur-[155px]" />
    </Ambient>
  );
}

/* ── /status — uptime ticks ───────────────────────────────────────────── */

/** Narrow vertical ticks, the same shape as the uptime bars on the page. */
export function StatusBackdrop({ healthy }: { healthy: boolean }) {
  return (
    <Ambient base="bg-[#060607]">
      <div
        className={`absolute left-1/2 top-[-20rem] h-[34rem] w-[48rem] -translate-x-1/2 rounded-full blur-[165px] ${
          healthy ? "bg-gold/[0.06]" : "bg-gold/[0.05]"
        }`}
      />
      <div
        className="absolute inset-0 opacity-[0.5] mask-fade-b"
        style={{
          backgroundImage:
            "repeating-linear-gradient(90deg, rgba(176,184,196,0.07) 0 2px, transparent 2px 14px)",
          backgroundSize: "auto 40%",
          backgroundRepeat: "repeat-x",
        }}
      />
    </Ambient>
  );
}

/* ── legal — warm paper ───────────────────────────────────────────────── */

export function LegalBackdrop() {
  return (
    <Ambient base="bg-[#0A0908]">
      <div className="absolute left-1/2 top-[-26rem] h-[42rem] w-[52rem] -translate-x-1/2 rounded-full bg-gold/[0.035] blur-[180px]" />
    </Ambient>
  );
}

/* ── 404 ──────────────────────────────────────────────────────────────── */

export function NotFoundBackdrop() {
  return (
    <Ambient base="bg-ink">
      <div className="absolute left-1/2 top-[-20rem] h-[36rem] w-[46rem] -translate-x-1/2 rounded-full bg-gold/[0.05] blur-[160px]" />
      <div className="absolute inset-0 bg-[radial-gradient(ellipse_at_center,transparent_58%,rgba(0,0,0,0.6))]" />
    </Ambient>
  );
}
