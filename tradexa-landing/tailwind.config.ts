import type { Config } from "tailwindcss";

/**
 * TradeLogX Nexus design tokens, shared with the dashboard.
 *
 * One palette for the whole platform: the dashboard's near-black surfaces,
 * white text in a few tiers, ONE accent (the dashboard's gold, #EAB54F), and
 * green / red reserved for what they mean (profit / loss, pass / fail).
 * Everything else is neutral. The values mirror the dashboard's CSS tokens
 * (automation-hub-dashboard/src/index.css: --bg, --card-*, --gold, --green,
 * --red, --dim) so moving from the site into the app changes nothing.
 */
export default {
  darkMode: "class",
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        // Core surfaces
        ink: {
          DEFAULT: "#070708", // primary black
          800: "#0E0E10",
          700: "#121214",
          600: "#17171A",
          500: "#1E1E22",
          400: "#2A2A2F",
        },
        // Brand
        gold: {
          DEFAULT: "#EAB54F",
          soft: "#F2C766",
          deep: "#A9832F",
        },
        // Secondary tone for data and links: neutral silver (the dashboard's
        // --dim), not a second hue. Kept as its own name so it can still be
        // told apart from gold in two-series diagrams.
        signal: {
          DEFAULT: "#B0B8C4",
          soft: "#D4D9E0",
          deep: "#1A1A1E",
        },
        emerald: {
          DEFAULT: "#22C55E",
          soft: "#4ADE80",
          deep: "#16A34A",
        },
        // Semantic only: profit / pass and loss / fail. Never decoration.
        loss: {
          DEFAULT: "#EF4444",
          soft: "#F87171",
          deep: "#DC2626",
        },
        line: "rgba(255,255,255,0.08)",
        "line-strong": "rgba(255,255,255,0.14)",

        // ── Page surface aliases ─────────────────────────────────────────
        // Product pages used to carry their own hue families (electric blue,
        // aqua, navy, phosphor green). They now alias the one platform
        // palette, so every page reads as the same product the dashboard is.
        // The names stay so the pages need no rewrite.

        // /engine surfaces; `electric` is the gold accent, `aqua` the silver
        graphite: {
          DEFAULT: "#0B0B0D",
          800: "#121214",
          700: "#141417",
          600: "#1C1C20",
          500: "#26262B",
        },
        electric: {
          DEFAULT: "#EAB54F",
          soft: "#F2C766",
          deep: "#1F1A10",
        },
        aqua: {
          DEFAULT: "#B0B8C4",
          soft: "#D4D9E0",
          deep: "#1A1C20",
        },

        // /live-trade surfaces
        term: {
          DEFAULT: "#070708",
          800: "#0E0E10",
          700: "#0F0F11",
          600: "#17171A",
          500: "#1E1E22",
        },

        // /selectivity surfaces
        obsidian: {
          DEFAULT: "#040404",
          800: "#0A0908",
          700: "#12100B",
          600: "#1B1710",
          500: "#272117",
        },

        // /security surfaces
        navy: {
          DEFAULT: "#070708",
          800: "#0C0C0E",
          700: "#111113",
          600: "#1A1A1E",
          500: "#26262B",
        },
      },
      fontFamily: {
        sans: ["Inter", "system-ui", "-apple-system", "Segoe UI", "sans-serif"],
        mono: ["JetBrains Mono", "SFMono-Regular", "Menlo", "monospace"],
      },
      spacing: {
        // `h-13` (the large button) was used but never defined, so large
        // buttons had no height at all and sized to whatever their content did.
        13: "3.25rem",
      },
      borderRadius: {
        xl: "1rem",
        "2xl": "1.25rem",
        "3xl": "1.75rem",
      },
      boxShadow: {
        glass: "0 1px 0 0 rgba(255,255,255,0.05) inset, 0 24px 60px -20px rgba(0,0,0,0.7)",
        gold: "0 10px 40px -12px rgba(234,181,79,0.45)",
        card: "0 20px 50px -24px rgba(0,0,0,0.8)",
      },
      backgroundImage: {
        "gold-sheen": "linear-gradient(135deg, #F2C766 0%, #EAB54F 45%, #C99A3A 100%)",
        "radial-fade": "radial-gradient(ellipse 80% 60% at 50% -10%, rgba(234,181,79,0.14), transparent 60%)",
        // page base: barely-warm charcoal falling to true black — depth without
        // leaving the near-black identity
        "page-depth":
          "radial-gradient(120% 85% at 50% 0%, #0D0C0A 0%, #070708 48%, #050506 100%)",
        // Landing-only. There used to be three more grid variants beside this
        // one — grid-cool, grid-emerald and grid-term, one per product page,
        // all the same 1px lattice in a different hue — and the app painted
        // this one behind every route besides. That is how each page ended up
        // looking like the landing page wearing a filter. The variants are
        // gone and the global layer with them; this is now applied only by the
        // hero and two landing sections, via `GridTexture`. A new page needing
        // a texture gets its own in components/site/backdrops.tsx.
        "grid-lines":
          "linear-gradient(to right, rgba(226,214,182,0.045) 1px, transparent 1px), linear-gradient(to bottom, rgba(226,214,182,0.045) 1px, transparent 1px)",
        "electric-sheen": "linear-gradient(135deg, #F2C766 0%, #EAB54F 45%, #C99A3A 100%)",
        "emerald-sheen": "linear-gradient(135deg, #86EFAC 0%, #22C55E 50%, #16A34A 100%)",
        // phosphor scanlines — the terminal surface only
        scanlines:
          "repeating-linear-gradient(to bottom, rgba(255,255,255,0.028) 0px, rgba(255,255,255,0.028) 1px, transparent 1px, transparent 3px)",
      },
      keyframes: {
        "fade-up": {
          "0%": { opacity: "0", transform: "translateY(14px)" },
          "100%": { opacity: "1", transform: "translateY(0)" },
        },
        float: {
          "0%,100%": { transform: "translateY(0)" },
          "50%": { transform: "translateY(-8px)" },
        },
        bloom: {
          "0%,100%": { transform: "translate(-50%, 0) scale(1)", opacity: "1" },
          "50%": { transform: "translate(-46%, 2rem) scale(1.08)", opacity: "0.85" },
        },
        "bloom-slow": {
          "0%,100%": { transform: "translate(0, 0) scale(1)" },
          "50%": { transform: "translate(-3rem, -2.5rem) scale(1.12)" },
        },
        shimmer: {
          "100%": { transform: "translateX(100%)" },
        },
        "pulse-ring": {
          "0%": { boxShadow: "0 0 0 0 rgba(34,197,94,0.45)" },
          "70%": { boxShadow: "0 0 0 8px rgba(34,197,94,0)" },
          "100%": { boxShadow: "0 0 0 0 rgba(34,197,94,0)" },
        },
        "grid-pan": {
          "0%": { backgroundPosition: "0 0" },
          "100%": { backgroundPosition: "40px 40px" },
        },
        // Per-page motion primitives (CSS-driven so they cost no JS frames).
        "dash-flow": {
          "0%": { strokeDashoffset: "24" },
          "100%": { strokeDashoffset: "0" },
        },
        "scan-down": {
          "0%": { transform: "translateY(-100%)" },
          "100%": { transform: "translateY(1000%)" },
        },
        "sweep-x": {
          "0%": { transform: "translateX(-120%)" },
          "100%": { transform: "translateX(120%)" },
        },
        "caret-blink": {
          "0%,49%": { opacity: "1" },
          "50%,100%": { opacity: "0" },
        },
        "ping-ring": {
          "0%": { transform: "scale(0.85)", opacity: "0.7" },
          "100%": { transform: "scale(1.9)", opacity: "0" },
        },
        "tape-scroll": {
          "0%": { transform: "translateX(0)" },
          "100%": { transform: "translateX(-50%)" },
        },
        // Light travelling across gold type. Background-position only: no
        // layout, no repaint of anything but the glyphs themselves.
        "gold-pan": {
          "0%": { backgroundPosition: "0% 50%" },
          "100%": { backgroundPosition: "200% 50%" },
        },
        // A conic highlight orbiting a card's border.
        "orbit": {
          "0%": { transform: "translate(-50%, -50%) rotate(0deg)" },
          "100%": { transform: "translate(-50%, -50%) rotate(360deg)" },
        },
        "rise-in": {
          "0%": { opacity: "0", transform: "translateY(8px) scale(0.98)" },
          "100%": { opacity: "1", transform: "translateY(0) scale(1)" },
        },
      },
      animation: {
        "fade-up": "fade-up 0.6s cubic-bezier(0.22,1,0.36,1) both",
        float: "float 6s ease-in-out infinite",
        shimmer: "shimmer 2.5s infinite",
        "pulse-ring": "pulse-ring 2s cubic-bezier(0.4,0,0.6,1) infinite",
        "grid-pan": "grid-pan 8s linear infinite",
        bloom: "bloom 18s ease-in-out infinite",
        "bloom-slow": "bloom-slow 26s ease-in-out infinite",
        "dash-flow": "dash-flow 1.1s linear infinite",
        "scan-down": "scan-down 4.5s linear infinite",
        "sweep-x": "sweep-x 2.8s ease-in-out infinite",
        "caret-blink": "caret-blink 1.1s step-end infinite",
        "ping-ring": "ping-ring 2.2s cubic-bezier(0,0,0.2,1) infinite",
        "tape-scroll": "tape-scroll 38s linear infinite",
        "gold-pan": "gold-pan 7s linear infinite",
        orbit: "orbit 9s linear infinite",
        "rise-in": "rise-in 0.5s cubic-bezier(0.22,1,0.36,1) both",
      },
    },
  },
  plugins: [],
} satisfies Config;
