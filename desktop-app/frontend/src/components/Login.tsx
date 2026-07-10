import { useEffect, useState, type FormEvent } from "react";
import {
  DEFAULT_GLOBAL_SERVER,
  clientConfig,
  isRemote,
  login,
  register,
  session,
  signInWithGoogle,
} from "../api";

type View = "signin" | "register";
type Scope = "edge" | "global";
type EdgeMode = "device" | "network";

// The sign-in screen's choice is Edge or Global. Edge keeps everything on
// the user's side — this device, or a private server on their own network
// (a static address a group shares); Global is the OoLu online service (a
// paired OOLU_SERVER_URL overrides its address quietly). An Edge private
// network still uses real accounts — a username and password — so that
// onboarding a node created under a Supernode names an actual person.
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
  const [edgeMode, setEdgeMode] = useState<EdgeMode>("device");
  const [edgeServer, setEdgeServer] = useState(session.edgeServer ?? "");
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
  const onEdgeNetwork = scope === "edge" && edgeMode === "network";
  const authTarget = (): string | undefined => {
    if (onEdgeNetwork) {
      const url = edgeServer.trim().replace(/\/+$/, "");
      if (!url) throw new Error("enter your private server's address");
      session.setEdgeServer(url);
      return url;
    }
    return isRemote() ? undefined : globalServer;
  };
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
              Edge keeps everything on your side: this device, or a private
              server on your own network.
            </p>
            <div className="mode-tabs scope-tabs">
              <button
                type="button"
                className={edgeMode === "device" ? "on" : ""}
                onClick={() => setEdgeMode("device")}
              >
                This device
              </button>
              <button
                type="button"
                className={edgeMode === "network" ? "on" : ""}
                onClick={() => setEdgeMode("network")}
              >
                Private network
              </button>
            </div>
            {edgeMode === "device" ? (
              <>
                <p className="muted">
                  Your account, your engine, and everything you teach OoLu
                  stay on this machine.
                </p>
                <button type="button" onClick={onStayLocal}>
                  Continue on Edge
                </button>
              </>
            ) : (
              <>
                <p className="muted">
                  A private server your group runs on its own network (a
                  static address everyone can reach). You still sign in with
                  a username and password — onboarding a node created under
                  a Supernode has to name an actual person.
                </p>
                <label htmlFor="edge-server">Private server address</label>
                <input
                  id="edge-server"
                  placeholder="http://192.168.1.20:8787"
                  value={edgeServer}
                  onChange={(e) => setEdgeServer(e.target.value)}
                />
              </>
            )}
          </>
        ) : null}

        {showScope && scope === "edge" && edgeMode === "device" ? null : (
          <>
            <p className="muted">
              {onEdgeNetwork
                ? view === "signin"
                  ? "Sign in to your private network server."
                  : "Create your account on the private network server."
                : view === "signin"
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
