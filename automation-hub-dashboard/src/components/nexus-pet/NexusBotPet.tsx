import { useCallback, useEffect, useRef, useState } from "react";
import { useApp } from "../../app-context";
import NexusPetPopover from "./NexusPetPopover";
import NexusPetSettings, { NEXUS_PETS } from "./NexusPetSettings";
import { useNexusPetController } from "./NexusPetController";
import type { NexusPetAppearance, NexusPetId, NexusPetSize } from "./types";
import "./NexusPet.css";

type Interaction = "idle" | "hover" | "click";

/* Sprig's behaviour, kept out of React state so a moving pointer or a timer
   never rerenders the dashboard. `applyPose` turns these inputs into one pose
   (which cell of the sprite sheet shows) and one eye state, written straight
   to the root's dataset for CSS. An unhealthy platform always wins: Sprig is
   never happy while the engine reports a warning, a loss or an error. */
type Behaviour = {
  state: string;
  near: boolean;      // pointer within reach: looks up from the laptop
  hovered: boolean;   // pointer on Sprig: waves
  petted: boolean;    // stroked or held: happy
  glance: boolean;    // an occasional look up while working
  popover: boolean;   // status open: looks at the reader
};
const UNHEALTHY = new Set(["warning", "trade-loss", "error"]);
const PET_HOLD_MS = 480;          // press and hold (touch, pen or mouse) to pet
const PET_STROKE_PX = 80;         // or stroke back and forth across Sprig
const PET_HAPPY_MS = 1700;
const random = (min: number, max: number) => min + Math.random() * (max - min);

function derivePose(b: Behaviour): { pose: string; eyes: string } {
  if (UNHEALTHY.has(b.state)) return { pose: "alert", eyes: "open" };
  const pose = b.hovered || b.petted ? "greet" : b.near || b.glance || b.popover ? "aware" : "working";
  const eyes = b.state === "paused" ? "drowsy"
    : b.state === "offline" && pose === "working" ? "sleep"       // nothing running: dozes at the laptop
    : "open";
  return { pose, eyes };
}

const PET_STORAGE_KEY = "tradelogx:nexus-pet:v1";
const SPRIG_SPRITE_URL = `${import.meta.env.BASE_URL}nexus-pet-concepts/sprig-production-poses-v4.png`;
const PET_IDS = new Set<NexusPetId>(NEXUS_PETS.map((pet) => pet.id));
const PET_SIZES = new Set<NexusPetSize>(["small", "medium", "large"]);
const DEFAULT_APPEARANCE: NexusPetAppearance = { pet: "sprig", size: "medium" };
const LEGACY_PET_IDS: Record<string, NexusPetId> = {
  codex: "sprig",
  seedy: "sprig",
  stacky: "pulse",
  dewey: "orbit",
  rocky: "glint",
  hoots: "echo",
  bsod: "nova",
  fireball: "volt",
  "null-signal": "kiro",
};

const loadAppearance = (): NexusPetAppearance => {
  try {
    const saved = JSON.parse(window.localStorage.getItem(PET_STORAGE_KEY) ?? "null") as Partial<NexusPetAppearance> | null;
    const savedPet = String(saved?.pet ?? "");
    const migratedPet = LEGACY_PET_IDS[savedPet] ?? savedPet;
    return {
      pet: PET_IDS.has(migratedPet as NexusPetId) ? migratedPet as NexusPetId : DEFAULT_APPEARANCE.pet,
      size: saved?.size && PET_SIZES.has(saved.size) ? saved.size : DEFAULT_APPEARANCE.size,
    };
  } catch {
    return DEFAULT_APPEARANCE;
  }
};

