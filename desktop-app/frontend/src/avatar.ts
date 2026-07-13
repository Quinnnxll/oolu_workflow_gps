// The avatar's mind: pure signal fusion, no pixels.
//
// OoLu's living profile reacts to four honest inputs — voice activity
// (listening/speaking), workload (active runs), the conversation's tone
// (the assistant's last turn), and the user's mood (their last message).
// This module turns those into one MoodState; the SVG component only
// draws what this decides, so the behavior is testable without a canvas.

export type Mood = "calm" | "happy" | "thinking" | "worried" | "excited";

export interface AvatarSignals {
  listening: boolean;
  speaking: boolean;
  // A model turn is in flight — the face glows while it reasons, so the
  // user knows OoLu is still working on it.
  thinking?: boolean;
  workload: number; // active (non-terminal) runs
  tone: "neutral" | "good" | "bad" | "asking";
  userMood: "neutral" | "positive" | "negative" | "urgent";
}

export interface MoodState {
  mood: Mood;
  // 0..1 — how much the blob churns (voice + busyness drive it).
  agitation: number;
  // 0..1 — eyelid openness; 1 wide awake, 0.35 squint.
  eyeOpen: number;
  // 0..1 — pupil size; attention dilates.
  pupil: number;
  // Two hues for the camouflage gradient, degrees.
  hueA: number;
  hueB: number;
}

export const DEFAULT_SIGNALS: AvatarSignals = {
  listening: false,
  speaking: false,
  thinking: false,
  workload: 0,
  tone: "neutral",
  userMood: "neutral",
};

const _POSITIVE = /\b(thanks|thank you|great|awesome|nice|love|perfect|cool|good|well done)\b/i;
const _NEGATIVE = /\b(wrong|bad|broken|hate|angry|terrible|awful|fail|failed|annoy|useless)\b/i;
const _URGENT = /\b(asap|urgent|now|immediately|quick|hurry|emergency)\b/i;

// The user's mood from their message — a deliberately light heuristic
// (words, exclamations, shouting), replaced by the chat model's judgement
// once one is wired. Honest inputs beat pretend empathy.
export function deriveUserMood(text: string): AvatarSignals["userMood"] {
  const trimmed = text.trim();
  if (!trimmed) return "neutral";
  if (_URGENT.test(trimmed) || /!{2,}/.test(trimmed)) return "urgent";
  const letters = trimmed.replace(/[^a-zA-Z]/g, "");
  const shouting =
    letters.length >= 6 && letters === letters.toUpperCase();
  if (_NEGATIVE.test(trimmed) || shouting) return "negative";
  if (_POSITIVE.test(trimmed)) return "positive";
  return "neutral";
}

// The conversation's tone from the assistant's side of the turn.
export function deriveTone(reply: string): AvatarSignals["tone"] {
  if (/didn't work|failed|couldn't|wrong|sorry/i.test(reply)) return "bad";
  if (/\?\s*$/.test(reply.trim()) || /which one do you mean/i.test(reply)) {
    return "asking";
  }
  if (/done|finished|saved|anytime|verified/i.test(reply)) return "good";
  return "neutral";
}

const _HUES: Record<Mood, [number, number]> = {
  calm: [190, 230], // sea blues
  happy: [95, 160], // spring greens
  thinking: [255, 285], // violets
  worried: [18, 42], // ambers
  excited: [315, 350], // magentas
};

export function moodOf(signals: AvatarSignals): MoodState {
  let mood: Mood = "calm";
  if (signals.listening) mood = "excited";
  else if (signals.thinking) mood = "thinking";
  else if (signals.tone === "bad" || signals.userMood === "negative") {
    mood = "worried";
  } else if (signals.workload >= 2 || signals.userMood === "urgent") {
    mood = "thinking";
  } else if (signals.tone === "good" || signals.userMood === "positive") {
    mood = "happy";
  } else if (signals.tone === "asking") mood = "thinking";

  const agitation = Math.min(
    1,
    0.18 +
      0.12 * Math.min(signals.workload, 4) +
      (signals.speaking ? 0.3 : 0) +
      (signals.listening ? 0.28 : 0) +
      (signals.thinking ? 0.22 : 0) +
      (signals.userMood === "urgent" ? 0.15 : 0),
  );
  const eyeOpen =
    mood === "worried" ? 0.55 : mood === "thinking" ? 0.7 : mood === "excited" ? 1 : 0.85;
  const pupil = signals.listening || signals.tone === "asking" ? 0.85 : 0.6;
  const [hueA, hueB] = _HUES[mood];
  return { mood, agitation, eyeOpen, pupil, hueA, hueB };
}

// ---------------------------------------------------------------------------
// A tiny pub/sub so the chat (deep in one pane) can feed the avatar (in the
// sidebar) without threading props through every layer.
// ---------------------------------------------------------------------------
type Listener = (signals: AvatarSignals) => void;

let _signals: AvatarSignals = { ...DEFAULT_SIGNALS };
const _listeners = new Set<Listener>();

export function updateAvatarSignals(patch: Partial<AvatarSignals>): void {
  _signals = { ..._signals, ...patch };
  for (const listener of _listeners) listener(_signals);
}

export function currentAvatarSignals(): AvatarSignals {
  return _signals;
}

export function onAvatarSignals(listener: Listener): () => void {
  _listeners.add(listener);
  listener(_signals);
  return () => _listeners.delete(listener);
}

export function resetAvatarSignals(): void {
  _signals = { ...DEFAULT_SIGNALS };
  for (const listener of _listeners) listener(_signals);
}

// ---------------------------------------------------------------------------
// Ordinary accounts: a deterministic identity color, so every account's
// picture is stable and its own without any uploads yet.
// ---------------------------------------------------------------------------
export function identityHue(name: string): number {
  let hash = 0;
  for (let i = 0; i < name.length; i++) {
    hash = (hash * 31 + name.charCodeAt(i)) >>> 0;
  }
  return hash % 360;
}
