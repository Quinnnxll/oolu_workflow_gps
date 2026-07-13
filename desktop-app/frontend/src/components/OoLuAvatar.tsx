import { useEffect, useRef, useState } from "react";
import { moodOf, onAvatarSignals } from "../avatar";
import type { MoodState } from "../avatar";

// OoLu's face: a fluid, camouflage-patterned geometric shape with eyes.
// Two layered blobs breathe against each other (the camouflage), the eyes
// blink, widen, and dilate — all driven by the mood engine, never by
// hardcoded theatrics. Deliberately larger than ordinary avatars: the
// user is talking WITH something, mini-RPG style.

const POINTS = 8;
const TAU = Math.PI * 2;

function blobPath(
  cx: number,
  cy: number,
  radius: number,
  time: number,
  agitation: number,
  seed: number,
): string {
  // Per-vertex radial wobble; a closed Catmull-Rom loop keeps it fluid.
  const pts: [number, number][] = [];
  for (let i = 0; i < POINTS; i++) {
    const angle = (i / POINTS) * TAU;
    const wobble =
      Math.sin(time * (0.9 + agitation * 1.6) + i * 1.7 + seed) *
      (0.06 + agitation * 0.16);
    const r = radius * (1 + wobble);
    pts.push([cx + r * Math.cos(angle), cy + r * Math.sin(angle)]);
  }
  let d = "";
  for (let i = 0; i < POINTS; i++) {
    const p0 = pts[(i - 1 + POINTS) % POINTS];
    const p1 = pts[i];
    const p2 = pts[(i + 1) % POINTS];
    const p3 = pts[(i + 2) % POINTS];
    const c1: [number, number] = [
      p1[0] + (p2[0] - p0[0]) / 6,
      p1[1] + (p2[1] - p0[1]) / 6,
    ];
    const c2: [number, number] = [
      p2[0] - (p3[0] - p1[0]) / 6,
      p2[1] - (p3[1] - p1[1]) / 6,
    ];
    if (i === 0) d += `M ${p1[0].toFixed(2)} ${p1[1].toFixed(2)} `;
    d += `C ${c1[0].toFixed(2)} ${c1[1].toFixed(2)}, ${c2[0].toFixed(2)} ${c2[1].toFixed(2)}, ${p2[0].toFixed(2)} ${p2[1].toFixed(2)} `;
  }
  return d + "Z";
}

export function OoLuAvatar({ size = 56 }: { size?: number }) {
  const [state, setState] = useState<MoodState>(() =>
    moodOf({
      listening: false,
      speaking: false,
      workload: 0,
      tone: "neutral",
      userMood: "neutral",
    }),
  );
  // The raw in-flight signal, kept apart from mood: the breathing glow
  // marks "a model turn is running RIGHT NOW", not general busyness.
  const [thinking, setThinking] = useState(false);
  const stateRef = useRef(state);
  stateRef.current = state;

  const bodyRef = useRef<SVGPathElement>(null);
  const camoRef = useRef<SVGPathElement>(null);
  const leftEyeRef = useRef<SVGEllipseElement>(null);
  const rightEyeRef = useRef<SVGEllipseElement>(null);
  const leftPupilRef = useRef<SVGCircleElement>(null);
  const rightPupilRef = useRef<SVGCircleElement>(null);
  const blinkRef = useRef({ until: 0, next: 2 });

  useEffect(
    () =>
      onAvatarSignals((signals) => {
        setState(moodOf(signals));
        setThinking(Boolean(signals.thinking));
      }),
    [],
  );

  useEffect(() => {
    // Reduced motion (or no rAF, e.g. tests): the first frame stands still.
    const still =
      typeof window.matchMedia === "function" &&
      window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    drawFrame(0);
    if (still || typeof requestAnimationFrame !== "function") return;

    let raf = 0;
    const start = performance.now();
    const loop = (now: number) => {
      drawFrame((now - start) / 1000);
      raf = requestAnimationFrame(loop);
    };
    raf = requestAnimationFrame(loop);
    return () => cancelAnimationFrame(raf);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  function drawFrame(t: number) {
    const s = stateRef.current;
    const c = 50;
    bodyRef.current?.setAttribute("d", blobPath(c, c, 38, t, s.agitation, 0));
    camoRef.current?.setAttribute(
      "d",
      blobPath(c, c + 2, 30, t * 1.25, s.agitation, 2.4),
    );
    // Blinking: brief closes on a drifting timer; mood sets the resting lid.
    const blink = blinkRef.current;
    if (t > blink.next) {
      blink.until = t + 0.12;
      blink.next = t + 2.2 + (Math.sin(t * 7.3) + 1) * 1.4;
    }
    const open = t < blink.until ? 0.08 : s.eyeOpen;
    const ry = (5.5 * open).toFixed(2);
    const pupilShift = Math.sin(t * 0.6) * 2 * (0.4 + s.agitation);
    for (const [eyeRef, pupilRef, x] of [
      [leftEyeRef, leftPupilRef, 40],
      [rightEyeRef, rightPupilRef, 60],
    ] as const) {
      eyeRef.current?.setAttribute("ry", ry);
      eyeRef.current?.setAttribute("cx", (x + pupilShift).toFixed(2));
      // The pupils travel with the eyes plus a little lead — a gaze.
      pupilRef.current?.setAttribute("cx", (x + pupilShift * 1.4).toFixed(2));
      pupilRef.current?.setAttribute("r", (3.2 * s.pupil * open).toFixed(2));
    }
  }

  const { hueA, hueB, mood, pupil } = state;
  return (
    <svg
      viewBox="0 0 100 100"
      width={size}
      height={size}
      className="oolu-avatar"
      data-mood={mood}
      data-thinking={thinking ? "true" : undefined}
      role="img"
      aria-label={`OoLu (${mood})`}
    >
      <defs>
        <radialGradient id="oolu-body" cx="35%" cy="30%">
          <stop offset="0%" stopColor={`hsl(${hueA} 75% 62%)`} />
          <stop offset="100%" stopColor={`hsl(${hueB} 70% 40%)`} />
        </radialGradient>
        <radialGradient id="oolu-camo" cx="65%" cy="70%">
          <stop offset="0%" stopColor={`hsl(${hueB} 65% 55% / 0.85)`} />
          <stop offset="100%" stopColor={`hsl(${hueA} 60% 35% / 0.4)`} />
        </radialGradient>
      </defs>
      <path ref={bodyRef} fill="url(#oolu-body)" />
      <path ref={camoRef} fill="url(#oolu-camo)" />
      <ellipse ref={leftEyeRef} cx="40" cy="44" rx="6" ry="5" fill="#fff" />
      <ellipse ref={rightEyeRef} cx="60" cy="44" rx="6" ry="5" fill="#fff" />
      <circle ref={leftPupilRef} cx="40" cy="44" r={3.2 * pupil} fill="#101418" />
      <circle ref={rightPupilRef} cx="60" cy="44" r={3.2 * pupil} fill="#101418" />
    </svg>
  );
}
