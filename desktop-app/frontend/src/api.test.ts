import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  api,
  captureEngineToken,
  confirmReset,
  login,
  register,
  requestReset,
  requiresLogin,
  session,
  signOut,
  verifyEmail,
} from "./api";

// A recorded fetch call, so tests can assert the exact route + payload the
// adapter sent to the gateway.
interface Call {
  method: string;
  url: string;
  path: string;
  query: string;
  body: unknown;
  headers: Record<string, string>;
  protocols?: string[];
}

let calls: Call[] = [];
let routes: Record<string, { status: number; body: unknown }> = {};

function reply(status: number, body: unknown) {
  return {
    ok: status >= 200 && status < 300,
    status,
    text: async () => (body === undefined ? "" : JSON.stringify(body)),
    json: async () => body,
  } as Response;
}

// Match a queued response by "METHOD /path" (query stripped).
function resolve(method: string, path: string) {
  const key = `${method} ${path}`;
  return routes[key] ?? { status: 200, body: {} };
}

const fetchMock = vi.fn(
  async (input: string | URL | Request, init?: RequestInit) => {
    const raw = String(input);
    const u = new URL(raw);
    const path = u.origin + u.pathname;
    const query = u.search.slice(1);
    const method = init?.method ?? "GET";
    const headers = (init?.headers as Record<string, string>) ?? {};
    const body = init?.body ? JSON.parse(String(init.body)) : undefined;
    calls.push({ method, url: raw, path, query, body, headers });
    // Route table is keyed by pathname (base-agnostic), e.g. "POST /v1/runs".
    const { status, body: resBody } = resolve(method, u.pathname);
    return reply(status, resBody);
  },
);

function lastCall(): Call {
  return calls[calls.length - 1];
}

beforeEach(() => {
  calls = [];
  routes = {};
  localStorage.clear();
  sessionStorage.clear();
  vi.stubGlobal("fetch", fetchMock);
  // Remote mode is the interesting case for auth; individual tests opt out.
  window.__OOLU_API__ = "https://host.example";
  window.__OOLU_REMOTE__ = true;
});

afterEach(() => {
  vi.unstubAllGlobals();
  delete window.__OOLU_API__;
  delete window.__OOLU_REMOTE__;
});

