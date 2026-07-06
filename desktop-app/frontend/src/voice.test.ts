import { afterEach, describe, expect, it, vi } from "vitest";
import {
  createRecognizer,
  speak,
  speechInputSupported,
  speechOutputSupported,
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
});
