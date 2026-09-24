import type { Config } from "tailwindcss";

/**
 * Tradexa Trading Bot — dark-luxury design tokens.
 * Bloomberg Terminal precision × Apple restraint × Linear polish.
 */
export default {
  darkMode: "class",
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        // Core surfaces
        ink: {
          DEFAULT: "#08080A", // primary black
          800: "#0C0C0F",
          700: "#111114",
          600: "#17171B",
          500: "#1E1E23",
          400: "#2A2A31",
        },
        // Brand
        gold: {
          DEFAULT: "#C9A24B",
          soft: "#E7CE86",
          deep: "#8A7233",
        },
        // Secondary brand accent — TradeLogX Nexus "signal blue" (data / links)
        signal: {
          DEFAULT: "#3E7BD6",
          soft: "#6EA3EC",
          deep: "#13233F",
        },
        emerald: {
          DEFAULT: "#2FBF71",
          soft: "#4FD98E",
          deep: "#1E9457",
        },
        loss: {
          DEFAULT: "#E5605B", // soft red
          soft: "#F07E7A",
          deep: "#C24A45",
        },
        line: "rgba(255,255,255,0.08)",
        "line-strong": "rgba(255,255,255,0.14)",

        // ── Per-page palettes ────────────────────────────────────────────
        // Each dedicated route gets its own surface + accent family so the
        // page reads as its own product, while every family is derived from
        // the same Nexus primaries (gold, signal blue, emerald) rather than
        // introducing unrelated hues.

        // /engine — AI operating system: cold graphite under electric blue
        graphite: {
          DEFAULT: "#0B0E12",
          800: "#0E1219",
          700: "#131A24",
          600: "#1A2331",
          500: "#243043",
        },
        electric: {
          DEFAULT: "#2E7BFF",
          soft: "#7CADFF",
          deep: "#0E1E3C",
        },
        // named `aqua` rather than `cyan` so Tailwind's default cyan scale
        // stays intact for anything that still uses it
        aqua: {
          DEFAULT: "#22D3EE",
          soft: "#7DE9F8",
          deep: "#0B4453",
        },

        // /live-trade — trading terminal: near-black with phosphor accents
        term: {
          DEFAULT: "#06080A",
          800: "#0A0D11",
          700: "#0E1318",
          600: "#151C22",
          500: "#1E272F",
        },

        // /selectivity — premium black + gold
        obsidian: {
          DEFAULT: "#040404",
          800: "#0A0908",
          700: "#12100B",
          600: "#1B1710",
          500: "#272117",
        },

        // /security — dark navy under emerald
        navy: {
          DEFAULT: "#060B15",
          800: "#09101F",
          700: "#0D172A",
          600: "#132239",
          500: "#1B3050",
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
        gold: "0 10px 40px -12px rgba(200,169,75,0.45)",
        card: "0 20px 50px -24px rgba(0,0,0,0.8)",
      },
      backgroundImage: {
        "gold-sheen": "linear-gradient(135deg, #E7D89A 0%, #C8A94B 45%, #A98E3A 100%)",
        "radial-fade": "radial-gradient(ellipse 80% 60% at 50% -10%, rgba(200,169,75,0.14), transparent 60%)",
        // page base: barely-warm charcoal falling to true black — depth without
        // leaving the near-black identity
        "page-depth":
          "radial-gradient(120% 85% at 50% 0%, #0D0C0A 0%, #08080A 48%, #050506 100%)",
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
        "electric-sheen": "linear-gradient(135deg, #7CADFF 0%, #2E7BFF 45%, #22D3EE 100%)",
        "emerald-sheen": "linear-gradient(135deg, #7DF0B4 0%, #2FBF71 50%, #1E9457 100%)",
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
          "0%": { boxShadow: "0 0 0 0 rgba(47,191,113,0.45)" },
          "70%": { boxShadow: "0 0 0 8px rgba(47,191,113,0)" },
          "100%": { boxShadow: "0 0 0 0 rgba(47,191,113,0)" },
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
