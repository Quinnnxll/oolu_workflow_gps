import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { resetAvatarSignals } from "../avatar";
import { Chat, RunCard } from "./Chat";

// Route table keyed by "METHOD /path"; the chat talks to the same gateway
// contract as everything else, so tests assert the exact wire traffic.
let routes: Record<string, { status: number; body: unknown }>;
let calls: { method: string; path: string; body: unknown }[];

const fetchMock = vi.fn(async (input: string | URL, init?: RequestInit) => {
  const u = new URL(String(input), "http://local.test");
  const method = init?.method ?? "GET";
  const body = init?.body ? JSON.parse(String(init.body)) : undefined;
  calls.push({ method, path: u.pathname, body });
  const hit = routes[`${method} ${u.pathname}`] ?? { status: 200, body: {} };
  return {
    ok: hit.status >= 200 && hit.status < 300,
    status: hit.status,
    text: async () => JSON.stringify(hit.body),
    json: async () => hit.body,
  } as Response;
});

beforeEach(() => {
  routes = {};
  calls = [];
  localStorage.clear();
  window.__OOLU_API__ = "http://local.test";
  vi.stubGlobal("fetch", fetchMock);
  fetchMock.mockClear();
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  delete window.__OOLU_API__;
  resetAvatarSignals();
});

function baseRun(overrides: Record<string, unknown> = {}) {
  return {
    run_id: "r1",
    intent: "email bob the numbers",
    phase: "executing",
    awaiting: null,
    prompt: null,
    failure_reason: null,
    result: null,
    ...overrides,
  };
}

