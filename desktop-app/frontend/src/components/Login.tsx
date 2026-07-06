import { useState, type FormEvent } from "react";
import { isRemote, login, register, session } from "../api";

type View = "signin" | "register";

export function Login({
  onSignedIn,
  onStayLocal,
}: {
  onSignedIn: () => void;
  // Present when the local engine is running underneath: the screen is an
  // offer, not a wall, and this is the way back to it.
  onStayLocal?: () => void;
}) {
  const [view, setView] = useState<View>("signin");
  const [server, setServer] = useState(session.server ?? "");
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  // A local build signs into a server the user names; a remote build's
  // server is baked in, so the field would be noise.
  const askServer = !isRemote();

  function switchView(next: View) {
    setView(next);
    setError("");
  }

  async function submit(event: FormEvent) {
    event.preventDefault();
    setError("");
    setBusy(true);
    try {
      if (view === "signin") {
        await login(username, password, askServer ? server : undefined);
      } else {
        await register(username, password, askServer ? server : undefined);
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
        <p className="muted">
          {view === "signin"
            ? "Sign in to your host."
            : "Create an account on the online server."}
        </p>

        {askServer ? (
          <>
            <label htmlFor="server">Server</label>
            <input
              id="server"
              placeholder="https://your-oolu-host"
              autoComplete="url"
              value={server}
              onChange={(e) => setServer(e.target.value)}
            />
          </>
        ) : null}

        <label htmlFor="username">{view === "signin" ? "Username" : "E-mail"}</label>
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
          autoComplete={view === "signin" ? "current-password" : "new-password"}
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

        {view === "register" ? (
          <div className="alt-auth">
            <button type="button" disabled title="Coming soon">
              Continue with Google
            </button>
            <button type="button" disabled title="Coming soon">
              Continue with phone
            </button>
          </div>
        ) : null}

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

        {onStayLocal ? (
          <div className="stay-local">
            Prefer to keep working offline? Learned paths and generated skills
            stay in your local database either way.{" "}
            <button type="button" className="linklike" onClick={onStayLocal}>
              Stay local
            </button>
          </div>
        ) : null}
      </form>
    </div>
  );
}
