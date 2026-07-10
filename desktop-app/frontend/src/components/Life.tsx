import { useCallback, useEffect, useState } from "react";
import { api, TERMINAL_PHASES } from "../api";
import { identityHue, updateAvatarSignals } from "../avatar";
import { conciseName } from "../naming";
import type { RunSummary, TimelineEvent } from "../types";
import { Chat } from "./Chat";
import { FilesPane } from "./FilesPane";
import { SettingsPane } from "./SettingsPane";
import { Work } from "./Work";

// The Life environment: a messenger. The left pane lists who you can talk
// to — OoLu (the assistant, full function access), Friends (people and
// legal entities), and Noder (one log thread per node interaction) — and
// the right pane is the open conversation. Work is the same architecture
// over a separate environment; it ships in the next build.

type Selection =
  | { kind: "oolu" }
  | { kind: "files" }
  | { kind: "settings" }
  | { kind: "friends" }
  | { kind: "noder"; run: RunSummary };

export function Life() {
  const [mode, setMode] = useState<"life" | "work">("life");
  const [selected, setSelected] = useState<Selection>({ kind: "oolu" });
  const [runs, setRuns] = useState<RunSummary[]>([]);

  const refreshRuns = useCallback(async () => {
    try {
      const items = (await api.runs()).items;
      setRuns(items);
      // The avatar carries the workload on its face.
      updateAvatarSignals({
        workload: items.filter((r) => !TERMINAL_PHASES.includes(r.phase)).length,
      });
    } catch {
      setRuns([]);
    }
  }, []);

  useEffect(() => {
    void refreshRuns();
    const t = setInterval(refreshRuns, 5000);
    return () => clearInterval(t);
  }, [refreshRuns]);

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

        <button
          className={`convo ${selected.kind === "files" ? "on" : ""}`}
          onClick={() => setSelected({ kind: "files" })}
        >
          <span className="convo-avatar file">≡</span>
          <span className="convo-body">
            <span className="convo-name">Files</span>
            <span className="convo-sub">documents & sheets</span>
          </span>
        </button>

        {/* Settings sits right below Files, above the conversations — a
            long Friends/Noder list must never hide it below the fold. */}
        <button
          className={`convo ${selected.kind === "settings" ? "on" : ""}`}
          onClick={() => setSelected({ kind: "settings" })}
        >
          <span className="convo-avatar file">⚙</span>
          <span className="convo-body">
            <span className="convo-name">Settings</span>
            <span className="convo-sub">app, account, model, budget</span>
          </span>
        </button>

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
            title={r.intent}
            onClick={() => setSelected({ kind: "noder", run: r })}
          >
            <span
              className="convo-avatar node"
              style={{
                background: `hsl(${identityHue(r.intent)} 45% 34%)`,
                color: "#fff",
                borderColor: "transparent",
              }}
            >
              {conciseName(r.intent).slice(0, 1).toUpperCase()}
            </span>
            <span className="convo-body">
              {/* A name is a label, not a transcript: the keywords name
                  the thread; the full request lives in the tooltip and
                  the thread itself. */}
              <span className="convo-name">{conciseName(r.intent)}</span>
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
            <p className="muted">
              Friends arrive with a server — OoLu Global, or your own private
              network server signed in from Edge.
            </p>
          </div>
        )}
        {selected.kind === "noder" && (
          <NoderThread
            key={selected.run.run_id}
            run={selected.run}
            onRunAgain={refreshRuns}
          />
        )}
        {selected.kind === "files" && <FilesPane />}
        {selected.kind === "settings" && <SettingsPane />}
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
          <div className="run-card-intent">{conciseName(run.intent)}</div>
          <div className="muted">“{run.intent}”</div>
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
