import { afterEach, describe, expect, it, vi } from "vitest";
import {
  createRecognizer,
  speak,
  speakableText,
  speechInputSupported,
  speechOutputSupported,
  toneForMood,
} from "./voice";

afterEach(() => {
  vi.unstubAllGlobals();
  delete (window as unknown as Record<string, unknown>).webkitSpeechRecognition;
});

class FakeRecognition {
  static last: FakeRecognition | null = null;
  lang = "";
  interimResults = false;
  continuous = false;
  onresult: ((e: unknown) => void) | null = null;
  onend: (() => void) | null = null;
  onerror: (() => void) | null = null;
  started = false;
  constructor() {
    FakeRecognition.last = this;
  }
  start() {
    this.started = true;
  }
  stop() {
    this.started = false;
  }
}

describe("voice", () => {
  it("degrades to nothing where the platform has no engine", () => {
    expect(speechInputSupported()).toBe(false);
    expect(createRecognizer({ onFinal: vi.fn() })).toBeNull();
    // And speaking without an engine is a silent no-op, not a crash.
    expect(() => speak("hello")).not.toThrow();
  });

  it("delivers interim then final transcripts", () => {
    (window as unknown as Record<string, unknown>).webkitSpeechRecognition = FakeRecognition;
    const onFinal = vi.fn();
    const onInterim = vi.fn();
    const rec = createRecognizer({ onFinal, onInterim });
    expect(rec).not.toBeNull();
    rec!.start();

    const fake = FakeRecognition.last!;
    fake.onresult!({
      resultIndex: 0,
      results: [{ isFinal: false, 0: { transcript: "email b" } }],
    });
    expect(onInterim).toHaveBeenCalledWith("email b");

    fake.onresult!({
      resultIndex: 0,
      results: [{ isFinal: true, 0: { transcript: " email bob " } }],
    });
    expect(onFinal).toHaveBeenCalledWith("email bob");
  });

  it("a new utterance always interrupts the previous one", () => {
    const cancel = vi.fn();
    const speakSpy = vi.fn();
    vi.stubGlobal("speechSynthesis", { cancel, speak: speakSpy });
    vi.stubGlobal(
      "SpeechSynthesisUtterance",
      class {
        rate = 1;
        pitch = 1;
        constructor(public text: string) {}
      },
    );
    expect(speechOutputSupported()).toBe(true);

    speak("first");
    speak("second");

    expect(cancel).toHaveBeenCalledTimes(2);
    expect(speakSpy).toHaveBeenCalledTimes(2);
    expect(
      (speakSpy.mock.calls[1][0] as { text: string }).text,
    ).toBe("second");
  });

  it("the spoken delivery is energetic and follows the mood", () => {
    // The default is lively — never the old flat 1.05.
    const base = toneForMood();
    expect(base.rate).toBeGreaterThan(1.05);
    expect(base.pitch).toBeGreaterThan(1);
    // Excited speaks faster and brighter than steady/worried.
    expect(toneForMood("excited").rate).toBeGreaterThan(
      toneForMood("worried").rate,
    );
    expect(toneForMood("excited").pitch).toBeGreaterThan(
      toneForMood("worried").pitch,
    );

    // speak() applies the mood tone to the utterance.
    const speakSpy = vi.fn();
    vi.stubGlobal("speechSynthesis", { cancel: vi.fn(), speak: speakSpy });
    vi.stubGlobal(
      "SpeechSynthesisUtterance",
      class {
        rate = 1;
        pitch = 1;
        constructor(public text: string) {}
      },
    );
    speak("let's go", "excited");
    const utt = speakSpy.mock.calls[0][0] as { rate: number; pitch: number };
    expect(utt.rate).toBe(toneForMood("excited").rate);
    expect(utt.pitch).toBe(toneForMood("excited").pitch);
  });

  it("emoji are for the eye, never the ear", () => {
    // Pictographs, skin tones, flags, keycaps, and ZWJ families all go;
    // words and their punctuation stay exactly as written.
    expect(speakableText("On it! 🚀 I'll ping you.")).toBe(
      "On it! I'll ping you.",
    );
    expect(speakableText("Done ✅✅ — all 3 files converted! 🎉")).toBe(
      "Done — all 3 files converted!",
    );
    expect(speakableText("greetings 👍🏽 from 🇲🇼 the 👨‍👩‍👧 team #️⃣")).toBe(
      "greetings from the team #",
    );
    // A reply that is ONLY emoji is silence, not a description of emoji.
    expect(speakableText("🎉🎉🎉")).toBe("");

    // speak() itself sends the cleaned words to the engine — and never
    // utters an all-emoji reply at all.
    const speakSpy = vi.fn();
    vi.stubGlobal("speechSynthesis", { cancel: vi.fn(), speak: speakSpy });
    vi.stubGlobal(
      "SpeechSynthesisUtterance",
      class {
        constructor(public text: string) {}
      },
    );
    speak("Love it! 🚀 Rolling now.");
    const utt = speakSpy.mock.calls[0][0] as { text: string };
    expect(utt.text).toBe("Love it! Rolling now.");
    speakSpy.mockClear();
    speak("💯🔥");
    expect(speakSpy).not.toHaveBeenCalled();
  });
});
