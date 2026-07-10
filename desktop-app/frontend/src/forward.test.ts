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
  it("drops a marked message into the destination thread's history", () => {
    forwardMessage("the numbers are ready", "OoLu", {
      kind: "node",
      id: "n1",
      title: "Invoice Cleaner",
    });
    const thread = JSON.parse(
      localStorage.getItem("oolu_node_chat_n1") ?? "[]",
    );
    expect(thread).toHaveLength(1);
    expect(thread[0].kind).toBe("user");
    expect(thread[0].text).toContain(FORWARDED_MARK);
    expect(thread[0].text).toContain("from OoLu");
    expect(thread[0].text).toContain("the numbers are ready");

    // ...and into the OoLu conversation, preserving what was there.
    localStorage.setItem(
      "oolu_chat",
      JSON.stringify([{ kind: "assistant", text: "hello" }]),
    );
    forwardMessage("please review", "Invoice Cleaner", {
      kind: "oolu",
      title: "OoLu",
    });
    const oolu = JSON.parse(localStorage.getItem("oolu_chat") ?? "[]");
    expect(oolu).toHaveLength(2);
    expect(oolu[1].text).toContain("from Invoice Cleaner");
  });

  it("saves a forwarded message as a document in the Life drawer", async () => {
    routes["POST /v1/files"] = {
      status: 201,
      body: { file_id: "f1", name: "convert report pdf.md" },
    };
    const name = await forwardMessageToFile(
      "convert the report to pdf",
      "OoLu",
    );
    expect(name).toBe("convert report pdf.md");
    const create = calls.find((c) => c.method === "POST");
    const body = create?.body as { folder: string; content: string };
    expect(body.folder).toBe("forwarded");
    expect(body.content).toContain(FORWARDED_MARK);
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
