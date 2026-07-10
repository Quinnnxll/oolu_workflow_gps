import { useCallback, useEffect, useState } from "react";
import { api, isRemote, requiresLogin, session, signOut } from "./api";
import { applyLanguage, applyTheme } from "./ui";
import type { InboxItem, TaskView } from "./types";
import { Life } from "./components/Life";
import { TaskPane } from "./components/TaskPane";
import { Inbox } from "./components/Inbox";
import { Skills } from "./components/Skills";
import { Login } from "./components/Login";

// The product face is the conversation: users talk to OoLu, and the
// machinery (runs, skills, assembly) stays behind it. The old operator
// screens survive only in dev builds, behind the header's "dev" toggle.

export function App() {
  // Local loopback needs no sign-in; a remote host does until we hold a token.
  const [authed, setAuthed] = useState(!requiresLogin());
  // Local mode can *optionally* sign into the online server; this shows the
  // sign-in screen over the running local app when the user asks for it.
  const [showAuth, setShowAuth] = useState(false);
  const [dev, setDev] = useState(false);

  // The settings node owns theme and language; once signed in, its
  // values replace the locally cached guesses.
  useEffect(() => {
    if (!authed) return;
    void api
      .settings()
      .then(({ items }) => {
        for (const item of items ?? []) {
          if (item.key === "app.theme") applyTheme(String(item.value));
          if (item.key === "app.language") applyLanguage(String(item.value));
        }
      })
      .catch(() => {}); // unreachable settings: the cached look stands
  }, [authed]);

  if (!authed) {
    return <Login onSignedIn={() => setAuthed(true)} />;
  }

  if (showAuth) {
    return (
      <Login
        onSignedIn={() => setShowAuth(false)}
        onStayLocal={() => setShowAuth(false)}
      />
    );
  }

  return (
    <div className="app">
      <header>
        <div className="brand">OoLu</div>
        {import.meta.env.DEV && (
          <button className="linklike dev-toggle" onClick={() => setDev(!dev)}>
            {dev ? "chat" : "dev"}
          </button>
        )}
        <div className="loopback">
          {isRemote() || session.signedIn() ? (
            <>
              {session.principal ?? "signed in"} ·{" "}
              <button className="linklike" onClick={signOut}>
                sign out
              </button>
            </>
          ) : (
            <>
              <span
                className="chip"
                title="Not signed in to an online server — learned paths and generated skills stay in your local database"
              >
                Local
              </span>{" "}
              <button className="linklike" onClick={() => setShowAuth(true)}>
                sign in
              </button>
            </>
          )}
        </div>
      </header>

      <main className={dev ? "" : "chat-main"}>
        {dev ? <DevScreens /> : <Life />}
      </main>
    </div>
  );
}

// The pre-chat operator surface (task console, inbox, skill search) — dev
// builds only. Useful for poking the engine; never shipped to end users.
type Tab = "task" | "inbox" | "skills";

function DevScreens() {
  const [tab, setTab] = useState<Tab>("task");
  const [task, setTask] = useState<TaskView | null>(null);
  const [inbox, setInbox] = useState<InboxItem[]>([]);

  const refreshInbox = useCallback(async () => {
    try {
      setInbox((await api.inbox()).items);
    } catch {
      setInbox([]);
    }
  }, []);

  useEffect(() => {
    void refreshInbox();
    const t = setInterval(refreshInbox, 4000);
    return () => clearInterval(t);
  }, [refreshInbox]);

  const openTask = useCallback(async (runId: string) => {
    setTask(await api.task(runId));
    setTab("task");
  }, []);

  return (
    <>
      <nav className="dev-nav">
        <button className={tab === "task" ? "on" : ""} onClick={() => setTab("task")}>
          Task
        </button>
        <button className={tab === "inbox" ? "on" : ""} onClick={() => setTab("inbox")}>
          Inbox{inbox.length ? <span className="badge">{inbox.length}</span> : null}
        </button>
        <button className={tab === "skills" ? "on" : ""} onClick={() => setTab("skills")}>
          Skills
        </button>
      </nav>
      {tab === "task" && (
        <TaskPane task={task} setTask={setTask} onChanged={refreshInbox} />
      )}
      {tab === "inbox" && (
        <Inbox items={inbox} onOpen={openTask} onRefresh={refreshInbox} />
      )}
      {tab === "skills" && <Skills />}
    </>
  );
}