export default function NexusBotPet() {
  const app = useApp();
  const model = useNexusPetController(app.selectedInstanceId);
  const rootRef = useRef<HTMLDivElement>(null);
  const buttonRef = useRef<HTMLButtonElement>(null);
  const frameRef = useRef<number | null>(null);
  const interactionTimer = useRef<number | null>(null);
  const curiosityTimer = useRef<number | null>(null);
  const [open, setOpen] = useState(false);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [interaction, setInteraction] = useState<Interaction>("idle");
  const [appearance, setAppearance] = useState<NexusPetAppearance>(loadAppearance);
  const [spriteLoaded, setSpriteLoaded] = useState(false);
  const behaviour = useRef<Behaviour>({ state: model.state, near: false, hovered: false, petted: false,
                                        glance: false, popover: false });
  const petTimer = useRef<number | null>(null);
  const holdTimer = useRef<number | null>(null);
  const suppressClick = useRef(false);
  const lastPet = useRef(Number.NEGATIVE_INFINITY);   // page time starts at 0, so 0 would block the first second
  const stroke = useRef({ at: 0, x: 0, dir: 0, distance: 0, turns: 0 });

  const applyPose = useCallback(() => {
    const root = rootRef.current;
    if (!root) return;
    const { pose, eyes } = derivePose(behaviour.current);
    if (root.dataset.pose !== pose) root.dataset.pose = pose;
    if (root.dataset.eyes !== eyes) root.dataset.eyes = eyes;
  }, []);

  const pet = useCallback(() => {
    const root = rootRef.current;
    const now = performance.now();
    if (!root || now - lastPet.current < 900) return;
    lastPet.current = now;
    if (UNHEALTHY.has(behaviour.current.state)) return;   // attentive, never happy, when something is wrong
    behaviour.current.petted = true;
    root.dataset.petted = "false";
    void root.offsetWidth;                                  // restart the hearts for a second pat
    root.dataset.petted = "true";
    applyPose();
    if (petTimer.current !== null) window.clearTimeout(petTimer.current);
    petTimer.current = window.setTimeout(() => {
      behaviour.current.petted = false;
      if (rootRef.current) rootRef.current.dataset.petted = "false";
      applyPose();
    }, PET_HAPPY_MS);
  }, [applyPose]);

  const petName = NEXUS_PETS.find((pet) => pet.id === appearance.pet)?.name ?? "Sprig";

  const updateAppearance = (next: NexusPetAppearance) => {
    setAppearance(next);
    try {
      window.localStorage.setItem(PET_STORAGE_KEY, JSON.stringify(next));
    } catch {
      // Keep the in-memory selection when browser storage is unavailable.
    }
  };

  const react = useCallback((next: Exclude<Interaction, "idle">, duration: number) => {
    if (interactionTimer.current !== null) window.clearTimeout(interactionTimer.current);
    setInteraction(next);
    interactionTimer.current = window.setTimeout(() => setInteraction("idle"), duration);
  }, []);

  useEffect(() => () => {
    if (interactionTimer.current !== null) window.clearTimeout(interactionTimer.current);
    if (curiosityTimer.current !== null) window.clearTimeout(curiosityTimer.current);
    if (frameRef.current !== null) window.cancelAnimationFrame(frameRef.current);
    if (petTimer.current !== null) window.clearTimeout(petTimer.current);
    if (holdTimer.current !== null) window.clearTimeout(holdTimer.current);
  }, []);

  // The engine's state and the status panel feed the pose.
  useEffect(() => { behaviour.current.state = model.state; applyPose(); }, [model.state, applyPose]);
  useEffect(() => { behaviour.current.popover = open || settingsOpen; applyPose(); }, [open, settingsOpen, applyPose]);

  // Life while nobody is interacting: Sprig blinks every few seconds and now
  // and then looks up from the laptop. Neither runs with reduced motion or in
  // a hidden tab.
  useEffect(() => {
    if (appearance.pet !== "sprig") return;
    const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)");
    let blinkTimer = 0, glanceTimer = 0, glanceEnd = 0, open = 0;
    const quiet = () => reduceMotion.matches || document.hidden;
    const blink = () => {
      const root = rootRef.current;
      if (root && !quiet() && root.dataset.eyes === "open") {
        root.dataset.blink = "true";
        open = window.setTimeout(() => { if (rootRef.current) rootRef.current.dataset.blink = "false"; }, 150);
      }
      blinkTimer = window.setTimeout(blink, random(2600, 6400));
    };
    const glance = () => {
      const b = behaviour.current;
      if (!quiet() && !b.near && !b.hovered && !b.petted && b.state !== "offline") {
        b.glance = true;
        applyPose();
        glanceEnd = window.setTimeout(() => { behaviour.current.glance = false; applyPose(); }, 1800);
      }
      glanceTimer = window.setTimeout(glance, random(22000, 40000));
    };
    blinkTimer = window.setTimeout(blink, random(1500, 3500));
    glanceTimer = window.setTimeout(glance, random(18000, 30000));
    return () => { [blinkTimer, glanceTimer, glanceEnd, open].forEach((t) => window.clearTimeout(t)); };
  }, [appearance.pet, applyPose]);

  useEffect(() => {
    const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)");
    const trackPointer = (event: PointerEvent) => {
      if (reduceMotion.matches || frameRef.current !== null) return;
      const { clientX, clientY } = event;
      frameRef.current = window.requestAnimationFrame(() => {
        frameRef.current = null;
        const root = rootRef.current;
        const button = buttonRef.current;
        if (!root || !button) return;
        const rect = button.getBoundingClientRect();
        const dx = clientX - (rect.left + rect.width / 2);
        const dy = clientY - (rect.top + rect.height / 2);
        const distance = Math.hypot(dx, dy);
        const strength = Math.max(0, 1 - distance / 190);
        const x = distance ? dx / distance * strength : 0;
        const y = distance ? dy / distance * strength : 0;
        const near = distance < 190;
        const close = distance < 82;
        root.dataset.near = String(near);
        root.dataset.close = String(close);
        if (behaviour.current.near !== near) { behaviour.current.near = near; applyPose(); }
        root.style.setProperty("--pet-look-x", `${(x * 3).toFixed(2)}px`);
        root.style.setProperty("--pet-look-y", `${(y * 1.7).toFixed(2)}px`);
        root.style.setProperty("--pet-head-turn", `${(x * 5.2).toFixed(2)}deg`);
        root.style.setProperty("--pet-head-lift", `${Math.min(0, y * 1.3).toFixed(2)}px`);
        root.style.setProperty("--pet-body-x", `${(x * 1.25).toFixed(2)}px`);
        root.style.setProperty("--pet-body-turn", `${(x * 1.8).toFixed(2)}deg`);
        root.style.setProperty("--pet-leaf-turn", `${(x * 8 - y * 2).toFixed(2)}deg`);
        root.style.setProperty("--pet-glow-scale", (1 + strength * .16).toFixed(3));
      });
    };
    window.addEventListener("pointermove", trackPointer, { passive: true });
    return () => window.removeEventListener("pointermove", trackPointer);
  }, [applyPose]);

  useEffect(() => {
    const onVisibility = () => { if (rootRef.current) rootRef.current.dataset.hidden = String(document.hidden); };
    onVisibility();
    document.addEventListener("visibilitychange", onVisibility);
    return () => document.removeEventListener("visibilitychange", onVisibility);
  }, []);

  useEffect(() => {
    if (!open && !settingsOpen) return;
    const onPointerDown = (event: PointerEvent) => {
      if (!rootRef.current?.contains(event.target as Node)) {
        setOpen(false);
        setSettingsOpen(false);
      }
    };
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      setOpen(false);
      setSettingsOpen(false);
    };
    document.addEventListener("pointerdown", onPointerDown);
    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("pointerdown", onPointerDown);
      document.removeEventListener("keydown", onKeyDown);
    };
  }, [open, settingsOpen]);

  const toggle = () => {
    if (suppressClick.current) {             // the press was a pat, not a request for status
      suppressClick.current = false;
      return;
    }
    react("click", 560);
    setSettingsOpen(false);
    setOpen((value) => !value);
  };

  const openSettings = () => {
    setOpen(false);
    setSettingsOpen(true);
  };

  const greet = () => {
    const root = rootRef.current;
    if (root) root.dataset.hovered = "true";
    behaviour.current.hovered = true;
    applyPose();
    react("hover", 480);
    if (curiosityTimer.current !== null) window.clearTimeout(curiosityTimer.current);
    curiosityTimer.current = window.setTimeout(() => {
      if (rootRef.current?.dataset.hovered === "true") rootRef.current.dataset.curious = "true";
    }, 650);
  };

  const settle = () => {
    if (curiosityTimer.current !== null) window.clearTimeout(curiosityTimer.current);
    cancelHold();
    behaviour.current.hovered = false;
    applyPose();
    const root = rootRef.current;
    if (!root) return;
    root.dataset.hovered = "false";
    root.dataset.curious = "false";
  };

  // Petting: press and hold anywhere on Sprig, or stroke back and forth.
  const cancelHold = () => {
    if (holdTimer.current !== null) window.clearTimeout(holdTimer.current);
    holdTimer.current = null;
  };
  const startHold = (event: React.PointerEvent) => {
    if (event.button !== 0) return;
    cancelHold();
    stroke.current = { at: performance.now(), x: event.clientX, dir: 0, distance: 0, turns: 0 };
    holdTimer.current = window.setTimeout(() => {
      holdTimer.current = null;
      suppressClick.current = true;
      pet();
    }, PET_HOLD_MS);
  };
  const strokeOrDrift = (event: React.PointerEvent) => {
    const now = performance.now();
    if (now - stroke.current.at > 450) {     // a pause ends a stroke; this move starts a new one
      stroke.current = { at: now, x: event.clientX, dir: 0, distance: 0, turns: 0 };
      return;
    }
    const s = stroke.current;
    s.at = now;
    const dx = event.clientX - s.x;
    if (Math.abs(dx) < 2) return;
    if (holdTimer.current !== null && Math.abs(dx) > 8) cancelHold();   // moving, not holding
    const dir = Math.sign(dx);
    if (s.dir && dir !== s.dir) s.turns += 1;
    s.dir = dir;
    s.distance += Math.abs(dx);
    s.x = event.clientX;
    if (event.pointerType === "mouse" && s.turns >= 2 && s.distance >= PET_STROKE_PX) {
      stroke.current = { at: now, x: event.clientX, dir: 0, distance: 0, turns: 0 };
      pet();
    }
  };

  return (
    <div ref={rootRef} className="nexus-pet-root" data-state={model.state} data-interaction={interaction} data-pet={appearance.pet} data-size={appearance.size} data-sprite-loaded={appearance.pet === "sprig" && spriteLoaded}>
      {open && <NexusPetPopover model={model} onClose={() => setOpen(false)} onOpenSettings={openSettings} />}
      {settingsOpen && <NexusPetSettings appearance={appearance} onChange={updateAppearance} onClose={() => setSettingsOpen(false)} />}
      <button
        ref={buttonRef}
        type="button"
        className="nexus-pet-button"
        aria-label={`${petName}, Nexus pet: ${model.statusLabel}. Open status.`}
        aria-haspopup="dialog"
        aria-expanded={open || settingsOpen}
        title={`${petName} · Nexus Engine · ${model.statusLabel} · click for status, hold or stroke to pet`}
        onClick={toggle}
        onPointerEnter={greet}
        onPointerLeave={settle}
        onPointerDown={startHold}
        onPointerUp={cancelHold}
        onPointerCancel={cancelHold}
        onPointerMove={strokeOrDrift}
        onContextMenu={(event) => { if (suppressClick.current) event.preventDefault(); }}
      >
        <span className="nexus-pet-stage" aria-hidden="true">
          {appearance.pet === "sprig" && (
            <span className="nexus-pet-production-sprite">
              <img
                className="nexus-pet-production-image"
                src={SPRIG_SPRITE_URL}
                alt=""
                draggable={false}
                onLoad={() => setSpriteLoaded(true)}
                onError={() => setSpriteLoaded(false)}
              />
              {/* Live LED eyes over the painted ones: they follow the pointer,
                  blink, doze and droop. Hidden while Sprig waves (its happy
                  eyes are painted). */}
              <span className="nexus-pet-live-eyes">
                <span className="nexus-pet-socket nexus-pet-socket-l"><i className="nexus-pet-led" /></span>
                <span className="nexus-pet-socket nexus-pet-socket-r"><i className="nexus-pet-led" /></span>
              </span>
            </span>
          )}
          {appearance.pet === "sprig" && (
            <>
              <span className="nexus-pet-hearts">
                {[0, 1, 2].map((i) => (
                  <svg key={i} viewBox="0 0 12 11" focusable="false"><path d="M6 10.4 1.3 5.9a3 3 0 0 1 4.3-4.2L6 2.1l.4-.4a3 3 0 0 1 4.3 4.2Z" /></svg>
                ))}
              </span>
              <span className="nexus-pet-zzz"><i>z</i><i>z</i><i>z</i></span>
            </>
          )}
          <svg className="nexus-pet-vector-fallback" viewBox="0 0 64 74" role="presentation" focusable="false">
            <ellipse className="nexus-pet-floor" cx="32" cy="69" rx="20" ry="3.5" />
            <g className="nexus-pet-avatar">
              <g className="nexus-pet-body">
                <path d="M22 48h20c4 0 7 3 7 7v8c0 3-2 5-5 5H20c-3 0-5-2-5-5v-8c0-4 3-7 7-7Z" />
                <path className="nexus-pet-body-gold" d="M27 52h10l2 4-7 7-7-7 2-4Z" />
                <path className="nexus-pet-arm nexus-pet-arm-left" d="M17 53c-4 1-6 4-6 8" />
                <path className="nexus-pet-arm nexus-pet-arm-right" d="M47 53c4 1 6 4 6 8" />
                <path className="nexus-pet-foot" d="M22 67v3M42 67v3" />
              </g>
              <g className="nexus-pet-head">
                <path className="nexus-pet-antenna" d="M32 15V9" />
                <g className="nexus-pet-leaf">
                  <path d="M32 10c1-6 6-8 11-7-1 5-4 9-11 7Z" />
                  <path d="M32 10c-1-5-5-7-9-6 0 4 3 7 9 6Z" />
                </g>
                <path className="nexus-pet-pulse-mark" d="M24 10h4l2-5 4 10 2-5h4" />
                <g className="nexus-pet-orbit-mark"><ellipse cx="32" cy="9" rx="12" ry="4" /><circle cx="44" cy="8" r="1.6" /></g>
                <path className="nexus-pet-glint-mark" d="M32 2l1.5 4.5L38 8l-4.5 1.5L32 14l-1.5-4.5L26 8l4.5-1.5L32 2Z" />
                <path className="nexus-pet-echo-mark" d="M14 24v14m-4-11v8m44-11v14m4-11v8" />
                <path className="nexus-pet-nova-mark" d="M25 14l3-7 4 4 4-8 4 8 4-4 2 7" />
                <path className="nexus-pet-volt-mark" d="M37 2l-8 9h5l-3 8 9-11h-5l2-6Z" />
                <path className="nexus-pet-kiro-mark" d="M27 5h10l-5 8-5-8Z" />
                <rect className="nexus-pet-head-shell" x="10" y="15" width="44" height="36" rx="14" />
                <rect className="nexus-pet-face" x="15" y="21" width="34" height="23" rx="9" />
                <path className="nexus-pet-brow" d="M20 27h9M35 27h9" />
                <g className="nexus-pet-eyes">
                  <rect x="21" y="30" width="7" height="4" rx="2" />
                  <rect x="36" y="30" width="7" height="4" rx="2" />
                </g>
                <path className="nexus-pet-mouth" d="M28 39h8" />
                <path className="nexus-pet-smile" d="M27.5 38.5q4.5 3 9 0" />
                <path className="nexus-pet-trim" d="M18 46c8 3 20 3 28 0" />
                <path className="nexus-pet-warning" d="M50 18l5-8 5 8h-10Zm5-5v2.5m0 1.5v.2" />
              </g>
              <g className="nexus-pet-particles">
                <circle cx="8" cy="45" r="1.2" /><circle cx="56" cy="43" r="1" /><circle cx="52" cy="52" r=".8" />
              </g>
            </g>
            <g className="nexus-pet-laptop">
              <path className="nexus-pet-laptop-shell" d="M3 49.5 29 52l-1.5 15L5 64.5 3 49.5Z" />
              <path className="nexus-pet-laptop-screen" d="m6 52 20 2-1 9.5-17.5-2L6 52Z" />
              <path className="nexus-pet-laptop-chart" d="m9 59 2.5-2 2 1 2.5-3 2 2 2-4 2.5 3" />
              <path className="nexus-pet-laptop-base" d="m4.5 65 23 2 5 2.5-23.5-2L4.5 65Z" />
              <g className="nexus-pet-typing-hands">
                <circle cx="25" cy="56" r="2.2" />
                <circle cx="31" cy="58" r="2.2" />
              </g>
            </g>
          </svg>
        </span>
      </button>
    </div>
  );
}
