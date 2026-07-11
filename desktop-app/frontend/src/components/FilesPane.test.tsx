import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { FilesPane } from "./FilesPane";

// The device picker opens native UI — stubbed here; the pane's own
// upload plumbing (drawer writes, notices) is what's under test.
vi.mock("../device", () => ({
  pickLocalFiles: vi.fn(),
  fileToDrawerContent: vi.fn(),
}));
import { fileToDrawerContent, pickLocalFiles } from "../device";

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

const TWO_FILES = {
  items: [
    ...FILES.items,
    {
      file_id: "f2",
      node_id: null,
      name: "budget.csv",
      media_type: "text/csv",
      size: 90,
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

    // An upload lands in the CURRENT folder — documents themselves are
    // OoLu's to write, so the + holds only the human moves.
    expect(screen.queryByRole("button", { name: "New document" })).toBeNull();
    routes["POST /v1/files"] = {
      status: 201,
      body: { ...FILES.items[0], file_id: "f9" },
    };
    vi.mocked(pickLocalFiles).mockResolvedValue([
      new File(["a,b"], "budget.csv", { type: "text/csv" }),
    ]);
    vi.mocked(fileToDrawerContent).mockResolvedValue({
      content: "a,b",
      mediaType: "text/csv",
    });
    fireEvent.click(screen.getByRole("button", { name: "Add" }));
    fireEvent.click(
      screen.getByRole("button", { name: "Upload from device" }),
    );
    await waitFor(() => {
      const create = calls.find((c) => c.method === "POST");
      expect((create?.body as { folder: string }).folder).toBe("reports");
    });
  });

  it("creates an empty folder and keeps it until a file lands", async () => {
    routes["GET /v1/files"] = { status: 200, body: { items: [] } };
    render(<FilesPane />);

    fireEvent.click(await screen.findByRole("button", { name: "Add" }));
    fireEvent.click(screen.getByRole("button", { name: "New folder" }));
    fireEvent.change(screen.getByLabelText("Folder name"), {
      target: { value: "invoices" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Create" }));

    // The pane navigates straight into the fresh folder.
    expect(screen.getByText("/ invoices")).toBeTruthy();
    expect(
      screen.getByText(/Empty folder — drag a file in/),
    ).toBeTruthy();
  });

  it("uploads picked device files into the open folder", async () => {
    routes["GET /v1/files"] = { status: 200, body: FILES };
    routes["POST /v1/files"] = {
      status: 201,
      body: { ...FILES.items[0], file_id: "f9" },
    };
    vi.mocked(pickLocalFiles).mockResolvedValue([
      new File(["a,b"], "budget.csv", { type: "text/csv" }),
    ]);
    vi.mocked(fileToDrawerContent).mockResolvedValue({
      content: "a,b",
      mediaType: "text/csv",
    });
    render(<FilesPane />);

    fireEvent.click(await screen.findByRole("button", { name: "Add" }));
    fireEvent.click(
      screen.getByRole("button", { name: "Upload from device" }),
    );

    await waitFor(() => {
      const post = calls.find(
        (c) => c.method === "POST" && c.path === "/v1/files",
      );
      expect(post?.body).toEqual({
        name: "budget.csv",
        content: "a,b",
        media_type: "text/csv",
      });
    });
    expect(await screen.findByText(/uploaded 1 file/)).toBeTruthy();
  });

  it("a refused upload lands as words next to the successes", async () => {
    routes["GET /v1/files"] = { status: 200, body: FILES };
    vi.mocked(pickLocalFiles).mockResolvedValue([
      new File(["x"], "huge.bin"),
    ]);
    vi.mocked(fileToDrawerContent).mockRejectedValue(
      new Error("huge.bin is too large for the drawer (1 MB cap)"),
    );
    render(<FilesPane />);

    fireEvent.click(await screen.findByRole("button", { name: "Add" }));
    fireEvent.click(
      screen.getByRole("button", { name: "Upload from device" }),
    );

    expect(
      await screen.findByText(/huge.bin is too large/),
    ).toBeTruthy();
    expect(
      calls.some((c) => c.method === "POST" && c.path === "/v1/files"),
    ).toBe(false);
  });

  it("dragging a file onto a folder moves it there", async () => {
    routes["GET /v1/files"] = {
      status: 200,
      body: {
        items: [
          { ...FILES.items[0] }, // notes.md at the top level
          {
            file_id: "f2",
            node_id: null,
            name: "q3.md",
            folder: "reports",
            media_type: "text/markdown",
            size: 10,
            created_at: "t",
            updated_at: "t",
          },
        ],
      },
    };
    routes["PUT /v1/files/f1"] = {
      status: 200,
      body: { ...FILES.items[0], folder: "reports" },
    };
    render(<FilesPane />);

    const tile = (await screen.findByText("notes.md")).closest("button")!;
    const folder = screen.getByText("reports").closest("button")!;
    const dataTransfer = {
      data: {} as Record<string, string>,
      setData(key: string, value: string) {
        this.data[key] = value;
      },
      getData(key: string) {
        return this.data[key] ?? "";
      },
    };
    fireEvent.dragStart(tile, { dataTransfer });
    fireEvent.drop(folder, { dataTransfer });

    await waitFor(() => {
      const move = calls.find(
        (c) => c.method === "PUT" && c.path === "/v1/files/f1",
      );
      expect(move?.body).toEqual({ folder: "reports" });
    });
    expect(await screen.findByText(/moved “notes.md” to reports/)).toBeTruthy();
  });

  it("selects several files and deletes them in one two-tap move", async () => {
    routes["GET /v1/files"] = { status: 200, body: TWO_FILES };
    routes["DELETE /v1/files/f1"] = { status: 200, body: { deleted: true } };
    routes["DELETE /v1/files/f2"] = { status: 200, body: { deleted: true } };
    render(<FilesPane />);

    fireEvent.click(await screen.findByRole("button", { name: "Select" }));
    fireEvent.click(screen.getByText("notes.md"));
    fireEvent.click(screen.getByText("budget.csv"));
    expect(screen.getByText("2 selected")).toBeTruthy();

    // First tap arms, second tap fires — no silent mass delete.
    fireEvent.click(screen.getByRole("button", { name: "Delete…" }));
    fireEvent.click(
      screen.getByRole("button", { name: "Really delete 2?" }),
    );

    await waitFor(() => {
      const deletes = calls.filter((c) => c.method === "DELETE");
      expect(deletes.map((c) => c.path).sort()).toEqual([
        "/v1/files/f1",
        "/v1/files/f2",
      ]);
    });
    expect(await screen.findByText(/deleted 2 files/)).toBeTruthy();
  });

  it("forwards the whole selection to one picked destination", async () => {
    routes["GET /v1/files"] = { status: 200, body: TWO_FILES };
    routes["GET /v1/friends"] = { status: 200, body: { items: [] } };
    routes["GET /v1/work/nodes"] = {
      status: 200,
      body: { items: [{ node_id: "n1", title: "Invoice Cleaner" }] },
    };
    routes["GET /v1/files/f1"] = {
      status: 200,
      body: { ...TWO_FILES.items[0], content: "hello" },
    };
    routes["GET /v1/files/f2"] = {
      status: 200,
      body: { ...TWO_FILES.items[1], content: "a,b" },
    };
    routes["POST /v1/files"] = {
      status: 201,
      body: { ...TWO_FILES.items[0], file_id: "copy" },
    };
    render(<FilesPane />);

    fireEvent.click(await screen.findByRole("button", { name: "Select" }));
    fireEvent.click(screen.getByText("notes.md"));
    fireEvent.click(screen.getByText("budget.csv"));
    fireEvent.click(screen.getByRole("button", { name: "Forward…" }));
    fireEvent.click(
      await screen.findByRole("button", { name: "Invoice Cleaner" }),
    );

    await waitFor(() => {
      const copies = calls.filter(
        (c) => c.method === "POST" && c.path === "/v1/files",
      );
      expect(copies).toHaveLength(2);
      expect(
        copies.every(
          (c) => (c.body as { node_id?: string }).node_id === "n1",
        ),
      ).toBe(true);
    });
    expect(
      await screen.findByText(/forwarded 2 files to Invoice Cleaner/),
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

    // No "New document" in the node's drawer either — documents are
    // OoLu's to write. An upload still lands in the NODE's files.
    expect(screen.queryByRole("button", { name: "New document" })).toBeNull();
    vi.mocked(pickLocalFiles).mockResolvedValue([
      new File(["hi"], "spec.md", { type: "text/markdown" }),
    ]);
    vi.mocked(fileToDrawerContent).mockResolvedValue({
      content: "hi",
      mediaType: "text/markdown",
    });
    fireEvent.click(screen.getByRole("button", { name: "Add" }));
    fireEvent.click(
      screen.getByRole("button", { name: "Upload from device" }),
    );
    await waitFor(() => {
      const create = calls.find((c) => c.method === "POST");
      expect((create?.body as { node_id: string }).node_id).toBe("n1");
    });
  });
});
