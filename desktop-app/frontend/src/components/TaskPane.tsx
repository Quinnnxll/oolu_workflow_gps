import { useState } from "react";
import { api } from "../api";
import type { TaskView } from "../types";
import { Clarification } from "./Clarification";
import { Timeline } from "./Timeline";

interface Props {
  task: TaskView | null;
  setTask: (t: TaskView | null) => void;
  onChanged: () => void;
}

export function TaskPane({ task, setTask, onChanged }: Props) {
  const [intent, setIntent] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function submit() {
    if (!intent.trim()) return;
    setBusy(true);
    setError(null);
    try {
      const t = await api.submitTask(intent.trim());
      setTask(t);
      setIntent("");
      onChanged();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="task-pane">
      <div className="entry">
        <textarea
          placeholder="Describe what you want done…"
          value={intent}
          onChange={(e) => setIntent(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) void submit();
          }}
          rows={3}
        />
        <button disabled={busy} onClick={() => void submit()}>
          {busy ? "Submitting…" : "Submit"}
        </button>
      </div>
      {error && <div className="error">{error}</div>}

      {task && (
        <div className="run">
          <div className="run-head">
            <div>
              <div className="run-intent">{task.intent}</div>
              <div className="run-id">{task.run_id}</div>
            </div>
            <Phase task={task} />
          </div>

          {task.awaiting === "clarification" && (
            <Clarification
              task={task}
              onResolved={(t) => {
                setTask(t);
                onChanged();
              }}
            />
          )}

          {task.awaiting === "confirmation" && (
            <Decision
              prompt={task.prompt}
              actions={[
                { label: "Approve", run: () => api.confirm(task.run_id, true) },
                { label: "Reject", run: () => api.confirm(task.run_id, false) },
              ]}
              onDone={(t) => {
                setTask(t);
                onChanged();
              }}
            />
          )}

          {task.awaiting === "incident" && (
            <Decision
              prompt={task.prompt}
              actions={[
                { label: "Retry", run: () => api.resolveIncident(task.run_id, "retry") },
                { label: "Abort", run: () => api.resolveIncident(task.run_id, "abort") },
              ]}
              onDone={(t) => {
                setTask(t);
                onChanged();
              }}
            />
          )}

          {task.failure_reason && <div className="error">{task.failure_reason}</div>}
          {task.result && (
            <pre className="result">{JSON.stringify(task.result, null, 2)}</pre>
          )}

          <Timeline runId={task.run_id} phase={task.phase} />

          {task.can_cancel && (
            <button
              className="ghost"
              onClick={async () => {
                setTask(await api.cancel(task.run_id));
                onChanged();
              }}
            >
              Cancel
            </button>
          )}
        </div>
      )}
    </section>
  );
}

function Phase({ task }: { task: TaskView }) {
  const p = task.awaiting ?? task.phase;
  return <span className={`phase phase-${p}`}>{p}</span>;
}

interface DecisionAction {
  label: string;
  run: () => Promise<TaskView>;
}

function Decision({
  prompt,
  actions,
  onDone,
}: {
  prompt: string | null;
  actions: DecisionAction[];
  onDone: (t: TaskView) => void;
}) {
  const [busy, setBusy] = useState(false);
  return (
    <div className="decision">
      {prompt && <p>{prompt}</p>}
      <div className="row">
        {actions.map((a) => (
          <button
            key={a.label}
            disabled={busy}
            onClick={async () => {
              setBusy(true);
              try {
                onDone(await a.run());
              } finally {
                setBusy(false);
              }
            }}
          >
            {a.label}
          </button>
        ))}
      </div>
    </div>
  );
}
