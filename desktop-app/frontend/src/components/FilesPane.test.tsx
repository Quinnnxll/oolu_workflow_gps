import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { FilesPane } from "./FilesPane";

let routes: Record<string, { status: number; body: unknown }>;
let calls: { method: string; path: string; query: string; body: unknown }[];

const fetchMock = vi.fn(async (input: string | URL, init?: RequestInit) => {
  const u = new URL(String(input), "http://local.test");
  const method = init?.method ?? "GET";
  const body = init?.body ? JSON.parse(String(init.body)) : undefined;
  calls.push({ method, path: u.pathname, query: u.search.slice(1), body });
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

const FILES = {
  items: [
    {
      file_id: "f1",
      node_id: null,
      name: "notes.md",
      media_type: "text/markdown",
      size: 200,
      created_at: "t",
      updated_at: "t",
    },
  ],
};

describe("FilesPane", () => {
  it("shows the drawer as tiles and opens a file with a way back", async () => {
    routes["GET /v1/files"] = { status: 200, body: FILES };
    routes["GET /v1/files/f1"] = {
      status: 200,
      body: { ...FILES.items[0], content: "hello" },
    };
    render(<FilesPane />);

    fireEvent.click(await screen.findByText("notes.md"));
    expect(await screen.findByText("hello")).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "← files" }));
    expect(await screen.findByText("notes.md")).toBeTruthy();
  });

  it("scopes a node's drawer with the node_id query", async () => {
    routes["GET /v1/files"] = { status: 200, body: { items: [] } };
    routes["POST /v1/files"] = {
      status: 201,
      body: { ...FILES.items[0], file_id: "f9", node_id: "n1" },
    };
    routes["GET /v1/files/f9"] = {
      status: 200,
      body: { ...FILES.items[0], file_id: "f9", node_id: "n1", content: "" },
    };
    render(<FilesPane nodeId="n1" />);

    expect(await screen.findByText(/keeps its files to itself/)).toBeTruthy();
    expect(calls[0].query).toBe("node_id=n1");

    fireEvent.click(screen.getByRole("button", { name: "New document" }));
    await screen.findByLabelText("File name");
    const create = calls.find((c) => c.method === "POST");
    expect((create?.body as { node_id: string }).node_id).toBe("n1");
  });
});
