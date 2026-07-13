import { useEffect, useState, type FormEvent } from "react";
import { tf, useT } from "../ui";
import {
  DEFAULT_GLOBAL_SERVER,
  clientConfig,
  confirmReset,
  isRemote,
  login,
  register,
  requestReset,
  session,
  signInWithGoogle,
  verifyEmail,
} from "../api";

// verify: the code-entry step a mail-verifying host adds after register.
// reset: forgot-password — an e-mail first, then the code + new password.
type View = "signin" | "register" | "verify" | "reset";
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
  const tr = useT(); // the sign-in screen speaks the device's language
  const [view, setView] = useState<View>("signin");
  const [scope, setScope] = useState<Scope>("global");
  const [edgeMode, setEdgeMode] = useState<EdgeMode>("device");
  const [edgeServer, setEdgeServer] = useState(session.edgeServer ?? "");
  const [pairedServer, setPairedServer] = useState<string | null>(null);
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [code, setCode] = useState("");
  const [resetSent, setResetSent] = useState(false);
  const [notice, setNotice] = useState("");
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
      if (!url) throw new Error(tr("login.enterServer"));
      session.setEdgeServer(url);
      return url;
    }
    return isRemote() ? undefined : globalServer;
  };
  const showScope = !isRemote() && Boolean(onStayLocal);

  function switchView(next: View) {
    setView(next);
    setError("");
    setNotice("");
    setCode("");
    setResetSent(false);
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
      setError(err instanceof Error ? err.message : tr("login.googleFailed"));
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
        onSignedIn();
      } else if (view === "register") {
        const result = await register(username, password, authTarget());
        if (result.verificationRequired) {
          setNotice(
            tf("login.codeSent", { mail: username.trim() }),
          );
          setView("verify");
        } else {
          onSignedIn();
        }
      } else if (view === "verify") {
        await verifyEmail(username, code, password, authTarget());
        onSignedIn();
      } else if (!resetSent) {
        await requestReset(username, authTarget());
        setResetSent(true);
        setNotice(
          tf("login.resetSent", { mail: username.trim() }),
        );
      } else {
        await confirmReset(username, code, password, authTarget());
        setCode("");
        setPassword("");
        setResetSent(false);
        setNotice(tr("login.passwordChanged"));
        setView("signin");
      }
    } catch (err) {
      setError(
        err instanceof Error
          ? err.message
          : view === "signin"
            ? tr("login.signInFailed")
            : tr("login.registerFailed"),
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
              {tr("login.edgeIntro")}
            </p>
            <div className="mode-tabs scope-tabs">
              <button
                type="button"
                className={edgeMode === "device" ? "on" : ""}
                onClick={() => setEdgeMode("device")}
              >
                {tr("login.thisDevice")}
              </button>
              <button
                type="button"
                className={edgeMode === "network" ? "on" : ""}
                onClick={() => setEdgeMode("network")}
              >
                {tr("login.privateNetwork")}
              </button>
            </div>
            {edgeMode === "device" ? (
              <>
                <p className="muted">
                  {tr("login.deviceIntro")}
                </p>
                <button type="button" onClick={onStayLocal}>
                  {tr("login.continueEdge")}
                </button>
              </>
            ) : (
              <>
                <p className="muted">
                  {tr("login.networkIntro")}
                </p>
                <label htmlFor="edge-server">{tr("login.serverAddress")}</label>
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
              {view === "verify"
                ? tr("login.checkInbox")
                : view === "reset"
                  ? resetSent
                    ? tr("login.resetEnterCode")
                    : tr("login.resetEnterEmail")
                  : onEdgeNetwork
                    ? view === "signin"
                      ? tr("login.signInEdge")
                      : tr("login.registerEdge")
                    : view === "signin"
                      ? tr("login.signInGlobal")
                      : tr("login.registerGlobal")}
            </p>

            {view === "verify" ? null : (
              <>
                <label htmlFor="username">
                  {view === "signin" ? tr("login.username") : tr("login.email")}
                </label>
                <input
                  id="username"
                  autoComplete={view === "signin" ? "username" : "email"}
                  value={username}
                  onChange={(e) => setUsername(e.target.value)}
                />
              </>
            )}
            {view === "verify" || (view === "reset" && resetSent) ? (
              <>
                <label htmlFor="mail-code">{tr("login.code")}</label>
                <input
                  id="mail-code"
                  inputMode="numeric"
                  autoComplete="one-time-code"
                  placeholder="123456"
                  value={code}
                  onChange={(e) => setCode(e.target.value)}
                />
              </>
            ) : null}
            {view === "verify" || (view === "reset" && !resetSent) ? null : (
              <>
                <label htmlFor="password">
                  {view === "reset" ? tr("login.newPassword") : tr("login.password")}
                </label>
                <input
                  id="password"
                  type="password"
                  autoComplete={
                    view === "signin" ? "current-password" : "new-password"
                  }
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                />
              </>
            )}
            <button type="submit" disabled={busy}>
              {view === "signin"
                ? busy
                  ? tr("login.signingIn")
                  : tr("login.signIn")
                : view === "register"
                  ? busy
                    ? tr("login.creatingAccount")
                    : tr("login.createAccount")
                  : view === "verify"
                    ? busy
                      ? tr("login.verifying")
                      : tr("login.verify")
                    : resetSent
                      ? busy
                        ? tr("login.changingPassword")
                        : tr("login.changePassword")
                      : busy
                        ? tr("login.sendingCode")
                        : tr("login.sendCode")}
            </button>

            {view === "signin" || view === "register" ? (
              <div className="alt-auth">
                <button
                  type="button"
                  disabled={busy}
                  onClick={() => void google()}
                >
                  {tr("login.google")}
                </button>
                {view === "register" ? (
                  <button type="button" disabled title={tr("login.comingSoon")}>
                    {tr("login.phone")}
                  </button>
                ) : null}
              </div>
            ) : null}

            {notice ? <div className="muted">{notice}</div> : null}
            {error ? <div className="error">{error}</div> : null}

            <div className="login-switch">
              {view === "signin" ? (
                <>
                  {tr("login.noAccount")}{" "}
                  <button
                    type="button"
                    className="linklike"
                    onClick={() => switchView("register")}
                  >
                    {tr("login.createOne")}
                  </button>{" "}
                  <button
                    type="button"
                    className="linklike"
                    onClick={() => switchView("reset")}
                  >
                    {tr("login.forgot")}
                  </button>
                </>
              ) : view === "verify" ? (
                <>
                  {tr("login.wrongAddress")}{" "}
                  <button
                    type="button"
                    className="linklike"
                    onClick={() => switchView("register")}
                  >
                    {tr("login.startOver")}
                  </button>
                </>
              ) : (
                <>
                  {tr("login.haveAccount")}{" "}
                  <button
                    type="button"
                    className="linklike"
                    onClick={() => switchView("signin")}
                  >
                    {tr("login.signIn")}
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
