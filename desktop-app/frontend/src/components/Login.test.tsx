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

  it("registers with e-mail from the create-account view", async () => {
    fetchMock.mockResolvedValue(
      reply(200, { token: "t", principal: "bob@example.com" }),
    );
    const onSignedIn = vi.fn();
    render(<Login onSignedIn={onSignedIn} />);

    fireEvent.click(screen.getByRole("button", { name: "Create one" }));
    fireEvent.change(screen.getByLabelText("E-mail"), {
      target: { value: "bob@example.com" },
    });
    fireEvent.change(screen.getByLabelText("Password"), {
      target: { value: "pw" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Create account" }));

    await waitFor(() => expect(onSignedIn).toHaveBeenCalled());
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("https://host.example/v1/auth/register");
    expect(JSON.parse(String(init.body))).toEqual({
      email: "bob@example.com",
      password: "pw",
    });
    expect(session.token).toBe("t");
  });

  it("offers Google and phone sign-up, disabled until a provider exists", () => {
    render(<Login onSignedIn={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: "Create one" }));

    const google = screen.getByRole("button", {
      name: "Continue with Google",
    }) as HTMLButtonElement;
    const phone = screen.getByRole("button", {
      name: "Continue with phone",
    }) as HTMLButtonElement;
    expect(google.disabled).toBe(true);
    expect(phone.disabled).toBe(true);
  });

  describe("local build (no baked-in server)", () => {
    beforeEach(() => {
      window.__OOLU_REMOTE__ = false;
    });

    it("asks which server to sign in to and remembers it", async () => {
      fetchMock.mockResolvedValue(
        reply(200, { token: "t", principal: "alice" }),
      );
      const onSignedIn = vi.fn();
      render(<Login onSignedIn={onSignedIn} />);

      fireEvent.change(screen.getByLabelText("Server"), {
        target: { value: "https://online.oolu.example" },
      });
      fireEvent.change(screen.getByLabelText("Username"), {
        target: { value: "alice" },
      });
      fireEvent.change(screen.getByLabelText("Password"), {
        target: { value: "pw" },
      });
      fireEvent.click(screen.getByRole("button", { name: "Sign in" }));

      await waitFor(() => expect(onSignedIn).toHaveBeenCalled());
      const [url] = fetchMock.mock.calls[0] as [string];
      expect(url).toBe("https://online.oolu.example/v1/auth/login");
      expect(session.server).toBe("https://online.oolu.example");
    });

    it("lets the user stay local instead of signing in", () => {
      const onStayLocal = vi.fn();
      render(<Login onSignedIn={vi.fn()} onStayLocal={onStayLocal} />);

      fireEvent.click(screen.getByRole("button", { name: "Stay local" }));

      expect(onStayLocal).toHaveBeenCalled();
      expect(fetchMock).not.toHaveBeenCalled();
    });
  });
});
