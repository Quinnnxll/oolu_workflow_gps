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

  it("organizes the drawer with folders: navigate in, create inside, back out", async () => {
    routes["GET /v1/files"] = {
      status: 200,
      body: {
        items: [
          { ...FILES.items[0] },
          {
            ...FILES.items[0],
            file_id: "f2",
            name: "q3.md",
            folder: "reports/2026",
          },
        ],
      },
    };
    routes["POST /v1/files"] = {
      status: 201,
      body: { ...FILES.items[0], file_id: "f9", folder: "reports", content: "" },
    };
    routes["GET /v1/files/f9"] = {
      status: 200,
      body: { ...FILES.items[0], file_id: "f9", folder: "reports", content: "" },
    };
    render(<FilesPane />);

    // The root shows the top-level folder plus root files — never the
    // nested file itself.
    expect(await screen.findByText("reports")).toBeTruthy();
    expect(screen.getByText("notes.md")).toBeTruthy();
    expect(screen.queryByText("q3.md")).toBeNull();

    // Into the folder: its subfolder appears, with a way back up.
    fireEvent.click(screen.getByText("reports"));
    expect(await screen.findByText("2026")).toBeTruthy();
    expect(screen.getByText("/ reports")).toBeTruthy();
    fireEvent.click(screen.getByText("2026"));
    expect(await screen.findByText("q3.md")).toBeTruthy();

    // Back up one level.
    fireEvent.click(screen.getByText("..").closest("button")!);
    expect(await screen.findByText("2026")).toBeTruthy();

    // A new document lands in the CURRENT folder.
    fireEvent.click(screen.getByRole("button", { name: "New document" }));
    await screen.findByLabelText("File name");
    const create = calls.find((c) => c.method === "POST");
    expect((create?.body as { folder: string }).folder).toBe("reports");
  });

  it("creates an empty folder and keeps it until a file lands", async () => {
    routes["GET /v1/files"] = { status: 200, body: { items: [] } };
    render(<FilesPane />);

    fireEvent.click(await screen.findByRole("button", { name: "New folder" }));
    fireEvent.change(screen.getByLabelText("Folder name"), {
      target: { value: "invoices" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Create" }));

    // The pane navigates straight into the fresh folder.
    expect(screen.getByText("/ invoices")).toBeTruthy();
    expect(
      screen.getByText(/Empty folder — create a document to keep it/),
    ).toBeTruthy();
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
