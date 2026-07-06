import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
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

  it("dictation sends the final transcript; no mic without an engine", async () => {
    // jsdom default: no engine, no button.
    const { unmount } = render(<Chat />);
    expect(screen.queryByLabelText("Speak to OoLu")).toBeNull();
    unmount();

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
    try {
      render(<Chat />);
      fireEvent.click(screen.getByLabelText("Speak to OoLu"));
      const rec = FakeRecognition.last!;
      expect(rec.start).toHaveBeenCalled();

      rec.onresult!({
        resultIndex: 0,
        results: [{ isFinal: true, 0: { transcript: "email bob the numbers" } }],
      });

      expect(await screen.findByText("On it.")).toBeTruthy();
      const chat = calls.find((c) => c.path === "/v1/chat");
      expect((chat?.body as { message: string }).message).toBe(
        "email bob the numbers",
      );
    } finally {
      delete (window as unknown as Record<string, unknown>).webkitSpeechRecognition;
    }
  });

  it("speaks replies aloud when the voice toggle is on", async () => {
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

    fireEvent.click(screen.getByLabelText("Speak replies aloud"));
    fireEvent.change(screen.getByPlaceholderText("Message OoLu…"), {
      target: { value: "thanks" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Send" }));

    await screen.findByText("Anytime.");
    expect(
      (speakSpy.mock.calls[0][0] as { text: string }).text,
    ).toBe("Anytime.");
    expect(localStorage.getItem("oolu_voice_out")).toBe("on");
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
    expect(chat?.body).toEqual({ message: "thanks", history: [] });
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
});
