import { useCallback, useEffect, useRef, useState } from "react";
import { api, TERMINAL_PHASES } from "../api";
import type { ChatAction } from "../api";
import { humanizeEvent, statusSentence } from "../humanize";
import type { TaskView, TimelineEvent } from "../types";
import { Clarification } from "./Clarification";

// The whole product face: one conversation with OoLu. Work the assistant
// starts appears inline as a live run card — status, questions, decisions,
// result — so the machinery (skills, paths, synthesis) stays invisible.

type Msg =
  | { kind: "user"; text: string }
  | { kind: "assistant"; text: string; actions?: ChatAction[] }
  | { kind: "run"; runId: string };

const CHAT_KEY = "oolu_chat";
const WELCOME = "Hi! I'm OoLu. Tell me what you need done.";

function loadThread(): Msg[] {
  try {
    const raw = localStorage.getItem(CHAT_KEY);
    const parsed = raw ? (JSON.parse(raw) as Msg[]) : [];
    return Array.isArray(parsed) ? parsed : [];
  } catch {
    return [];
  }
}

export function Chat() {
  const [thread, setThread] = useState<Msg[]>(loadThread);
  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState(false);
  const endRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    localStorage.setItem(CHAT_KEY, JSON.stringify(thread));
    endRef.current?.scrollIntoView?.({ block: "end" });
  }, [thread]);

  async function send() {
    const text = draft.trim();
    if (!text || busy) return;
    setDraft("");
    setBusy(true);
    setThread((t) => [...t, { kind: "user", text }]);
    try {
      const history = thread
        .filter((m): m is Exclude<Msg, { kind: "run" }> => m.kind !== "run")
        .slice(-12)
        .map((m) => ({
          role: m.kind === "user" ? ("user" as const) : ("assistant" as const),
          content: m.text,
        }));
      const turn = await api.chat(text, history);
      setThread((t) => {
        const next: Msg[] = [
          ...t,
          { kind: "assistant", text: turn.reply, actions: turn.actions },
        ];
        if (turn.run_id) next.push({ kind: "run", runId: turn.run_id });
        return next;
      });
    } catch (e) {
      setThread((t) => [
        ...t,
        {
          kind: "assistant",
          text: `Sorry — that didn't go through (${(e as Error).message}).`,
        },
      ]);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="chat">
      <div className="chat-thread">
        {thread.length === 0 && <div className="bubble assistant">{WELCOME}</div>}
        {thread.map((m, i) =>
          m.kind === "run" ? (
            <RunCard key={m.runId} runId={m.runId} />
          ) : (
            <div key={i} className={`bubble ${m.kind}`}>
              {m.text}
              {m.kind === "assistant" && m.actions && m.actions.length > 0 && (
                <div className="tool-chips">
                  {m.actions.map((a, j) => (
                    <span key={j} className="tool-chip">
                      {actionLabel(a)}
                    </span>
                  ))}
                </div>
              )}
            </div>
          ),
        )}
        <div ref={endRef} />
      </div>
      <div className="chat-composer">
        <textarea
          placeholder="Message OoLu…"
          value={draft}
          rows={2}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey) {
              e.preventDefault();
              void send();
            }
          }}
        />
        <button disabled={busy || !draft.trim()} onClick={() => void send()}>
          {busy ? "…" : "Send"}
        </button>
      </div>
    </div>
  );
}