describe("Chat", () => {
  it("shows a welcome bubble on an empty thread", () => {
    render(<Chat />);
    expect(screen.getByText(/I'm OoLu/)).toBeTruthy();
  });

  it("hosts the living avatar at the head of the conversation", async () => {
    routes["POST /v1/chat"] = {
      status: 200,
      body: { reply: "It didn't work.", source: "intent", run_id: null },
    };
    const { container } = render(<Chat />);

    // The companion and its presence line live in the chat window.
    const head = container.querySelector(".chat-head")!;
    expect(head.querySelector("svg.oolu-avatar")).toBeTruthy();
    expect(screen.getByText("here with you")).toBeTruthy();

    // A bad turn shows on its face — and in the presence line.
    fireEvent.change(screen.getByPlaceholderText("Message OoLu…"), {
      target: { value: "convert the report" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Send" }));

    expect(await screen.findByText("on it — sorting a problem")).toBeTruthy();
    expect(
      head.querySelector("svg.oolu-avatar")!.getAttribute("data-mood"),
    ).toBe("worried");
  });

  it("quick starts send real commands with one tap", async () => {
    routes["POST /v1/chat"] = {
      status: 200,
      body: { reply: "Your tasks:\n(none)", source: "tool", run_id: null },
    };
    render(<Chat />);

    fireEvent.click(screen.getByRole("button", { name: "My tasks" }));

    await screen.findByText(/Your tasks/);
    const chat = calls.find((c) => c.path === "/v1/chat");
    expect((chat?.body as { message: string }).message).toBe("my tasks");
    // The thread has begun: the quick starts step aside.
    expect(screen.queryByRole("button", { name: "My tasks" })).toBeNull();
  });

  it("has no mic and no mute button — the chat stays clean", () => {
    render(<Chat />);
    expect(screen.queryByLabelText("Speak to OoLu")).toBeNull();
    expect(screen.queryByLabelText("Speak replies aloud")).toBeNull();
    expect(screen.queryByLabelText("Stop speaking replies")).toBeNull();
  });

  it("holding Send starts a voice conversation; the release never double-fires", async () => {
    class FakeRecognition {
      static last: FakeRecognition | null = null;
      lang = "";
      interimResults = false;
      continuous = false;
      onresult: ((e: unknown) => void) | null = null;
      onend: (() => void) | null = null;
      onerror: (() => void) | null = null;
      start = vi.fn();
      stop = vi.fn();
      constructor() {
        FakeRecognition.last = this;
      }
    }
    (window as unknown as Record<string, unknown>).webkitSpeechRecognition = FakeRecognition;
    routes["POST /v1/chat"] = {
      status: 200,
      body: { reply: "On it.", source: "intent", run_id: null },
    };
    routes["GET /v1/runs/r1"] = { status: 200, body: baseRun() };
    vi.useFakeTimers();
    try {
      render(<Chat />);
      const send = screen.getByRole("button", { name: "Send" });

      fireEvent.pointerDown(send);
      act(() => {
        vi.advanceTimersByTime(600); // past the long-press threshold
      });
      const rec = FakeRecognition.last!;
      expect(rec.start).toHaveBeenCalled();

      // The release (and the browser's trailing click) is swallowed —
      // the hold must not also send the empty draft.
      fireEvent.pointerUp(send);
      fireEvent.click(screen.getByRole("button", { name: "Stop listening" }));
      expect(calls.filter((c) => c.path === "/v1/chat")).toEqual([]);

      act(() => {
        rec.onresult!({
          resultIndex: 0,
          results: [
            { isFinal: true, 0: { transcript: "email bob the numbers" } },
          ],
        });
      });
      vi.useRealTimers();

      expect(await screen.findByText("On it.")).toBeTruthy();
      const chat = calls.find((c) => c.path === "/v1/chat");
      expect((chat?.body as { message: string }).message).toBe(
        "email bob the numbers",
      );
    } finally {
      vi.useRealTimers();
      delete (window as unknown as Record<string, unknown>).webkitSpeechRecognition;
    }
  });

  it("speaks replies aloud by default — no toggle involved", async () => {
    const speakSpy = vi.fn();
    vi.stubGlobal("speechSynthesis", { cancel: vi.fn(), speak: speakSpy });
    vi.stubGlobal(
      "SpeechSynthesisUtterance",
      class {
        rate = 1;
        constructor(public text: string) {}
      },
    );
    routes["POST /v1/chat"] = {
      status: 200,
      body: { reply: "Anytime.", source: "rule", run_id: null },
    };
    render(<Chat />);

    fireEvent.change(screen.getByPlaceholderText("Message OoLu…"), {
      target: { value: "thanks" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Send" }));

    await screen.findByText("Anytime.");
    expect(
      (speakSpy.mock.calls[0][0] as { text: string }).text,
    ).toBe("Anytime.");
  });

  it("goes silent when Settings says so (app.voice_replies off)", async () => {
    const speakSpy = vi.fn();
    vi.stubGlobal("speechSynthesis", { cancel: vi.fn(), speak: speakSpy });
    vi.stubGlobal(
      "SpeechSynthesisUtterance",
      class {
        constructor(public text: string) {}
      },
    );
    routes["GET /v1/settings"] = {
      status: 200,
      body: {
        items: [
          {
            key: "app.voice_replies",
            group: "app",
            label: "Speak replies aloud",
            kind: "bool",
            description: "",
            value: false,
          },
        ],
      },
    };
    routes["POST /v1/chat"] = {
      status: 200,
      body: { reply: "Anytime.", source: "rule", run_id: null },
    };
    render(<Chat />);
    await waitFor(() =>
      expect(calls.some((c) => c.path === "/v1/settings")).toBe(true),
    );

    fireEvent.change(screen.getByPlaceholderText("Message OoLu…"), {
      target: { value: "thanks" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Send" }));

    await screen.findByText("Anytime.");
    expect(speakSpy).not.toHaveBeenCalled();
  });

  it("sends a message with history and renders both sides", async () => {
    routes["POST /v1/chat"] = {
      status: 200,
      body: { reply: "Anytime.", source: "rule", run_id: null },
    };
    render(<Chat />);

    fireEvent.change(screen.getByPlaceholderText("Message OoLu…"), {
      target: { value: "thanks" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Send" }));

    expect(await screen.findByText("Anytime.")).toBeTruthy();
    expect(screen.getByText("thanks")).toBeTruthy();
    const chat = calls.find((c) => c.path === "/v1/chat");
    // The turn carries the message, history, and the avatar's mood (so
    // OoLu's voice can follow its face).
    expect(chat?.body).toMatchObject({ message: "thanks", history: [] });
  });

  it("shows what the assistant touched as tool chips", async () => {
    routes["POST /v1/chat"] = {
      status: 200,
      body: {
        reply: "notes.md:\nship on friday",
        source: "tool",
        actions: [{ tool: "read_file", name: "notes.md" }],
        run_id: null,
      },
    };
    render(<Chat />);

    fireEvent.change(screen.getByPlaceholderText("Message OoLu…"), {
      target: { value: "read notes.md" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Send" }));

    expect(await screen.findByText("read notes.md", { selector: ".tool-chip" })).toBeTruthy();
    expect(screen.getByText(/ship on friday/)).toBeTruthy();
  });

  it("folds a work turn into the thread as a live run card", async () => {
    routes["POST /v1/chat"] = {
      status: 200,
      body: { reply: "On it.", source: "intent", run_id: "r1" },
    };
    routes["GET /v1/runs/r1"] = { status: 200, body: baseRun() };
    render(<Chat />);

    fireEvent.change(screen.getByPlaceholderText("Message OoLu…"), {
      target: { value: "email bob the numbers" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Send" }));

    expect(await screen.findByText("On it.")).toBeTruthy();
    expect(await screen.findByText("working…")).toBeTruthy();
    expect(
      calls.some((c) => c.method === "GET" && c.path === "/v1/runs/r1"),
    ).toBe(true);
  });

  it("persists the thread across remounts", async () => {
    routes["POST /v1/chat"] = {
      status: 200,
      body: { reply: "Anytime.", source: "rule", run_id: null },
    };
    const first = render(<Chat />);
    fireEvent.change(screen.getByPlaceholderText("Message OoLu…"), {
      target: { value: "thanks" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Send" }));
    await screen.findByText("Anytime.");
    first.unmount();

    render(<Chat />);
    expect(screen.getByText("Anytime.")).toBeTruthy();
  });

  it("loads the account's thread from the server — one thread across devices", async () => {
    // Another device talked earlier; this device's cache has older words.
    localStorage.setItem(
      "oolu_chat::local",
      JSON.stringify([{ kind: "assistant", text: "stale cache line" }]),
    );
    routes["GET /v1/chat/history"] = {
      status: 200,
      body: {
        items: [
          { seq: 1, kind: "user", body: "hello from my laptop", at: "t1" },
          { seq: 2, kind: "assistant", body: "Hey! On it.", at: "t2" },
        ],
      },
    };
    render(<Chat />);

    expect(await screen.findByText("hello from my laptop")).toBeTruthy();
    expect(screen.getByText("Hey! On it.")).toBeTruthy();
    // The server's thread replaced the stale cache, not merged into it.
    expect(screen.queryByText("stale cache line")).toBeNull();
  });

  it("keeps the local thread on hosts that keep no history", async () => {
    localStorage.setItem(
      "oolu_chat::local",
      JSON.stringify([{ kind: "assistant", text: "the local story" }]),
    );
    routes["GET /v1/chat/history"] = { status: 404, body: {} };
    render(<Chat />);

    expect(await screen.findByText("the local story")).toBeTruthy();
  });

  it("walks a newcomer to a first task, exactly once", async () => {
    const first = render(<Chat />);
    expect(screen.getByText(/First time here/)).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "Try a first task" }));
    // The task lands in the box ready to send — nothing fires unseen.
    const box = screen.getByPlaceholderText(
      "Message OoLu…",
    ) as HTMLTextAreaElement;
    expect(box.value).toContain("fetch https://example.com");

    // Used once, gone forever — even on a fresh empty thread.
    first.unmount();
    localStorage.removeItem("oolu_chat::local");
    render(<Chat />);
    expect(screen.queryByText(/First time here/)).toBeNull();
  });

  it("reminds an idle user of pending work — once, never again and again", async () => {
    vi.useFakeTimers();
    try {
      routes["GET /v1/runs"] = {
        status: 200,
        body: {
          items: [
            baseRun({ awaiting: "confirmation", phase: "confirmation" }),
          ],
        },
      };
      render(<Chat />);

      // Two idle minutes pass; the next 30s check drops the reminder in.
      await act(async () => {
        await vi.advanceTimersByTimeAsync(2 * 60_000 + 30_000);
      });
      expect(screen.getByText("reminder")).toBeTruthy();
      // Concise keyword name, never the whole sentence.
      expect(
        screen.getByText(/“Email Bob Numbers” \(needs a decision\)/),
      ).toBeTruthy();
      // The bubble sits at the bottom of the thread — repeating it while
      // the user is still away is a nag storm, not presence. Five minutes
      // on: still exactly one reminder.
      await act(async () => {
        await vi.advanceTimersByTimeAsync(5 * 60_000);
      });
      expect(screen.getAllByText("reminder")).toHaveLength(1);
      // And past the dormancy line the loop is asleep entirely.
      await act(async () => {
        await vi.advanceTimersByTimeAsync(13 * 60_000);
      });
      expect(screen.getAllByText("reminder")).toHaveLength(1);
    } finally {
      vi.useRealTimers();
    }
  }, 20_000);

  it("the user's return after a long absence earns one fresh reminder", async () => {
    vi.useFakeTimers();
    try {
      routes["GET /v1/runs"] = {
        status: 200,
        body: {
          items: [
            baseRun({ awaiting: "confirmation", phase: "confirmation" }),
          ],
        },
      };
      routes["POST /v1/chat"] = {
        status: 200,
        body: { reply: "Welcome back!", source: "model", run_id: null },
      };
      render(<Chat />);

      // The stretch's one reminder posts, then the loop goes dormant
      // while the user stays away past the fifteen-minute line.
      await act(async () => {
        await vi.advanceTimersByTimeAsync(16 * 60_000);
      });
      expect(screen.getAllByText("reminder")).toHaveLength(1);

      // The user comes back and speaks: the reply lands first, then ONE
      // fresh look at the still-open work follows it.
      fireEvent.change(screen.getByPlaceholderText("Message OoLu…"), {
        target: { value: "hey, I'm back" },
      });
      await act(async () => {
        fireEvent.click(screen.getByRole("button", { name: "Send" }));
        await vi.advanceTimersByTimeAsync(1_000);
      });
      expect(screen.getByText("Welcome back!")).toBeTruthy();
      expect(screen.getAllByText("reminder")).toHaveLength(2);
    } finally {
      vi.useRealTimers();
    }
  }, 20_000);

  it("a reminder's arrow points straight back to the action window", async () => {
    routes["GET /v1/runs"] = {
      status: 200,
      body: {
        items: [baseRun({ awaiting: "incident", phase: "recovery" })],
      },
    };
    routes["GET /v1/runs/r1"] = {
      status: 200,
      body: baseRun({
        awaiting: "incident",
        phase: "recovery",
        prompt: "node X failed — retry?",
      }),
    };
    vi.useFakeTimers();
    render(<Chat />);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(2 * 60_000 + 30_000);
    });
    // The snagged task rides the reminder as an arrow. Leave the fake
    // clock BEFORE awaiting anything — a timed-out await under fake
    // timers would leak them into every later test.
    const arrow = screen.getByRole("button", { name: "↦ Email Bob Numbers" });
    vi.useRealTimers();

    fireEvent.click(arrow);
    // The click brings the ACTION WINDOW into the thread: the run card
    // with the live Retry, not just words about it.
    expect(await screen.findByRole("button", { name: "Retry" })).toBeTruthy();
  });

  it("OoLu requests the location; the user's grant runs and sends it", async () => {
    routes["POST /v1/chat"] = {
      status: 200,
      body: {
        reply: "I need your location for that — may I?",
        source: "model",
        run_id: null,
        device: "location",
      },
    };
    vi.stubGlobal("navigator", {
      geolocation: {
        watchPosition: (
          ok: (p: {
            coords: { latitude: number; longitude: number; accuracy: number };
          }) => void,
        ) => {
          ok({ coords: { latitude: 52.52, longitude: 13.405, accuracy: 9 } });
          return 7;
        },
        clearWatch: () => undefined,
      },
    });
    render(<Chat />);

    // The + menu is gone: senses are OoLu's to request, never a button
    // to remember.
    expect(screen.queryByRole("button", { name: "Use this device" })).toBeNull();

    fireEvent.change(screen.getByPlaceholderText("Message OoLu…"), {
      target: { value: "how far is the office?" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Send" }));

    // The request lands as a grant button — nothing was read yet.
    const grant = await screen.findByRole("button", {
      name: "Share my location",
    });
    expect(calls.filter((c) => c.path === "/v1/chat")).toHaveLength(1);

    fireEvent.click(grant);
    await waitFor(() => {
      const chats = calls.filter((c) => c.path === "/v1/chat");
      expect(chats).toHaveLength(2);
      expect((chats[1].body as { message: string }).message).toContain(
        "my location right now: 52.520000, 13.405000 (±9 m)",
      );
    });
    // The settled request keeps a record, not live buttons.
    expect(screen.getByText("location request settled")).toBeTruthy();
  });

  it("declining a device request reads nothing and sends nothing", async () => {
    routes["POST /v1/chat"] = {
      status: 200,
      body: {
        reply: "May I use the camera?",
        source: "model",
        run_id: null,
        device: "camera",
      },
    };
    render(<Chat />);
    fireEvent.change(screen.getByPlaceholderText("Message OoLu…"), {
      target: { value: "what am I looking at?" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Send" }));

    fireEvent.click(await screen.findByRole("button", { name: "Not now" }));
    expect(screen.getByText("camera request settled")).toBeTruthy();
    // Only the user's own message went out — the decline sent nothing.
    expect(calls.filter((c) => c.path === "/v1/chat")).toHaveLength(1);
  });

  it("a refused location permission lands as words, not a dead button", async () => {
    routes["POST /v1/chat"] = {
      status: 200,
      body: {
        reply: "I need your location for that.",
        source: "model",
        run_id: null,
        device: "location",
      },
    };
    vi.stubGlobal("navigator", {
      geolocation: {
        watchPosition: (
          _ok: unknown,
          fail: (e: { code: number; PERMISSION_DENIED: number }) => void,
        ) => {
          fail({ code: 1, PERMISSION_DENIED: 1 });
          return 7;
        },
        clearWatch: () => undefined,
      },
    });
    render(<Chat />);
    fireEvent.change(screen.getByPlaceholderText("Message OoLu…"), {
      target: { value: "where am I?" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Send" }));

    fireEvent.click(
      await screen.findByRole("button", { name: "Share my location" }),
    );
    expect(
      await screen.findByText(/location permission was refused/),
    ).toBeTruthy();
    // Only the user's own message reached the assistant.
    expect(calls.filter((c) => c.path === "/v1/chat")).toHaveLength(1);
  });

  it("turns a transport failure into an apology, not a crash", async () => {
    routes["POST /v1/chat"] = {
      status: 500,
      body: { error: { message: "boom" } },
    };
    render(<Chat />);
    fireEvent.change(screen.getByPlaceholderText("Message OoLu…"), {
      target: { value: "do a thing" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Send" }));

    expect(await screen.findByText(/didn't go through/)).toBeTruthy();
  });
});

describe("RunCard", () => {
  it("answers a clarification in place", async () => {
    routes["GET /v1/runs/r1"] = {
      status: 200,
      body: baseRun({ awaiting: "clarification" }),
    };
    routes["GET /v1/runs/r1/questions"] = {
      status: 200,
      body: {
        questions: [
          { parameter: "quarter", question: "Which quarter?", suggested_values: [] },
        ],
      },
    };
    routes["POST /v1/runs/r1/answers"] = {
      status: 200,
      body: baseRun({ phase: "completed", result: { sent: true } }),
    };
    render(<RunCard runId="r1" />);

    expect(await screen.findByText("Which quarter?")).toBeTruthy();
  });

  it("shows the result when the run completes", async () => {
    routes["GET /v1/runs/r1"] = {
      status: 200,
      body: baseRun({ phase: "completed", result: { sent: true } }),
    };
    render(<RunCard runId="r1" />);

    expect(await screen.findByText("done")).toBeTruthy();
    expect(screen.getByText(/"sent": true/)).toBeTruthy();
  });

  it("reveals the humanized record behind 'what I did'", async () => {
    routes["GET /v1/runs/r1"] = {
      status: 200,
      body: baseRun({ phase: "completed" }),
    };
    routes["GET /v1/runs/r1/audit"] = {
      status: 200,
      body: {
        entries: [
          { at: "2026-07-06T10:00:00Z", event_type: "workflow.started", seq: 1 },
          { at: "2026-07-06T10:00:05Z", event_type: "workflow.completed", seq: 2 },
        ],
      },
    };
    render(<RunCard runId="r1" />);
    await screen.findByText("done");
    expect(screen.getByText(/verified result/)).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "what I did" }));

    expect(await screen.findByText("Started working")).toBeTruthy();
    expect(screen.getByText("Finished the job")).toBeTruthy();
  });

  it("surfaces a failure honestly", async () => {
    routes["GET /v1/runs/r1"] = {
      status: 200,
      body: baseRun({ phase: "failed", failure_reason: "no mail account" }),
    };
    render(<RunCard runId="r1" />);

    expect(await screen.findByText("failed")).toBeTruthy();
    expect(screen.getByText("no mail account")).toBeTruthy();
  });

  it("a dead run offers Run again and the card follows the new attempt", async () => {
    routes["GET /v1/runs/r1"] = {
      status: 200,
      body: baseRun({ phase: "failed", failure_reason: "no viable route" }),
    };
    routes["POST /v1/runs"] = {
      status: 202,
      body: baseRun({ run_id: "r2", phase: "running" }),
    };
    routes["GET /v1/runs/r2"] = {
      status: 200,
      body: baseRun({ run_id: "r2", phase: "completed", result: { ok: 1 } }),
    };
    render(<RunCard runId="r1" />);

    fireEvent.click(await screen.findByRole("button", { name: "Run again" }));
    const resubmit = calls.find(
      (c) => c.method === "POST" && c.path.endsWith("/v1/runs"),
    );
    expect((resubmit?.body as { intent: string }).intent).toBe(
      baseRun({}).intent,
    );
    // A completed run never shows the button — done is done.
    routes["GET /v1/runs/r1"] = {
      status: 200,
      body: baseRun({ phase: "completed" }),
    };
    cleanup();
    render(<RunCard runId="r1" />);
    await screen.findByText("done");
    expect(screen.queryByRole("button", { name: "Run again" })).toBeNull();
  });

  it("a pressed Retry shows it was pressed and posts the decision", async () => {
    routes["GET /v1/runs/r1"] = {
      status: 200,
      body: baseRun({
        phase: "recovery",
        awaiting: "incident",
        prompt: "node X failed — retry?",
        user_retries: 1,
      }),
    };
    routes["POST /v1/runs/r1/incidents"] = {
      status: 200,
      body: baseRun({
        phase: "recovery",
        awaiting: "incident",
        prompt: "node X failed — retry?",
        user_retries: 2,
      }),
    };
    render(<RunCard runId="r1" />);

    // The card says where the retries stand before the press...
    expect(await screen.findByText(/1 retry so far/)).toBeTruthy();
    const retry = screen.getByRole("button", { name: "Retry" });
    expect((retry as HTMLButtonElement).disabled).toBe(false);
    fireEvent.click(retry);

    // ...and the press lands: the decision posted, the count moved on.
    await waitFor(() => {
      const post = calls.find(
        (c) => c.method === "POST" && c.path === "/v1/runs/r1/incidents",
      );
      expect(post?.body).toEqual({ decision: "retry" });
    });
    expect(await screen.findByText(/2 retries so far/)).toBeTruthy();
    expect(screen.getByText(/plan and rebuild the path/)).toBeTruthy();
  });

  it("a refused Retry lands in the card instead of dying silently", async () => {
    routes["GET /v1/runs/r1"] = {
      status: 200,
      body: baseRun({
        phase: "recovery",
        awaiting: "incident",
        prompt: "node X failed — retry?",
      }),
    };
    routes["POST /v1/runs/r1/incidents"] = {
      status: 409,
      body: { error: { message: "this run is no longer paused" } },
    };
    render(<RunCard runId="r1" />);

    fireEvent.click(await screen.findByRole("button", { name: "Retry" }));

    expect(
      await screen.findByText(/no longer paused/),
    ).toBeTruthy();
    // The buttons come back for another try — nothing is wedged.
    expect(
      (screen.getByRole("button", { name: "Retry" }) as HTMLButtonElement)
        .disabled,
    ).toBe(false);
  });
});
