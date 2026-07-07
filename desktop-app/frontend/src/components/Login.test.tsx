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
    const [url, init] = fetchMock.mock.calls.find(([u]) =>
      String(u).endsWith("/v1/auth/register"),
    ) as [string, RequestInit];
    expect(url).toBe("https://host.example/v1/auth/register");
    expect(JSON.parse(String(init.body))).toEqual({
      email: "bob@example.com",
      password: "pw",
    });
    expect(session.token).toBe("t");
  });

  it("phone sign-up stays disabled until a provider exists", () => {
    render(<Login onSignedIn={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: "Create one" }));

    const phone = screen.getByRole("button", {
      name: "Continue with phone",
    }) as HTMLButtonElement;
    expect(phone.disabled).toBe(true);
  });

  it("signs in with Google: opens the consent page, polls, stores the token", async () => {
    const opened = vi.fn();
    vi.stubGlobal("open", opened);
    fetchMock.mockImplementation(async (input: string | URL) => {
      const url = String(input);
      if (url.endsWith("/v1/auth/google/start")) {
        return reply(200, {
          auth_url: "https://accounts.google.com/o/oauth2/v2/auth?x=1",
          state: "st-1",
        });
      }
      if (url.endsWith("/v1/auth/google/finish")) {
        return reply(200, {
          status: "complete",
          token: "g-token",
          principal: "quinn",
          tenant: "main",
        });
      }
      return reply(404, { error: { message: "nope" } });
    });
    const onSignedIn = vi.fn();
    render(<Login onSignedIn={onSignedIn} />);

    fireEvent.click(
      screen.getByRole("button", { name: "Continue with Google" }),
    );

    await waitFor(() => expect(onSignedIn).toHaveBeenCalled());
    expect(String(opened.mock.calls[0][0])).toContain(
      "accounts.google.com",
    );
    // The finish poll carried the one-shot state; the token is stored.
    const finish = fetchMock.mock.calls.find(([u]) =>
      String(u).endsWith("/v1/auth/google/finish"),
    ) as [string, RequestInit];
    expect(JSON.parse(String(finish[1].body))).toEqual({ state: "st-1" });
    expect(session.token).toBe("g-token");
    expect(session.principal).toBe("quinn");
  });

  it("says so plainly when the host has no Google client", async () => {
    fetchMock.mockResolvedValue(reply(404, {}));
    render(<Login onSignedIn={vi.fn()} />);

    fireEvent.click(
      screen.getByRole("button", { name: "Continue with Google" }),
    );

    expect(
      await screen.findByText(/does not offer Google sign-in yet/),
    ).toBeTruthy();
  });

  describe("paired install (OOLU_SERVER_URL configured)", () => {
    beforeEach(() => {
      window.__OOLU_REMOTE__ = false;
      window.__OOLU_API__ = ""; // the local engine serves client-config
    });

    it("redirects Global to the paired server, never showing a raw host", async () => {
      fetchMock.mockImplementation(async (input: string | URL) => {
        const url = String(input);
        if (url.endsWith("/v1/client-config")) {
          return reply(200, {
            server: "http://127.0.0.1:8771/",
            google: true,
            registration: true,
          });
        }
        if (url.endsWith("/v1/auth/login")) {
          return reply(200, { token: "t", principal: "alice", tenant: "main" });
        }
        return reply(404, {});
      });
      const onSignedIn = vi.fn();
      render(<Login onSignedIn={onSignedIn} onStayLocal={vi.fn()} />);
      await waitFor(() =>
        expect(
          fetchMock.mock.calls.some(([u]) =>
            String(u).endsWith("/v1/client-config"),
          ),
        ).toBe(true),
      );

      // The raw host:port never appears anywhere on the screen.
      expect(screen.queryByText(/127\.0\.0\.1/)).toBeNull();
      expect(screen.queryByLabelText("Server")).toBeNull();

      fireEvent.change(screen.getByLabelText("Username"), {
        target: { value: "alice" },
      });
      fireEvent.change(screen.getByLabelText("Password"), {
        target: { value: "pw" },
      });
      fireEvent.click(screen.getByRole("button", { name: "Sign in" }));

      await waitFor(() => expect(onSignedIn).toHaveBeenCalled());
      // ...but Global quietly targets the paired server.
      const login = fetchMock.mock.calls.find(([u]) =>
        String(u).endsWith("/v1/auth/login"),
      ) as [string, RequestInit];
      expect(String(login[0])).toBe("http://127.0.0.1:8771/v1/auth/login");
    });
  });

  describe("local build: Edge or Global, never a server field", () => {
    beforeEach(() => {
      window.__OOLU_REMOTE__ = false;
      window.__OOLU_API__ = "";
    });

    it("Global signs into the OoLu service by default", async () => {
      fetchMock.mockImplementation(async (input: string | URL) => {
        const url = String(input);
        if (url.endsWith("/v1/client-config")) return reply(200, {});
        return reply(200, { token: "t", principal: "alice" });
      });
      const onSignedIn = vi.fn();
      render(<Login onSignedIn={onSignedIn} onStayLocal={vi.fn()} />);

      expect(screen.queryByLabelText("Server")).toBeNull();
      fireEvent.change(screen.getByLabelText("Username"), {
        target: { value: "alice" },
      });
      fireEvent.change(screen.getByLabelText("Password"), {
        target: { value: "pw" },
      });
      fireEvent.click(screen.getByRole("button", { name: "Sign in" }));

      await waitFor(() => expect(onSignedIn).toHaveBeenCalled());
      const [url] = fetchMock.mock.calls.find(([u]) =>
        String(u).includes("/v1/auth/login"),
      ) as [string];
      expect(url).toBe("https://ooludomaintobedetermined/v1/auth/login");
      expect(session.server).toBe("https://ooludomaintobedetermined");
    });

    it("Edge keeps everything on this device", async () => {
      fetchMock.mockResolvedValue(reply(200, {}));
      const onStayLocal = vi.fn();
      render(<Login onSignedIn={vi.fn()} onStayLocal={onStayLocal} />);

      fireEvent.click(screen.getByRole("button", { name: "Edge" }));
      expect(
        screen.getByText(/account, your engine, and everything you teach/),
      ).toBeTruthy();
      fireEvent.click(screen.getByRole("button", { name: "Continue on Edge" }));

      expect(onStayLocal).toHaveBeenCalled();
      // Edge sends nothing to any auth door; only the mount-time
      // client-config probe (local and secret-free) happened.
      const authCalls = fetchMock.mock.calls.filter(([u]) =>
        String(u).includes("/v1/auth/"),
      );
      expect(authCalls).toEqual([]);
    });
  });
});