describe("run routes", () => {
  it("submits a run to POST /v1/runs and composes a cancellable view", async () => {
    routes["POST /v1/runs"] = {
      status: 200,
      body: {
        run_id: "r1",
        intent: "do it",
        phase: "running",
        awaiting: null,
        prompt: null,
        failure_reason: null,
        result: null,
      },
    };

    const task = await api.submitTask("do it");

    expect(lastCall().path).toBe("https://host.example/v1/runs");
    expect(lastCall().method).toBe("POST");
    expect(lastCall().body).toEqual({ intent: "do it" });
    expect(task.run_id).toBe("r1");
    // can_cancel is derived from a non-terminal phase, not sent on the wire.
    expect(task.can_cancel).toBe(true);
    expect(task.questions).toEqual([]);
  });

  it("marks terminal runs as not cancellable", async () => {
    routes["GET /v1/runs/r1"] = {
      status: 200,
      body: { run_id: "r1", phase: "completed", intent: "x", awaiting: null },
    };
    const task = await api.task("r1");
    expect(task.can_cancel).toBe(false);
  });

  it("fetches /questions when a run awaits clarification", async () => {
    routes["GET /v1/runs/r1"] = {
      status: 200,
      body: { run_id: "r1", phase: "paused", intent: "x", awaiting: "clarification" },
    };
    routes["GET /v1/runs/r1/questions"] = {
      status: 200,
      body: {
        questions: [
          { parameter: "city", question: "Which city?", suggested_values: ["NYC"] },
        ],
      },
    };

    const task = await api.task("r1");

    expect(task.questions).toHaveLength(1);
    expect(task.questions[0].parameter).toBe("city");
  });

  it("backfills the confirmation prompt from /route", async () => {
    routes["GET /v1/runs/r1"] = {
      status: 200,
      body: {
        run_id: "r1",
        phase: "paused",
        intent: "x",
        awaiting: "confirmation",
        prompt: null,
      },
    };
    routes["GET /v1/runs/r1/route"] = {
      status: 200,
      body: { chosen: "fast", total_cost: 10 },
    };

    const task = await api.task("r1");
    expect(task.prompt).toContain("fast");
  });

  it("backfills the incident prompt from /incidents", async () => {
    routes["GET /v1/runs/r1"] = {
      status: 200,
      body: {
        run_id: "r1",
        phase: "paused",
        intent: "x",
        awaiting: "incident",
        prompt: null,
      },
    };
    routes["GET /v1/runs/r1/incidents"] = {
      status: 200,
      body: { incidents: [{ reason: "quota exceeded" }] },
    };

    const task = await api.task("r1");
    expect(task.prompt).toBe("quota exceeded");
  });

  it("routes answer/confirm/incident/cancel to the run subresources", async () => {
    const run = {
      run_id: "r1",
      phase: "running",
      intent: "x",
      awaiting: null,
      prompt: null,
    };
    routes["POST /v1/runs/r1/answers"] = { status: 200, body: run };
    routes["POST /v1/runs/r1/confirmation"] = { status: 200, body: run };
    routes["POST /v1/runs/r1/incidents"] = { status: 200, body: run };
    routes["POST /v1/runs/r1/cancel"] = { status: 200, body: run };

    await api.answer("r1", { city: "NYC" });
    expect(lastCall().path).toBe("https://host.example/v1/runs/r1/answers");
    expect(lastCall().body).toEqual({ answers: { city: "NYC" } });

    await api.confirm("r1", true);
    expect(lastCall().path).toBe("https://host.example/v1/runs/r1/confirmation");
    expect(lastCall().body).toEqual({ approved: true });

    await api.resolveIncident("r1", "retry");
    expect(lastCall().path).toBe("https://host.example/v1/runs/r1/incidents");
    expect(lastCall().body).toEqual({ decision: "retry" });

    await api.cancel("r1");
    expect(lastCall().path).toBe("https://host.example/v1/runs/r1/cancel");
  });
});

describe("timeline", () => {
  it("maps the audit log to timeline events", async () => {
    routes["GET /v1/runs/r1/audit"] = {
      status: 200,
      body: {
        entries: [
          { seq: 1, event_type: "workflow.started", at: "2026-01-01T00:00:00Z" },
          { seq: 2, event_type: "workflow.completed", at: "2026-01-01T00:01:00Z" },
        ],
      },
    };

    const { items } = await api.timeline("r1");
    expect(items).toHaveLength(2);
    expect(items[0]).toEqual({
      at: "2026-01-01T00:00:00Z",
      label: "workflow.started",
      detail: "",
    });
  });
});

describe("inbox (derived from run list)", () => {
  it("lists GET /v1/runs and keeps only runs awaiting a human", async () => {
    routes["GET /v1/runs"] = {
      status: 200,
      body: {
        items: [
          { run_id: "a", intent: "one", phase: "paused", awaiting: "clarification", prompt: null },
          { run_id: "b", intent: "two", phase: "running", awaiting: null, prompt: null },
          { run_id: "c", intent: "three", phase: "paused", awaiting: "confirmation", prompt: "ok?" },
        ],
      },
    };

    const { items } = await api.inbox();

    expect(lastCall().path).toBe("https://host.example/v1/runs");
    expect(items.map((i) => i.run_id)).toEqual(["a", "c"]);
    expect(items[0].kind).toBe("clarification");
    expect(items[1].prompt).toBe("ok?");
  });
});

