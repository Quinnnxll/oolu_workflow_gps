import { useEffect, useRef, useState } from "react";
import { api } from "../api";
import type {
  ChatAction,
  ChatHistoryTurn,
  ChatTurnBlock,
  WorkNode,
} from "../api";
import { identityHue } from "../avatar";
import {
  actionLabel,
  Reasoning,
  replyLanded,
  SecretFormBlock,
  serverStillWorking,
  watchRunReport,
} from "./Chat";
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
      // The secret form (V2): an in-node build against a keyed API
      // asks for its credential right here, masked, never chat text.
      block?: ChatTurnBlock | null;
    };

// The node's thread now lives on the SERVER (V0) under agent
// "node:<id>" — leaving the window mid-turn no longer discards the
// eventual reply. localStorage stays as the instant-paint cache (and
// the whole story on history-less hosts). Run markers and working
// markers are presence, not messages.
function nodeThreadFromServer(items: ChatHistoryTurn[]): Msg[] {
  return items
    .filter((turn) => turn.kind === "user" || turn.kind === "assistant")
    .map((turn): Msg => ({
      kind: turn.kind as "user" | "assistant",
      text: turn.body,
      block: turn.block?.kind === "secret_form" ? turn.block : null,
    }));
}

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
  // The SERVER's working state (V0): a durable marker under this
  // node's thread means the backend is still on a turn — the indicator
  // derives from it, so returning mid-turn shows the work, then the
  // reply.
  const [serverWorking, setServerWorking] = useState(false);
  const endRef = useRef<HTMLDivElement>(null);
  // The standing run-report watch (V1): a queued run's finish lands as
  // a report turn in this node's server thread — the watch folds it in.
  const reportWatchRef = useRef<(() => void) | null>(null);
  useEffect(() => () => reportWatchRef.current?.(), []);

  useEffect(() => {
    saveCompose(`node:${node.node_id}`, draft);
  }, [node.node_id, draft]);

  useEffect(() => {
    localStorage.setItem(storageKey, JSON.stringify(thread));
    endRef.current?.scrollIntoView?.({ block: "end" });
  }, [thread, storageKey]);

  // On mount, the host's record replaces the cache — the reply a
  // previous visit never waited for is HERE now. History-less hosts
  // 404 and the cache stands, exactly as before.
  useEffect(() => {
    let cancelled = false;
    void api
      .chatHistory(`node:${node.node_id}`)
      .then(({ items }) => {
        if (cancelled) return;
        if (items && items.length > 0)
          setThread(nodeThreadFromServer(items));
        setServerWorking(serverStillWorking(items ?? []));
      })
      .catch(() => {});
    return () => {
      cancelled = true;
    };
  }, [node.node_id]);

  // While the server says "working", poll until the reply (or the
  // honest interruption note) resolves the marker.
  useEffect(() => {
    if (!serverWorking) return;
    const t = setInterval(async () => {
      try {
        const { items } = await api.chatHistory(`node:${node.node_id}`);
        if (!serverStillWorking(items ?? [])) {
          if (items && items.length > 0)
            setThread(nodeThreadFromServer(items));
          setServerWorking(false);
        }
      } catch {
        /* the host answers the next poll — the bubble stands */
      }
    }, 2500);
    return () => clearInterval(t);
  }, [serverWorking, node.node_id]);

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
          block: turn.block?.kind === "secret_form" ? turn.block : null,
        },
      ]);
      // A queued run (V1): watch for the worker's report turn and fold
      // it into the thread when the run settles.
      if (turn.run_id) {
        reportWatchRef.current?.();
        reportWatchRef.current = watchRunReport(
          turn.run_id,
          `node:${node.node_id}`,
          (items) => setThread(nodeThreadFromServer(items)),
        );
      }
    } catch (e) {
      // The request broke — the server's record decides what shows
      // (V0): a standing marker keeps the bubble, a landed reply shows
      // itself, and only a turn the host is NOT working and never
      // answered earns the apology.
      try {
        const { items } = await api.chatHistory(`node:${node.node_id}`);
        const turns = items ?? [];
        if (turns.length > 0) setThread(nodeThreadFromServer(turns));
        if (serverStillWorking(turns)) {
          setServerWorking(true);
        } else if (!replyLanded(turns, text)) {
          setThread((t) => [
            ...t,
            {
              kind: "assistant",
              text: `Sorry — that didn't go through (${(e as Error).message}).`,
            },
          ]);
        }
      } catch {
        setThread((t) => [
          ...t,
          {
            kind: "assistant",
            text:
              "I lost the connection to the host — your ask may still " +
              `be running there (${(e as Error).message}).`,
          },
        ]);
      }
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
            {m.kind === "assistant" && m.block?.kind === "secret_form" && (
              <SecretFormBlock block={m.block} />
            )}
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
        {(busy || serverWorking) && (
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
