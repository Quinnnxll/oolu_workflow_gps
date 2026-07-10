import { useEffect, useRef, useState } from "react";
import { api } from "../api";
import type { ChatAction, WorkNode } from "../api";
import { actionLabel } from "./Chat";
import { ForwardMenu } from "./ForwardMenu";

// The node's interaction window — a conversation, nothing else. The
// thread fills the pane and the composer sits under it; there is no
// button chrome and no banner text eating the space. Everything is a
// typed command OoLu answers deterministically: “pending” lists what
// waits (with each task's id), “sign <task id> as <your name>” passes a
// task to the next node, “reply <task id>: <message>” talks back, and
// “build <what's missing>” puts a new execution node on the path. The
// one hint line lives inside the empty thread and disappears with the
// first message.

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

export function NodeInteract({ node }: { node: WorkNode }) {
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

  return (
    <div className="node-interact">
      <div className="chat-thread node-interact-thread">
        {thread.length === 0 && (
          <div className="muted">
            Ask OoLu to act on this node — “pending” lists what waits,
            “sign &lt;task id&gt; as &lt;your name&gt;” passes a task to
            the next node, “reply &lt;task id&gt;: &lt;message&gt;”, or
            “build &lt;what's missing&gt;”.
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
