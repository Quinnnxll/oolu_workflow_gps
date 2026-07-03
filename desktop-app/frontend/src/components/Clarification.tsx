import { useState } from "react";
import { api } from "../api";
import type { TaskView } from "../types";

interface Props {
  task: TaskView;
  onResolved: (t: TaskView) => void;
}

export function Clarification({ task, onResolved }: Props) {
  const [answers, setAnswers] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState(false);

  const complete = task.questions.every((q) => (answers[q.parameter] ?? "").trim());

  return (
    <div className="clarify">
      {task.prompt && <p className="prompt">{task.prompt}</p>}
      {task.questions.map((q) => (
        <label key={q.parameter} className="field">
          <span>{q.question}</span>
          <input
            list={`sv-${q.parameter}`}
            value={answers[q.parameter] ?? ""}
            onChange={(e) =>
              setAnswers((a) => ({ ...a, [q.parameter]: e.target.value }))
            }
          />
          {q.suggested_values.length > 0 && (
            <datalist id={`sv-${q.parameter}`}>
              {q.suggested_values.map((v) => (
                <option key={String(v)} value={String(v)} />
              ))}
            </datalist>
          )}
        </label>
      ))}
      <button
        disabled={!complete || busy}
        onClick={async () => {
          setBusy(true);
          try {
            onResolved(await api.answer(task.run_id, answers));
          } finally {
            setBusy(false);
          }
        }}
      >
        {busy ? "Sending…" : "Answer"}
      </button>
    </div>
  );
}
