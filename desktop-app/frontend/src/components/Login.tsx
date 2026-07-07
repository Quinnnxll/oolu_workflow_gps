import { useEffect, useState, type FormEvent } from "react";
import {
  DEFAULT_GLOBAL_SERVER,
  clientConfig,
  isRemote,
  login,
  register,
  signInWithGoogle,
} from "../api";

type View = "signin" | "register";
type Scope = "edge" | "global";

// The sign-in screen never shows a raw host:port. The choice is Edge —
// this device: account, engine, and data stay here — or Global, the OoLu
// online service (a paired OOLU_SERVER_URL overrides its address quietly).
export function Login({
  onSignedIn,
  onStayLocal,
}: {
  onSignedIn: () => void;
  // Present when the local engine is running underneath: the screen is an
  // offer, not a wall, and Edge is the way back to it.
  onStayLocal?: () => void;
}) {
  const [view, setView] = useState<View>("signin");
  const [scope, setScope] = useState<Scope>("global");
  const [pairedServer, setPairedServer] = useState<string | null>(null);
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  // A paired install (OOLU_SERVER_URL) redirects Global to its own server.
  useEffect(() => {
    void clientConfig().then((cfg) => {
      if (typeof cfg.server === "string" && cfg.server.trim()) {
        setPairedServer(cfg.server.trim().replace(/\/+$/, ""));
      }
    });
  }, []);

  const globalServer = pairedServer ?? DEFAULT_GLOBAL_SERVER;
  const authTarget = (): string | undefined =>
    isRemote() ? undefined : globalServer;
  const showScope = !isRemote() && Boolean(onStayLocal);

  function switchView(next: View) {
    setView(next);
    setError("");
  }

  async function google() {
    setError("");
    setBusy(true);
    try {
      // Opens the system browser to Google's consent page and polls the
      // host until the browser leg lands; the token never rides the URL.
      await signInWithGoogle(authTarget());
      onSignedIn();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Google sign-in failed");
    } finally {
      setBusy(false);
    }
  }

  async function submit(event: FormEvent) {
    event.preventDefault();
    setError("");
    setBusy(true);
    try {
      if (view === "signin") {
        await login(username, password, authTarget());
      } else {
        await register(username, password, authTarget());
      }
      onSignedIn();
    } catch (err) {
      setError(
        err instanceof Error
          ? err.message
          : view === "signin"
            ? "sign-in failed"
            : "registration failed",
      );
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="login">
      <form className="login-card" onSubmit={submit}>
        <div className="brand">OoLu</div>

        {showScope ? (
          <div className="mode-tabs scope-tabs">
            <button
              type="button"
              className={scope === "edge" ? "on" : ""}
              onClick={() => setScope("edge")}
            >
              Edge
            </button>
            <button
              type="button"
              className={scope === "global" ? "on" : ""}
              onClick={() => setScope("global")}
            >
              Global
            </button>
          </div>
        ) : null}

        {showScope && scope === "edge" ? (
          <>
            <p className="muted">
              Edge is this device: your account, your engine, and everything
              you teach OoLu stay here.
            </p>
            <button type="button" onClick={onStayLocal}>
              Continue on Edge
            </button>
          </>
        ) : (
          <>
            <p className="muted">
              {view === "signin"
                ? "Sign in to OoLu Global."
                : "Create your OoLu Global account."}
            </p>

            <label htmlFor="username">
              {view === "signin" ? "Username" : "E-mail"}
            </label>
            <input
              id="username"
              autoComplete={view === "signin" ? "username" : "email"}
              value={username}
              onChange={(e) => setUsername(e.target.value)}
            />
            <label htmlFor="password">Password</label>
            <input
              id="password"
              type="password"
              autoComplete={
                view === "signin" ? "current-password" : "new-password"
              }
              value={password}
              onChange={(e) => setPassword(e.target.value)}
            />
            <button type="submit" disabled={busy}>
              {view === "signin"
                ? busy
                  ? "Signing in…"
                  : "Sign in"
                : busy
                  ? "Creating account…"
                  : "Create account"}
            </button>

            <div className="alt-auth">
              <button
                type="button"
                disabled={busy}
                onClick={() => void google()}
              >
                Continue with Google
              </button>
              {view === "register" ? (
                <button type="button" disabled title="Coming soon">
                  Continue with phone
                </button>
              ) : null}
            </div>

            {error ? <div className="error">{error}</div> : null}

            <div className="login-switch">
              {view === "signin" ? (
                <>
                  No account?{" "}
                  <button
                    type="button"
                    className="linklike"
                    onClick={() => switchView("register")}
                  >
                    Create one
                  </button>
                </>
              ) : (
                <>
                  Have an account?{" "}
                  <button
                    type="button"
                    className="linklike"
                    onClick={() => switchView("signin")}
                  >
                    Sign in
                  </button>
                </>
              )}
            </div>
          </>
        )}
      </form>
    </div>
  );
}
