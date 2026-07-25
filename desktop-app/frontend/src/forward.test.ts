import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  FORWARDED_MARK,
  forwardFile,
  forwardMessage,
  forwardMessageToFile,
  forwardMessageToFriend,
  forwardTargets,
} from "./forward";

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
});

afterEach(() => {
  vi.unstubAllGlobals();
  delete window.__OOLU_API__;
});

describe("forwarding", () => {
  it("delivers to a node's interact window and the node's model answers", async () => {
    routes["POST /v1/chat"] = {
      status: 200,
      body: { reply: "received — I'll fold them in.", actions: [] },
    };
    await forwardMessage("the numbers are ready", {
      kind: "node",
      id: "n1",
      title: "Invoice Cleaner",
    });
    const thread = JSON.parse(
      localStorage.getItem("oolu_node_chat_n1") ?? "[]",
    );
    // The full path: the user's turn AND the model's answer, no note —
    // exactly what the interact composer would have produced.
    expect(thread).toHaveLength(2);
    expect(thread[0]).toMatchObject({
      kind: "user",
      text: "the numbers are ready",
    });
    expect(thread[0].text).not.toContain(FORWARDED_MARK);
    expect(thread[1]).toMatchObject({
      kind: "assistant",
      text: "received — I'll fold them in.",
    });
    const turn = calls.find(
      (c) => c.method === "POST" && c.path === "/v1/chat",
    );
    expect(turn?.body).toMatchObject({
      message: "the numbers are ready",
      node_id: "n1",
    });
  });

  it("delivers to the OoLu conversation under the ACCOUNT-scoped key", async () => {
    routes["POST /v1/chat"] = {
      status: 200,
      body: { reply: "noted." },
    };
    // The key the Chat window actually reads (accountScope() with no
    // session is "local") — seeded with an earlier turn that must both
    // survive and ride the model call as history.
    localStorage.setItem(
      "oolu_chat::local",
      JSON.stringify([{ kind: "assistant", text: "hello" }]),
    );
    await forwardMessage("please review", { kind: "oolu", title: "OoLu" });
    const oolu = JSON.parse(localStorage.getItem("oolu_chat::local") ?? "[]");
    expect(oolu).toHaveLength(3);
    expect(oolu[1]).toMatchObject({ kind: "user", text: "please review" });
    expect(oolu[2]).toMatchObject({ kind: "assistant", text: "noted." });
    const turn = calls.find(
      (c) => c.method === "POST" && c.path === "/v1/chat",
    );
    const body = turn?.body as {
      node_id?: string;
      history: { role: string; content: string }[];
    };
    expect(body.node_id).toBeUndefined();
    expect(body.history).toEqual([{ role: "assistant", content: "hello" }]);
    // The old orphan key stays untouched — nothing writes beside the
    // account-scoped thread the conversation reads.
    expect(localStorage.getItem("oolu_chat")).toBeNull();
  });

  it("saves a forwarded message as a document in the Life drawer", async () => {
    routes["POST /v1/files"] = {
      status: 201,
      body: { file_id: "f1", name: "convert report pdf.md" },
    };
    const name = await forwardMessageToFile("convert the report to pdf");
    expect(name).toBe("convert report pdf.md");
    const create = calls.find((c) => c.method === "POST");
    const body = create?.body as { folder: string; content: string };
    expect(body.folder).toBe("forwarded");
    // The words alone — the "forwarded" folder already says how it
    // arrived; no note is stitched into the user's own document.
    expect(body.content).toBe("convert the report to pdf");
  });

  it("forwards a file as a COPY into the picked drawer", async () => {
    routes["GET /v1/files/f7"] = {
      status: 200,
      body: { file_id: "f7", name: "budget.csv", content: "a,b\n1,2" },
    };
    routes["POST /v1/files"] = {
      status: 201,
      body: { file_id: "f8", name: "budget.csv" },
    };
    const name = await forwardFile("f7", "n1");
    expect(name).toBe("budget.csv");
    const create = calls.find((c) => c.method === "POST");
    const body = create?.body as {
      node_id: string;
      content: string;
      folder: string;
    };
    expect(body.node_id).toBe("n1");
    expect(body.content).toBe("a,b\n1,2");
    expect(body.folder).toBe("forwarded");
  });

  it("offers OoLu, friends, and every node on the desk as destinations", async () => {
    routes["GET /v1/friends"] = {
      status: 200,
      body: {
        items: [
          {
            peer: "bob",
            last_text: "hi",
            last_from: "bob",
            last_at: "2026-07-10T10:00:00Z",
            unread: 0,
          },
        ],
      },
    };
    routes["GET /v1/work/nodes"] = {
      status: 200,
      body: {
        items: [
          { node_id: "n1", title: "Invoice Cleaner" },
          { node_id: "n2", title: "Tax Filer" },
        ],
      },
    };
    const targets = await forwardTargets();
    expect(targets.map((t) => t.title)).toEqual([
      "OoLu",
      "bob",
      "Invoice Cleaner",
      "Tax Filer",
    ]);
    expect(targets[1]).toMatchObject({ kind: "friend", id: "bob" });
  });

  it("delivers a forwarded message to a friend through the server", async () => {
    routes["POST /v1/friends/bob/messages"] = {
      status: 201,
      body: { message_id: "m1" },
    };
    await forwardMessageToFriend("the numbers are ready", "OoLu", "bob");

    const post = calls.find(
      (c) => c.method === "POST" && c.path === "/v1/friends/bob/messages",
    );
    expect(post?.body).toEqual({
      text: `${FORWARDED_MARK} from OoLu:\nthe numbers are ready`,
    });
    // Nothing landed in any local thread — a person is a real delivery.
    expect(localStorage.getItem("oolu_chat")).toBeNull();
  });
});
