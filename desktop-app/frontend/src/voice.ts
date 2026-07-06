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

// One voice at a time: a new reply always interrupts the previous one —
// an assistant that talks over itself is worse than a silent one.
export function speak(text: string): void {
  if (!speechOutputSupported() || !text.trim()) return;
  const w = window as unknown as {
    speechSynthesis: SpeechSynthesis;
    SpeechSynthesisUtterance?: typeof SpeechSynthesisUtterance;
  };
  if (!w.SpeechSynthesisUtterance) return;
  w.speechSynthesis.cancel();
  const utterance = new w.SpeechSynthesisUtterance(text);
  utterance.rate = 1.05;
  w.speechSynthesis.speak(utterance);
}
