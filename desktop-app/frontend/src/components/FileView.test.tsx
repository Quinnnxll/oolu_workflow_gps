import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { FileView } from "./FileView";

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

function doc(overrides: Record<string, unknown> = {}) {
  return {
    file_id: "f1",
    name: "notes.md",
    media_type: "text/markdown",
    size: 20,
    created_at: "t",
    updated_at: "t",
    content: "First paragraph.\n\nSecond paragraph.",
    ...overrides,
  };
}

describe("FileView — documents", () => {
  it("renders a reading page, then edits and saves", async () => {
    routes["GET /v1/files/f1"] = { status: 200, body: doc() };
    routes["PUT /v1/files/f1"] = {
      status: 200,
      body: doc({ content: "Rewritten." }),
    };
    render(<FileView fileId="f1" onChanged={vi.fn()} onDeleted={vi.fn()} />);

    expect(await screen.findByText("First paragraph.")).toBeTruthy();
    expect(screen.getByText("Second paragraph.")).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "Edit" }));
    fireEvent.change(screen.getByLabelText("Document content"), {
      target: { value: "Rewritten." },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() =>
      expect(
        calls.some(
          (c) =>
            c.method === "PUT" &&
            c.path === "/v1/files/f1" &&
            (c.body as { content: string }).content === "Rewritten.",
        ),
      ).toBe(true),
    );
  });

  it("deletes and reports back", async () => {
    routes["GET /v1/files/f1"] = { status: 200, body: doc() };
    routes["DELETE /v1/files/f1"] = { status: 200, body: { deleted: true } };
    const onDeleted = vi.fn();
    render(<FileView fileId="f1" onChanged={vi.fn()} onDeleted={onDeleted} />);

    fireEvent.click(await screen.findByRole("button", { name: "delete" }));

    await waitFor(() => expect(onDeleted).toHaveBeenCalled());
  });
});

describe("FileView — sheets", () => {
  it("renders CSV as the app's own grid and saves edited cells", async () => {
    routes["GET /v1/files/f2"] = {
      status: 200,
      body: doc({
        file_id: "f2",
        name: "budget.csv",
        media_type: "text/csv",
        content: "item,cost\ncoffee,3",
      }),
    };
    routes["PUT /v1/files/f2"] = { status: 200, body: doc({ file_id: "f2" }) };
    render(<FileView fileId="f2" onChanged={vi.fn()} onDeleted={vi.fn()} />);

    const cell = (await screen.findByLabelText("cell 2:2")) as HTMLInputElement;
    expect(cell.value).toBe("3");
    // No office plugin: it's a plain themed table.
    expect(document.querySelector("table.sheet")).toBeTruthy();

    fireEvent.change(cell, { target: { value: "4" } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => {
      const put = calls.find(
        (c) => c.method === "PUT" && c.path === "/v1/files/f2",
      );
      expect((put?.body as { content: string }).content).toBe(
        "item,cost\ncoffee,4",
      );
    });
  });

  it("adds rows to the grid", async () => {
    routes["GET /v1/files/f2"] = {
      status: 200,
      body: doc({
        file_id: "f2",
        name: "budget.csv",
        media_type: "text/csv",
        content: "item,cost\ncoffee,3",
      }),
    };
    render(<FileView fileId="f2" onChanged={vi.fn()} onDeleted={vi.fn()} />);
    await screen.findByLabelText("cell 2:1");

    fireEvent.click(screen.getByRole("button", { name: "+ row" }));

    expect(screen.getByLabelText("cell 3:1")).toBeTruthy();
  });
});
