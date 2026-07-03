import { useCallback, useEffect, useState } from "react";
import { api } from "./api";
import type { InboxItem, TaskView } from "./types";
import { TaskPane } from "./components/TaskPane";
import { Inbox } from "./components/Inbox";
import { Skills } from "./components/Skills";

type Tab = "task" | "inbox" | "skills";

export function App() {
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
        <div className="loopback">127.0.0.1 · local</div>
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
