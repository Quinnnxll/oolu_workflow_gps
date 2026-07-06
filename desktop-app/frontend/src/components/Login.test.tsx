import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { Login } from "./Login";
import { session } from "../api";

let fetchMock: ReturnType<typeof vi.fn>;

beforeEach(() => {
  localStorage.clear();
  window.__OOLU_API__ = "https://host.example";
  window.__OOLU_REMOTE__ = true;
  fetchMock = vi.fn();
  vi.stubGlobal("fetch", fetchMock);
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  delete window.__OOLU_API__;
  delete window.__OOLU_REMOTE__;
});

function reply(status: number, body: unknown) {
  return {
    ok: status >= 200 && status < 300,
    status,
    text: async () => JSON.stringify(body),
    json: async () => body,
  } as Response;
}

describe("Login", () => {
  it("signs in and notifies the parent on success", async () => {
    fetchMock.mockResolvedValue(
      reply(200, { token: "t", principal: "alice", tenant: "acme" }),
    );
    const onSignedIn = vi.fn();
    render(<Login onSignedIn={onSignedIn} />);

    fireEvent.change(screen.getByLabelText("Username"), {
      target: { value: "alice" },
    });
    fireEvent.change(screen.getByLabelText("Password"), {
      target: { value: "pw" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Sign in" }));

    await waitFor(() => expect(onSignedIn).toHaveBeenCalled());
    expect(session.token).toBe("t");
  });

  it("shows the server error and does not sign in on failure", async () => {
    fetchMock.mockResolvedValue(
      reply(401, { error: { message: "bad credentials" } }),
    );
    const onSignedIn = vi.fn();
    render(<Login onSignedIn={onSignedIn} />);

    fireEvent.click(screen.getByRole("button", { name: "Sign in" }));

    expect(await screen.findByText("bad credentials")).toBeTruthy();
    expect(onSignedIn).not.toHaveBeenCalled();
    expect(session.signedIn()).toBe(false);
  });
});
