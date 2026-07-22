import { useEffect, useRef, useState } from "react";
import { api } from "../api";
import type { ChatAction, WorkNode } from "../api";
import { identityHue } from "../avatar";
import { actionLabel, Reasoning } from "./Chat";
import { ForwardMenu } from "./ForwardMenu";
import { loadCompose, saveCompose, t, tf, useT } from "../ui";

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
  | {
      kind: "assistant";
      text: string;
      actions?: ChatAction[];
      // The model's own thinking behind this reply, when it showed it.
      reasoning?: string;
    };

// The node's profile photo: its stable identity color and initial. While
// the model reasons it breathes with a glowing light — the user sees the
// node is still working on it, not hung.
export function NodeFace({
  title,
  thinking,
}: {
  title: string;
  thinking?: boolean;
}) {
  return (
    <span
      className={`convo-avatar node-face${thinking ? " thinking" : ""}`}
      style={{ background: `hsl(${identityHue(title)} 45% 34%)` }}
      role="img"
      aria-label={thinking ? t("interact.thinking") : title}
    >
      {(title.trim()[0] || "?").toUpperCase()}
    </span>
  );
}

export function reliabilityLine(node: WorkNode): string {
  const health = node.health;
  const verified = health.verified_successes + health.verified_failures;
  if (health.score === null || verified === 0) {
    return t("interact.reliabilityNone");
  }
  return tf("interact.reliability", {
    pct: (health.score * 100).toFixed(1),
    n: verified,
    runs: verified === 1 ? t("interact.runOne") : t("interact.runMany"),
  });
}

export function NodeInteract({ node }: { node: WorkNode }) {
  const tr = useT();
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
  // Unsent words survive leaving to another conversation window: one
  // compose slot per node, same store as a friend thread's.
  const [draft, setDraft] = useState(() => loadCompose(`node:${node.node_id}`));
  const [busy, setBusy] = useState(false);
  const endRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    saveCompose(`node:${node.node_id}`, draft);
  }, [node.node_id, draft]);

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
        {
          kind: "assistant",
          text: turn.reply,
          actions: turn.actions,
          reasoning: turn.reasoning || undefined,
        },
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
          <div className="muted">{tr("interact.hint")}</div>
        )}
        {thread.map((m, i) => (
          <div key={i} className={`bubble ${m.kind}`}>
            {m.kind === "assistant" && m.reasoning && (
              <Reasoning text={m.reasoning} />
            )}
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
        {busy && (
          <div className="bubble assistant thinking-bubble">
            <NodeFace title={node.title} thinking />
            <span className="thinking-note">{tr("interact.thinking")}</span>
          </div>
        )}
        <div ref={endRef} />
      </div>

      <div className="chat-composer">
        <textarea
          placeholder={tf("interact.messageAbout", { name: node.title })}
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
          {busy ? "…" : tr("send")}
        </button>
      </div>
    </div>
  );
}