// One piece of work, living inside the conversation. Polls while active;
// pauses surface as questions/decisions the user answers in place.
export function RunCard({ runId }: { runId: string }) {
  const [task, setTask] = useState<TaskView | null>(null);
  const [gone, setGone] = useState(false);
  const [steps, setSteps] = useState<TimelineEvent[] | null>(null);
  const [showSteps, setShowSteps] = useState(false);

  const refresh = useCallback(async () => {
    try {
      setTask(await api.task(runId));
    } catch {
      // A vanished run (host wiped, other device) shouldn't wedge the chat.
      setGone(true);
    }
  }, [runId]);

  const refreshSteps = useCallback(async () => {
    try {
      setSteps((await api.timeline(runId)).items);
    } catch {
      setSteps([]);
    }
  }, [runId]);

  const terminal = task !== null && TERMINAL_PHASES.includes(task.phase);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  useEffect(() => {
    if (terminal || gone) return;
    const t = setInterval(() => {
      void refresh();
      if (showSteps) void refreshSteps();
    }, 2500);
    return () => clearInterval(t);
  }, [terminal, gone, refresh, refreshSteps, showSteps]);

  if (gone) {
    return <div className="run-card muted">This task is no longer available.</div>;
  }
  if (!task) {
    return <div className="run-card muted">Starting…</div>;
  }

  return (
    <div className="run-card">
      <div className="run-card-head">
        <span className="run-card-intent">{task.intent}</span>
        <span className={`phase phase-${task.awaiting ?? task.phase}`}>
          {statusLabel(task)}
        </span>
      </div>
      <div className="run-voice">{statusSentence(task)}</div>

      {task.awaiting === "clarification" && (
        <Clarification task={task} onResolved={setTask} />
      )}

      {task.awaiting === "confirmation" && (
        <div className="decision">
          {task.prompt && <p>{task.prompt}</p>}
          <div className="row">
            <button onClick={async () => setTask(await api.confirm(task.run_id, true))}>
              Approve
            </button>
            <button
              className="ghost"
              onClick={async () => setTask(await api.confirm(task.run_id, false))}
            >
              Reject
            </button>
          </div>
        </div>
      )}

      {task.awaiting === "incident" && (
        <div className="decision">
          {task.prompt && <p>{task.prompt}</p>}
          <div className="row">
            <button
              onClick={async () =>
                setTask(await api.resolveIncident(task.run_id, "retry"))
              }
            >
              Retry
            </button>
            <button
              className="ghost"
              onClick={async () =>
                setTask(await api.resolveIncident(task.run_id, "abort"))
              }
            >
              Abort
            </button>
          </div>
        </div>
      )}

      {task.failure_reason && <div className="error">{task.failure_reason}</div>}
      {task.result && (
        <pre className="result">{JSON.stringify(task.result, null, 2)}</pre>
      )}

      <div className="run-card-foot">
        <button
          className="linklike"
          onClick={() => {
            const next = !showSteps;
            setShowSteps(next);
            if (next && steps === null) void refreshSteps();
          }}
        >
          {showSteps ? "hide what I did" : "what I did"}
        </button>
        {task.can_cancel && !terminal && (
          <button
            className="linklike"
            onClick={async () => setTask(await api.cancel(task.run_id))}
          >
            cancel
          </button>
        )}
      </div>

      {showSteps && (
        <div className="run-steps">
          {steps === null && <div className="muted">Fetching the record…</div>}
          {steps !== null && steps.length === 0 && (
            <div className="muted">Nothing recorded yet.</div>
          )}
          {steps?.map((s, i) => (
            <div key={i} className="run-step" title={s.label}>
              <span className="run-step-dot">•</span>
              <span>{humanizeEvent(s.label)}</span>
              <span className="run-step-at">
                {new Date(s.at).toLocaleTimeString()}
              </span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

// The chip's verb: what the assistant actually touched this turn.
function actionLabel(action: ChatAction): string {
  if (action.tool === "list_files") return "listed your files";
  if (action.tool === "read_file") return `read ${action.name ?? "a file"}`;
  if (action.tool === "write_file") return `updated ${action.name ?? "a file"}`;
  return action.tool;
}

function statusLabel(task: TaskView): string {
  if (task.awaiting === "clarification") return "needs an answer";
  if (task.awaiting === "confirmation") return "needs a decision";
  if (task.awaiting === "incident") return "hit a snag";
  if (task.phase === "completed") return "done";
  if (task.phase === "failed") return "failed";
  if (task.phase === "cancelled") return "cancelled";
  return "working…";
}