describe("skills (marketplace listings)", () => {
  it("queries GET /v1/listings", async () => {
    routes["GET /v1/listings"] = {
      status: 200,
      body: { items: [{ listing_id: "l1", title: "Node", summary: "s", status: "active", tags: [] }] },
    };

    const { items } = await api.skills("node");
    expect(lastCall().path).toBe("https://host.example/v1/listings");
    expect(lastCall().query).toContain("q=node");
    expect(items[0].title).toBe("Node");
  });
});

describe("auth", () => {
  it("attaches a bearer token once signed in", async () => {
    session.set("tok123", "alice", "acme");
    routes["GET /v1/runs/r1"] = {
      status: 200,
      body: { run_id: "r1", phase: "running", intent: "x", awaiting: null },
    };

    await api.task("r1");
    expect(lastCall().headers["Authorization"]).toBe("Bearer tok123");
  });

  it("sends no Authorization header when signed out", async () => {
    routes["GET /v1/runs/r1"] = {
      status: 200,
      body: { run_id: "r1", phase: "running", intent: "x", awaiting: null },
    };
    await api.task("r1");
    expect(lastCall().headers["Authorization"]).toBeUndefined();
  });

  it("signs out and reloads on a 401 while holding a token", async () => {
    session.set("stale", "alice", "acme");
    const reload = vi.fn();
    vi.stubGlobal("location", { ...window.location, reload });
    routes["GET /v1/runs/r1"] = { status: 401, body: { error: { message: "expired" } } };

    await expect(api.task("r1")).rejects.toThrow();
    expect(session.signedIn()).toBe(false);
    expect(reload).toHaveBeenCalled();
  });

  it("login stores the session and posts credentials", async () => {
    routes["POST /v1/auth/login"] = {
      status: 200,
      body: { token: "t", principal: "alice", tenant: "acme" },
    };

    await login("alice", "pw");

    expect(lastCall().path).toBe("https://host.example/v1/auth/login");
    expect(lastCall().body).toEqual({ username: "alice", password: "pw" });
    expect(session.token).toBe("t");
    expect(session.principal).toBe("alice");
    expect(requiresLogin()).toBe(false);
  });

  it("login throws the server message on failure", async () => {
    routes["POST /v1/auth/login"] = {
      status: 401,
      body: { error: { message: "bad credentials" } },
    };
    await expect(login("alice", "nope")).rejects.toThrow("bad credentials");
    expect(session.signedIn()).toBe(false);
  });

  it("requiresLogin is false in local (non-remote) mode", () => {
    window.__OOLU_REMOTE__ = false;
    expect(requiresLogin()).toBe(false);
  });

  it("runs lists node interactions newest first", async () => {
    routes["GET /v1/runs"] = {
      status: 200,
      body: {
        items: [
          { run_id: "r1", intent: "older", phase: "completed", awaiting: null },
          { run_id: "r2", intent: "newer", phase: "executing", awaiting: null },
        ],
      },
    };
    const { items } = await api.runs();
    expect(items.map((r) => r.run_id)).toEqual(["r2", "r1"]);
    expect(items[0]).toEqual({
      run_id: "r2",
      intent: "newer",
      phase: "executing",
      awaiting: null,
    });
  });

  it("chat posts the message with history and returns the turn", async () => {
    routes["POST /v1/chat"] = {
      status: 200,
      body: { reply: "On it.", source: "intent", run_id: "r9" },
    };
    const turn = await api.chat("email bob", [
      { role: "user", content: "hi" },
      { role: "assistant", content: "Hi!" },
    ]);
    expect(lastCall().path).toBe("https://host.example/v1/chat");
    expect(lastCall().body).toEqual({
      message: "email bob",
      // The user's clock rides every turn so time-shaped asks resolve
      // in THEIR timezone.
      tz_offset_minutes: -new Date().getTimezoneOffset() || 0,
      history: [
        { role: "user", content: "hi" },
        { role: "assistant", content: "Hi!" },
      ],
    });
    expect(turn.run_id).toBe("r9");
  });

  it("register posts to the online server and stores the session", async () => {
    routes["POST /v1/auth/register"] = {
      status: 200,
      body: { token: "t2", principal: "bob@example.com", tenant: "acme" },
    };

    await register("bob@example.com", "pw");

    expect(lastCall().path).toBe("https://host.example/v1/auth/register");
    expect(lastCall().body).toEqual({ email: "bob@example.com", password: "pw" });
    expect(session.token).toBe("t2");
    expect(session.principal).toBe("bob@example.com");
  });

  it("register maps a 404 to a friendly 'not offered' message", async () => {
    routes["POST /v1/auth/register"] = { status: 404, body: {} };
    await expect(register("bob@example.com", "pw")).rejects.toThrow(
      "does not offer registration",
    );
    expect(session.signedIn()).toBe(false);
  });

  it("register on a verifying host reports the code step, no session", async () => {
    routes["POST /v1/auth/register"] = {
      status: 201,
      body: { verification_required: true, email: "bob@example.com" },
    };

    const result = await register("bob@example.com", "pw");

    expect(result.verificationRequired).toBe(true);
    expect(session.signedIn()).toBe(false);
  });

  it("verifyEmail posts the code and stores the session", async () => {
    routes["POST /v1/auth/verify"] = {
      status: 200,
      body: { token: "t9", principal: "bob", tenant: "acme" },
    };

    await verifyEmail("bob@example.com", "123456", "pw");

    expect(lastCall().path).toBe("https://host.example/v1/auth/verify");
    expect(lastCall().body).toEqual({
      email: "bob@example.com",
      code: "123456",
      password: "pw",
    });
    expect(session.token).toBe("t9");
    expect(session.principal).toBe("bob");
  });

  it("verifyEmail surfaces a wrong code as the server's message", async () => {
    routes["POST /v1/auth/verify"] = {
      status: 400,
      body: { error: { message: "that code is wrong or expired" } },
    };
    await expect(verifyEmail("bob@example.com", "000000", "pw")).rejects.toThrow(
      "wrong or expired",
    );
    expect(session.signedIn()).toBe(false);
  });

  it("reset request and confirm ride the auth endpoints", async () => {
    routes["POST /v1/auth/reset/request"] = {
      status: 202,
      body: { status: "sent" },
    };
    await requestReset("bob@example.com");
    expect(lastCall().path).toBe("https://host.example/v1/auth/reset/request");
    expect(lastCall().body).toEqual({ email: "bob@example.com" });

    routes["POST /v1/auth/reset/confirm"] = {
      status: 200,
      body: { status: "password_changed" },
    };
    await confirmReset("bob@example.com", "123456", "new-password");
    expect(lastCall().path).toBe("https://host.example/v1/auth/reset/confirm");
    expect(lastCall().body).toEqual({
      email: "bob@example.com",
      code: "123456",
      password: "new-password",
    });
    // Neither call mints a session — sign-in follows separately.
    expect(session.signedIn()).toBe(false);
  });

  it("reset maps a 404 to a friendly 'not offered' message", async () => {
    routes["POST /v1/auth/reset/request"] = { status: 404, body: {} };
    await expect(requestReset("bob@example.com")).rejects.toThrow(
      "does not offer password reset",
    );
  });

  it("local mode signs into the server the user names and remembers it", async () => {
    window.__OOLU_REMOTE__ = false;
    routes["POST /v1/auth/login"] = {
      status: 200,
      body: { token: "t3", principal: "alice" },
    };

    await login("alice", "pw", "https://online.oolu.example/");

    expect(lastCall().path).toBe("https://online.oolu.example/v1/auth/login");
    expect(session.server).toBe("https://online.oolu.example");
    expect(session.token).toBe("t3");
  });

  it("local mode refuses auth calls without a server", async () => {
    window.__OOLU_REMOTE__ = false;
    await expect(login("alice", "pw")).rejects.toThrow("enter the server");
    expect(calls.length).toBe(0);
  });

  it("captures the engine token from the URL hash and strips it", () => {
    window.__OOLU_REMOTE__ = false;
    window.location.hash = "#auth=eng123";
    captureEngineToken();
    expect(sessionStorage.getItem("oolu_engine_token")).toBe("eng123");
    expect(window.location.hash).toBe("");
  });

  it("local mode authenticates with the engine token, not the online session", async () => {
    window.__OOLU_REMOTE__ = false;
    window.__OOLU_API__ = "http://127.0.0.1:8765";
    sessionStorage.setItem("oolu_engine_token", "eng123");
    session.set("online-tok", "alice", "acme");
    routes["GET /v1/runs"] = { status: 200, body: { items: [] } };

    await api.inbox();

    expect(lastCall().headers["Authorization"]).toBe("Bearer eng123");
  });

  it("a local 401 drops the engine token but keeps the online account", async () => {
    window.__OOLU_REMOTE__ = false;
    window.__OOLU_API__ = "http://127.0.0.1:8765";
    sessionStorage.setItem("oolu_engine_token", "stale");
    session.set("online-tok", "alice", "acme");
    const reload = vi.fn();
    vi.stubGlobal("location", { ...window.location, reload });
    routes["GET /v1/runs"] = { status: 401, body: {} };

    await expect(api.inbox()).rejects.toThrow("signed out");

    expect(sessionStorage.getItem("oolu_engine_token")).toBeNull();
    expect(session.token).toBe("online-tok");
    expect(reload).toHaveBeenCalled();
  });

  it("signOut keeps the remembered server for the next sign-in", () => {
    session.setServer("https://online.oolu.example");
    session.set("t", "alice", "acme");
    const reload = vi.fn();
    vi.stubGlobal("location", { ...window.location, reload });
    signOut();
    expect(session.signedIn()).toBe(false);
    expect(session.server).toBe("https://online.oolu.example");
  });

  it("signOut clears the stored session", () => {
    session.set("t", "alice", "acme");
    const reload = vi.fn();
    vi.stubGlobal("location", { ...window.location, reload });
    signOut();
    expect(session.signedIn()).toBe(false);
  });

  it("signOut purges every account-content cache on the device", () => {
    // The strict gate: signing out on a shared device leaves no thread
    // and no typed drafts readable behind — whoever wrote them.
    session.set("t", "alice", "acme");
    localStorage.setItem("oolu_chat::alice", "[]");
    localStorage.setItem("oolu_compose_alice::bob", "half-typed words");
    localStorage.setItem("oolu_sidebar_folded", "1"); // a UI pref stays
    const reload = vi.fn();
    vi.stubGlobal("location", { ...window.location, reload });
    signOut();
    expect(localStorage.getItem("oolu_chat::alice")).toBeNull();
    expect(localStorage.getItem("oolu_compose_alice::bob")).toBeNull();
    expect(localStorage.getItem("oolu_sidebar_folded")).toBe("1");
  });
});

describe("non-JSON error bodies", () => {
  // A crashed server (or a proxy error page) answers in prose. The user
  // must read a plain sentence — never "Unexpected token 'I' ... is not
  // valid JSON" leaking from the parser.
  it("a plain-text 500 becomes a plain error", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        ({
          ok: false,
          status: 500,
          text: async () => "Internal Server Error",
        }) as unknown as Response,
      ),
    );
    await expect(api.representative()).rejects.toThrow(
      /server error \(500\)/,
    );
  });

  it("a JSON error body still surfaces its message", async () => {
    routes["GET /v1/representative"] = {
      status: 500,
      body: { error: { code: "internal", message: "the server hit a bug" } },
    };
    await expect(api.representative()).rejects.toThrow(
      /the server hit a bug/,
    );
  });
});
