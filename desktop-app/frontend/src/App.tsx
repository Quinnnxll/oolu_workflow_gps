import { useCallback, useEffect, useState } from "react";
import { api, isRemote, requiresLogin, session, signOut } from "./api";
import type { InboxItem, TaskView } from "./types";
import { TaskPane } from "./components/TaskPane";
import { Inbox } from "./components/Inbox";
import { Skills } from "./components/Skills";
import { Login } from "./components/Login";

type Tab = "task" | "inbox" | "skills";

export function App() {
  // Local loopback needs no sign-in; a remote host does until we hold a token.
  const [authed, setAuthed] = useState(!requiresLogin());
  // Local mode can *optionally* sign into the online server; this shows the
  // sign-in screen over the running local app when the user asks for it.
  const [showAuth, setShowAuth] = useState(false);
  const [tab, setTab] = useState<Tab>("task");
  const [task, setTask] = useState<TaskView | null>(null);
  const [inbox, setInbox] = useState<InboxItem[]>([]);

  const refreshInbox = useCallback(async () => {
    if (!authed) return;
    try {
      setInbox((await api.inbox()).items);
    } catch {
      setInbox([]);
    }
  }, [authed]);

  useEffect(() => {
    if (!authed) return;
    void refreshInbox();
    const t = setInterval(refreshInbox, 4000);
    return () => clearInterval(t);
  }, [authed, refreshInbox]);

  const openTask = useCallback(async (runId: string) => {
    setTask(await api.task(runId));
    setTab("task");
  }, []);

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
        <nav>
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

      <main>
        {tab === "task" && (
          <TaskPane task={task} setTask={setTask} onChanged={refreshInbox} />
        )}
        {tab === "inbox" && <Inbox items={inbox} onOpen={openTask} onRefresh={refreshInbox} />}
        {tab === "skills" && <Skills />}
      </main>
    </div>
  );
}
