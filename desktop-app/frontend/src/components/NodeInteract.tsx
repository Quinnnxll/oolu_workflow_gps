import { useEffect, useRef, useState } from "react";
import { api } from "../api";
import type { ChatAction, HoldItem, WorkNode } from "../api";
import { actionLabel } from "./Chat";
import { ForwardMenu } from "./ForwardMenu";

// The node's interaction window: call OoLu out to act ON THIS NODE —
// reply to requesters, sign final results onward (the manual audit
// floor), and build execution nodes on its path. Acceleration is not a
// button: whatever can move automatically already moved — what remains
// here is exactly the work that needs a human, surfaced by itself as
// clickable task chips the moment the window opens. The automation-
// reliability line is the vision made visible: every verified run takes
// an audit node closer to hands-off.

type Msg =
  | { kind: "user"; text: string }
  | { kind: "assistant"; text: string; actions?: ChatAction[] };

export function reliabilityLine(node: WorkNode): string {
  const health = node.health;
  const verified = health.verified_successes + health.verified_failures;
  if (health.score === null || verified === 0) {
    return "Automation reliability: no verified runs yet — it grows with every task this node executes.";
  }
  return (
    `Automation reliability: ${(health.score * 100).toFixed(1)}% over ` +
    `${verified} verified ${verified === 1 ? "run" : "runs"} — every ` +
    "verified run takes this node closer to hands-off."
  );
}

export function NodeInteract({
  node,
  holds = [],
}: {
  node: WorkNode;
  // This node's held requests (the thread's desk already polls them):
  // each becomes a clickable task chip that fills the sign command.
  holds?: HoldItem[];
}) {
  const storageKey = `oolu_node_chat_${node.node_id}`;
  const [thread, setThread] = useState<Msg[]>(() => {
    try {
      const raw = localStorage.getItem(storageKey);
      const parsed = raw ? (JSON.parse(raw) as Msg[]) : [];
      return Array.isArray(parsed) ? parsed : [];
    } catch {
      return [];
    }
  });
  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState(false);
  const endRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    localStorage.setItem(storageKey, JSON.stringify(thread));
    endRef.current?.scrollIntoView?.({ block: "end" });
  }, [thread, storageKey]);

  async function send(message?: string) {
    const text = (message ?? draft).trim();
    if (!text || busy) return;
    setDraft("");
    setBusy(true);
    setThread((t) => [...t, { kind: "user", text }]);
    try {
      const history = thread.slice(-12).map((m) => ({
        role: m.kind === "user" ? ("user" as const) : ("assistant" as const),
        content: m.text,
      }));
      const turn = await api.chat(text, history, node.node_id);
      setThread((t) => [
        ...t,
        { kind: "assistant", text: turn.reply, actions: turn.actions },
      ]);
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

  // Sign fills the command WITH the task id when only one task waits;
  // with several, the chips below (or “pending”) hand over the id.
  function fillSign(pendingId?: string) {
    const id = pendingId ?? (holds.length === 1 ? holds[0].pending_id : "");
    setDraft(id ? `sign ${id.slice(0, 8)} as ` : "sign ");
  }

  return (
    <div className="node-interact">
      <p className="muted">{reliabilityLine(node)}</p>

      {/* One row: list what waits, sign a task onward, build what's
          missing. Acceleration is nobody's button — it already happened. */}
      <div className="quickstarts">
        <button className="quickstart" onClick={() => void send("pending")}>
          Pending
        </button>
        <button className="quickstart" onClick={() => fillSign()}>
          Sign
        </button>
        <button className="quickstart" onClick={() => setDraft("build ")}>
          Build
        </button>
      </div>

      {/* Whatever still needs a human surfaces by itself: each waiting
          task is a chip; tapping it drops its id into the sign command. */}
      {holds.length > 0 && (
        <div className="quickstarts hold-chips">
          <span className="muted">waiting:</span>
          {holds.map((h) => (
            <button
              key={h.pending_id}
              className="quickstart"
              title={`sign ${h.pending_id.slice(0, 8)} onward`}
              onClick={() => fillSign(h.pending_id)}
            >
              {h.name} · {h.pending_id.slice(0, 8)}
            </button>
          ))}
        </div>
      )}

      <div className="chat-thread node-interact-thread">
        {thread.length === 0 && (
          <div className="muted">
            Ask OoLu to act on this node — “pending”, “sign &lt;task id&gt;
            as &lt;your name&gt;” to pass a task to the next node, “reply
            &lt;request&gt;: &lt;message&gt;”, or “build &lt;what's
            missing&gt;”.
          </div>
        )}
        {thread.map((m, i) => (
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
            <ForwardMenu
              text={m.text}
              from={m.kind === "user" ? "me" : node.title}
            />
          </div>
        ))}
        <div ref={endRef} />
      </div>

      <div className="chat-composer">
        <textarea
          placeholder={`Message OoLu about ${node.title}…`}
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
        <button disabled={busy} onClick={() => void send()}>
          {busy ? "…" : "Send"}
        </button>
      </div>
    </div>
  );
}
