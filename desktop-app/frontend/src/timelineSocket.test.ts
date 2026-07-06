import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { session, timelineSocket } from "./api";
import type { TimelineEvent } from "./types";

// Minimal WebSocket stand-in that records the handshake and lets the test
// push frames through the instance the code under test created.
class FakeWebSocket {
  static last: FakeWebSocket | null = null;
  onmessage: ((e: { data: string }) => void) | null = null;
  onopen: (() => void) | null = null;
  close = vi.fn();
  constructor(
    readonly url: string,
    readonly protocols?: string | string[],
  ) {
    FakeWebSocket.last = this;
  }
}

beforeEach(() => {
  localStorage.clear();
  sessionStorage.clear();
  vi.stubGlobal("WebSocket", FakeWebSocket as unknown as typeof WebSocket);
  window.__OOLU_API__ = "https://host.example";
  window.__OOLU_REMOTE__ = true;
});

afterEach(() => {
  vi.unstubAllGlobals();
  delete window.__OOLU_API__;
  delete window.__OOLU_REMOTE__;
});

describe("timelineSocket", () => {
  it("connects to the run events route over wss with a bearer subprotocol", () => {
    session.set("tok123", "alice", "acme");
    timelineSocket("r1", () => {});

    const ws = FakeWebSocket.last!;
    expect(ws.url).toBe("wss://host.example/v1/runs/r1/events");
    expect(ws.protocols).toEqual(["bearer", "tok123"]);
  });

  it("uses the engine token in local mode", () => {
    window.__OOLU_REMOTE__ = false;
    window.__OOLU_API__ = "http://127.0.0.1:8765";
    sessionStorage.setItem("oolu_engine_token", "eng456");
    timelineSocket("r1", () => {});
    expect(FakeWebSocket.last!.protocols).toEqual(["bearer", "eng456"]);
  });

  it("omits the subprotocol when there is no token", () => {
    timelineSocket("r1", () => {});
    expect(FakeWebSocket.last!.protocols).toBeUndefined();
  });

  it("maps an audit frame to a TimelineEvent", () => {
    const received: TimelineEvent[] = [];
    timelineSocket("r1", (e) => received.push(e));

    FakeWebSocket.last!.onmessage!({
      data: JSON.stringify({
        seq: 3,
        event_type: "worker.dispatched",
        phase: "running",
        at: "2026-01-01T00:00:00Z",
      }),
    });

    expect(received).toEqual([
      { at: "2026-01-01T00:00:00Z", label: "worker.dispatched", detail: "running" },
    ]);
  });

  it("ignores malformed frames without throwing", () => {
    const received: TimelineEvent[] = [];
    timelineSocket("r1", (e) => received.push(e));
    expect(() => FakeWebSocket.last!.onmessage!({ data: "not json" })).not.toThrow();
    expect(received).toHaveLength(0);
  });
});
