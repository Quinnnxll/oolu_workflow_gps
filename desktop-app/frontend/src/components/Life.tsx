import { useCallback, useEffect, useState } from "react";
import { api } from "../api";
import type { FileMeta } from "../api";
import type { RunSummary, TimelineEvent } from "../types";
import { Chat } from "./Chat";
import { FileView } from "./FileView";
import { Work } from "./Work";

// The Life environment: a messenger. The left pane lists who you can talk
// to — OoLu (the assistant, full function access), Friends (people and
// legal entities), and Noder (one log thread per node interaction) — and
// the right pane is the open conversation. Work is the same architecture
// over a separate environment; it ships in the next build.

type Selection =
  | { kind: "oolu" }
  | { kind: "friends" }
  | { kind: "noder"; run: RunSummary }
  | { kind: "file"; id: string };

export function Life() {
  const [mode, setMode] = useState<"life" | "work">("life");
  const [selected, setSelected] = useState<Selection>({ kind: "oolu" });
  const [runs, setRuns] = useState<RunSummary[]>([]);
  const [files, setFiles] = useState<FileMeta[]>([]);

  const refreshRuns = useCallback(async () => {
    try {
      setRuns((await api.runs()).items);
    } catch {
      setRuns([]);
    }
  }, []);

  const refreshFiles = useCallback(async () => {
    try {
      setFiles((await api.files()).items ?? []);
    } catch {
      setFiles([]);
    }
  }, []);

  useEffect(() => {
    void refreshRuns();
    void refreshFiles();
    const t = setInterval(() => {
      void refreshRuns();
      void refreshFiles();
    }, 5000);
    return () => clearInterval(t);
  }, [refreshRuns, refreshFiles]);

  if (mode === "work") {
    return <Work onLife={() => setMode("life")} />;
  }

  return (
    <div className="life">
      <aside className="convo-list">
        <div className="mode-tabs">
          <button className="on">Life</button>
          <button onClick={() => setMode("work")}>Work</button>
        </div>

        <button
          className={`convo ${selected.kind === "oolu" ? "on" : ""}`}
          onClick={() => setSelected({ kind: "oolu" })}
        >
          <span className="convo-avatar oolu">O</span>
          <span className="convo-body">
            <span className="convo-name">OoLu</span>
            <span className="convo-sub">your assistant</span>
          </span>
        </button>

        <div className="work-head">
          <span className="convo-group">Files</span>
          <button
            className="add-node"
            title="New document"
            onClick={async () => {
              const doc = await api.createFile("untitled.md");
              await refreshFiles();
              setSelected({ kind: "file", id: doc.file_id });
            }}
          >
            +
          </button>
        </div>
        {files.length === 0 && (
          <div className="convo-empty">Documents and sheets appear here.</div>
        )}
        {files.map((f) => (
          <button
            key={f.file_id}
            className={`convo ${
              selected.kind === "file" && selected.id === f.file_id ? "on" : ""
            }`}
            onClick={() => setSelected({ kind: "file", id: f.file_id })}
          >
            <span className="convo-avatar file">
              {/\.(csv|tsv)$/i.test(f.name) ? "▤" : "≡"}
            </span>
            <span className="convo-body">
              <span className="convo-name">{f.name}</span>
              <span className="convo-sub">
                {/\.(csv|tsv)$/i.test(f.name) ? "sheet" : "document"} ·{" "}
                {(f.size / 1024).toFixed(1)} kB
              </span>
            </span>
          </button>
        ))}

        <div className="convo-group">Friends</div>
        <button
          className={`convo ${selected.kind === "friends" ? "on" : ""}`}
          onClick={() => setSelected({ kind: "friends" })}
        >
          <span className="convo-avatar">+</span>
          <span className="convo-body">
            <span className="convo-sub">No conversations yet</span>
          </span>
        </button>

        <div className="convo-group">Noder</div>
        {runs.length === 0 && (
          <div className="convo-empty">Node activity appears here.</div>
        )}
        {runs.map((r) => (
          <button
            key={r.run_id}
            className={`convo ${
              selected.kind === "noder" && selected.run.run_id === r.run_id
                ? "on"
                : ""
            }`}
            onClick={() => setSelected({ kind: "noder", run: r })}
          >
            <span className="convo-avatar node">N</span>
            <span className="convo-body">
              <span className="convo-name">{r.intent}</span>
              <span className="convo-sub">{r.awaiting ?? r.phase}</span>
            </span>
          </button>
        ))}
      </aside>

      <section className="convo-pane">
        {selected.kind === "oolu" && <Chat />}
        {selected.kind === "friends" && (
          <div className="pane-empty">
            <p>Conversations with people and businesses will live here.</p>
            <p className="muted">Friends arrive with the online server.</p>
          </div>
        )}
        {selected.kind === "noder" && (
          <NoderThread
            key={selected.run.run_id}
            run={selected.run}
            onRunAgain={refreshRuns}
          />
        )}
        {selected.kind === "file" && (
          <FileView
            key={selected.id}
            fileId={selected.id}
            onChanged={refreshFiles}
            onDeleted={() => {
              void refreshFiles();
              setSelected({ kind: "oolu" });
            }}
          />
        )}
      </section>
    </div>
  );
}

// A node's side of the story: the raw audit log as a message history.
// Deliberately unpolished — this is developer material. Everyone else asks
// OoLu, who can read these logs and re-trigger the node on their behalf.
export function NoderThread({
  run,
  onRunAgain,
}: {
  run: RunSummary;
  onRunAgain: () => void;
}) {
  const [events, setEvents] = useState<TimelineEvent[] | null>(null);
  const [rerunning, setRerunning] = useState(false);
  const [notice, setNotice] = useState("");

  useEffect(() => {
    let cancelled = false;
    api
      .timeline(run.run_id)
      .then((t) => {
        if (!cancelled) setEvents(t.items);
      })
      .catch(() => {
        if (!cancelled) setEvents([]);
      });
    return () => {
      cancelled = true;
    };
  }, [run.run_id]);

  return (
    <div className="noder-thread">
      <div className="noder-head">
        <div>
          <div className="run-card-intent">{run.intent}</div>
          <div className="muted">
            {run.run_id} · {run.awaiting ?? run.phase}
          </div>
        </div>
        <button
          disabled={rerunning}
          onClick={async () => {
            setRerunning(true);
            setNotice("");
            try {
              await api.submitTask(run.intent);
              setNotice("Triggered again — the new interaction appears in the list.");
              onRunAgain();
            } catch (e) {
              setNotice(`Could not trigger: ${(e as Error).message}`);
            } finally {
              setRerunning(false);
            }
          }}
        >
          {rerunning ? "Triggering…" : "Run again"}
        </button>
      </div>
      {notice && <div className="muted">{notice}</div>}

      <div className="noder-log">
        {events === null && <div className="muted">Loading log…</div>}
        {events !== null && events.length === 0 && (
          <div className="muted">No log entries recorded.</div>
        )}
        {events?.map((e, i) => (
          <div key={i} className="log-line">
            <span className="log-at">{e.at}</span>
            <span className="log-label">{e.label}</span>
            {e.detail && <span className="log-detail">{e.detail}</span>}
          </div>
        ))}
      </div>

      <p className="muted noder-hint">
        Raw node log — ask OoLu to review it or dig into it for you.
      </p>
    </div>
  );
}
