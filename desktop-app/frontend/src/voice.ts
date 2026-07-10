// The wake word is "OoLu" — just the name, no "hey". The webview layer
// here is press-to-talk only; the native shell's wake-word engine (a
// later Tauri plugin) must listen for exactly this phrase.
export const WAKE_WORD = "OoLu";

// Voice in and out over the Web Speech API, degrading gracefully: where
// the platform has no engine, the UI simply never grows the buttons —
// no dead microphones, no silent failures.

interface SpeechRecognitionLike {
  lang: string;
  interimResults: boolean;
  continuous: boolean;
  onresult: ((event: SpeechResultEventLike) => void) | null;
  onend: (() => void) | null;
  onerror: (() => void) | null;
  start(): void;
  stop(): void;
}

interface SpeechResultEventLike {
  resultIndex: number;
  results: ArrayLike<{ isFinal: boolean; 0: { transcript: string } }>;
}

type RecognitionCtor = new () => SpeechRecognitionLike;

function recognitionCtor(): RecognitionCtor | null {
  const w = window as unknown as {
    SpeechRecognition?: RecognitionCtor;
    webkitSpeechRecognition?: RecognitionCtor;
  };
  return w.SpeechRecognition ?? w.webkitSpeechRecognition ?? null;
}

export function speechInputSupported(): boolean {
  return recognitionCtor() !== null;
}

export interface Recognizer {
  start(): void;
  stop(): void;
}

export function createRecognizer(handlers: {
  onFinal: (text: string) => void;
  onInterim?: (text: string) => void;
  onEnd?: () => void;
  lang?: string;
}): Recognizer | null {
  const Ctor = recognitionCtor();
  if (!Ctor) return null;
  const rec = new Ctor();
  rec.lang = handlers.lang ?? navigator.language ?? "en-US";
  rec.interimResults = true;
  rec.continuous = false;
  rec.onresult = (event) => {
    let interim = "";
    for (let i = event.resultIndex; i < event.results.length; i++) {
      const result = event.results[i];
      if (result.isFinal) {
        const text = result[0].transcript.trim();
        if (text) handlers.onFinal(text);
      } else {
        interim += result[0].transcript;
      }
    }
    if (interim && handlers.onInterim) handlers.onInterim(interim);
  };
  rec.onend = () => handlers.onEnd?.();
  rec.onerror = () => handlers.onEnd?.();
  return rec;
}

export function speechOutputSupported(): boolean {
  return "speechSynthesis" in window;
}

// OoLu's spoken delivery follows its mood — brighter and quicker when
// excited, calmer and lower when steady. The same energetic core, tuned
// to the moment (matching the mood the text itself carries).
export interface SpeechTone {
  rate: number;
  pitch: number;
}

const MOOD_TONE: Record<string, SpeechTone> = {
  excited: { rate: 1.18, pitch: 1.25 },
  happy: { rate: 1.1, pitch: 1.15 },
  thinking: { rate: 1.02, pitch: 1.02 },
  worried: { rate: 1.0, pitch: 0.95 },
  calm: { rate: 1.04, pitch: 1.05 },
};

export function toneForMood(mood?: string): SpeechTone {
  // The energetic default: livelier than the old flat 1.05, never robotic.
  return (mood && MOOD_TONE[mood]) || { rate: 1.1, pitch: 1.12 };
}

// One voice at a time: a new reply always interrupts the previous one —
// an assistant that talks over itself is worse than a silent one.
export function speak(text: string, mood?: string): void {
  if (!speechOutputSupported() || !text.trim()) return;
  const w = window as unknown as {
    speechSynthesis: SpeechSynthesis;
    SpeechSynthesisUtterance?: typeof SpeechSynthesisUtterance;
  };
  if (!w.SpeechSynthesisUtterance) return;
  w.speechSynthesis.cancel();
  const utterance = new w.SpeechSynthesisUtterance(text);
  const tone = toneForMood(mood);
  utterance.rate = tone.rate;
  utterance.pitch = tone.pitch;
  w.speechSynthesis.speak(utterance);
}
