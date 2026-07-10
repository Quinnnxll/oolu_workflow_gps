import { useState } from "react";
import { api } from "../api";
import type { PlanView, TaskView } from "../types";
import { Clarification } from "./Clarification";
import { Timeline } from "./Timeline";

const STATUS_GLYPH: Record<string, string> = {
  planned: "○",
  succeeded: "✓",
  failed: "✗",
  blocked: "⛔",
  cancelled: "–",
};

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
                {
                  label:
                    task.user_retries < 2
                      ? `Retry (${2 - task.user_retries} left before AI rebuild)`
                      : "Retry",
                  run: () => api.resolveIncident(task.run_id, "retry"),
                },
                { label: "Abort", run: () => api.resolveIncident(task.run_id, "abort") },
              ]}
              onDone={(t) => {
                setTask(t);
                onChanged();
              }}
            />
          )}

          {task.plan && <Plan plan={task.plan} />}

          {task.failure && task.failure.node_label && (
            <div className="failure-node">
              Failed at node <strong>{task.failure.node_label}</strong>
              {task.failure.error ? <>: {task.failure.error}</> : null}
              {task.user_retries > 0 && (
                <span className="retry-count">
                  {" "}
                  ({task.user_retries} {task.user_retries === 1 ? "retry" : "retries"}{" "}
                  so far)
                </span>
              )}
            </div>
          )}

          {task.failure?.rebuild_refusal && (
            <div className="hint">{task.failure.rebuild_refusal}</div>
          )}
          {task.autobuild?.hint && !task.failure?.rebuild_refusal && (
            <div className="hint">{task.autobuild.hint}</div>
          )}

          {task.no_route && (
            <div className="no-route">
              <div className="no-route-title">
                OoLu could not find a route to execute this.
              </div>
              <div>{task.no_route.reason}</div>
              {task.no_route.unresolved_terms.length > 0 && (
                <div>
                  Nothing to search from for:{" "}
                  {task.no_route.unresolved_terms.join(", ")}
                </div>
              )}
              {task.no_route.candidates.length > 0 && (
                <ul>
                  {task.no_route.candidates.map((c) => (
                    <li key={c.name}>
                      <strong>{c.name}</strong>
                      {c.reason ? <> — {c.reason}</> : null}
                    </li>
                  ))}
                </ul>
              )}
            </div>
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

// How OoLu planned the steps: the chosen route as an ordered node list with
// live statuses; the exact failing node is marked. An LLM-rebuilt route is
// badged and shows the model's own numbered plan.
function Plan({ plan }: { plan: PlanView }) {
  return (
    <div className="plan">
      <div className="plan-head">
        <span className="plan-route">Route: {plan.route}</span>
        {plan.origin === "llm_rebuild" && (
          <span className="plan-badge">AI rebuild</span>
        )}
      </div>
      {plan.notes.length > 0 && (
        <ol className="plan-notes">
          {plan.notes.map((n, i) => (
            <li key={i}>{n}</li>
          ))}
        </ol>
      )}
      <ol className="plan-steps">
        {plan.steps.map((s) => (
          <li
            key={s.id}
            className={`plan-step plan-step-${s.status}${s.failed ? " plan-step-culprit" : ""}`}
          >
            <span className="plan-glyph">{STATUS_GLYPH[s.status] ?? "•"}</span>
            <span className="plan-label">{s.label}</span>
            {s.failed && <span className="plan-fail-tag">failed here</span>}
            {s.error && <span className="plan-error">{s.error}</span>}
          </li>
        ))}
      </ol>
    </div>
  );
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
