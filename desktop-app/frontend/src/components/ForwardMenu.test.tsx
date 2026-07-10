import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { ForwardMenu } from "./ForwardMenu";

let routes: Record<string, { status: number; body: unknown }>;

const fetchMock = vi.fn(async (input: string | URL, init?: RequestInit) => {
  const u = new URL(String(input), "http://local.test");
  const method = init?.method ?? "GET";
  const hit = routes[`${method} ${u.pathname}`] ?? { status: 200, body: {} };
  return {
    ok: hit.status >= 200 && hit.status < 300,
    status: hit.status,
    text: async () => JSON.stringify(hit.body),
    json: async () => hit.body,
  } as Response;
});

beforeEach(() => {
  routes = {
    "GET /v1/friends": {
      status: 200,
      body: {
        items: [
          {
            peer: "bob",
            last_text: "hi",
            last_from: "bob",
            last_at: "t",
            unread: 0,
          },
        ],
      },
    },
    "GET /v1/work/nodes": {
      status: 200,
      body: {
        items: [
          { node_id: "n1", title: "Invoice Cleaner" },
          { node_id: "n2", title: "Tax Filer" },
        ],
      },
    },
  };
  localStorage.clear();
  window.__OOLU_API__ = "http://local.test";
  vi.stubGlobal("fetch", fetchMock);
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  delete window.__OOLU_API__;
});

async function openMenu() {
  render(
    <div>
      <ForwardMenu text="the numbers" from="OoLu" />
      <button>somewhere else</button>
    </div>,
  );
  fireEvent.click(screen.getByRole("button", { name: "Forward this message" }));
  await screen.findByText("Invoice Cleaner"); // targets loaded
}

describe("ForwardMenu", () => {
  it("narrows friends and nodes as the search types", async () => {
    await openMenu();
    expect(screen.getByText("bob")).toBeTruthy();
    expect(screen.getByText("Tax Filer")).toBeTruthy();

    fireEvent.change(screen.getByPlaceholderText(/search friends and nodes/), {
      target: { value: "invoice" },
    });
    expect(screen.getByText("Invoice Cleaner")).toBeTruthy();
    expect(screen.queryByText("bob")).toBeNull();
    expect(screen.queryByText("Tax Filer")).toBeNull();

    fireEvent.change(screen.getByPlaceholderText(/search friends and nodes/), {
      target: { value: "zzz" },
    });
    expect(screen.getByText("no matches")).toBeTruthy();
    // The save-to-file escape hatch never disappears.
    expect(screen.getByText("New file in Files")).toBeTruthy();
  });

  it("closes on a click anywhere else, and on Escape", async () => {
    await openMenu();
    fireEvent.mouseDown(screen.getByRole("button", { name: "somewhere else" }));
    expect(screen.queryByText("Invoice Cleaner")).toBeNull();

    fireEvent.click(
      screen.getByRole("button", { name: "Forward this message" }),
    );
    expect(await screen.findByText("Invoice Cleaner")).toBeTruthy();
    fireEvent.keyDown(document, { key: "Escape" });
    expect(screen.queryByText("Invoice Cleaner")).toBeNull();
  });

  it("clicking inside the menu does not close it", async () => {
    await openMenu();
    fireEvent.mouseDown(
      screen.getByPlaceholderText(/search friends and nodes/),
    );
    expect(screen.getByText("Invoice Cleaner")).toBeTruthy();
  });
});
