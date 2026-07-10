import { useEffect, useRef, useState } from "react";
import { api } from "../api";
import type { ChatAction, WorkNode } from "../api";
import { actionLabel } from "./Chat";
import { ForwardMenu } from "./ForwardMenu";

// The node's interaction window: call OoLu out to act ON THIS NODE —
// accelerate its pending work, reply to requesters, sign final results
// (the manual audit floor), and build execution nodes on its path. The
// automation-reliability line is the vision made visible: every verified
// run takes an audit node closer to hands-off, and when automation fails
// the error code lands here for the user to fix later.

type Msg =
  | { kind: "user"; text: string }
  | { kind: "assistant"; text: string; actions?: ChatAction[] };

const QUICK: { label: string; message: string; fills?: boolean }[] = [
  { label: "Pending requests", message: "pending" },
  { label: "Accelerate", message: "accelerate" },
  { label: "Sign all…", message: "sign all as ", fills: true },
  { label: "Build a node…", message: "build ", fills: true },
];

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
      <p className="muted">{reliabilityLine(node)}</p>

      <div className="quickstarts">
        {QUICK.map((q) => (
          <button
            key={q.label}
            className="quickstart"
            onClick={() => {
              if (q.fills) setDraft(q.message);
              else void send(q.message);
            }}
          >
            {q.label}
          </button>
        ))}
      </div>

      <div className="chat-thread node-interact-thread">
        {thread.length === 0 && (
          <div className="muted">
            Ask OoLu to act on this node — “pending”, “sign all as &lt;your
            name&gt;”, “reply &lt;request&gt;: &lt;message&gt;”, or “build
            &lt;what's missing&gt;”.
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
