import { useEffect, useRef, useState } from "react";
import { api } from "../api";
import type { RosterAgent } from "../api";
import { identityHue } from "../avatar";
import { loadCompose, saveCompose, tf, useT } from "../ui";

// A roster agent's conversation (agents-expansion plan A0): the same
// messenger shape as the OoLu chat, deliberately leaner — words only. No
// runs, no tools, no reminders: those are OoLu's; an agent that needs
// hands earns them in its own phase, through its own seat. The thread is
// the server's (per-account, per-agent); localStorage stays the warm
// cache exactly like the OoLu chat.

type AgentMsg = {
  kind: "user" | "assistant";
  text: string;
  reasoning?: string | null;
};

const cacheKey = (agent: string) => `oolu_agent_${agent}`;

function loadCached(agent: string): AgentMsg[] {
  try {
    const parsed = JSON.parse(localStorage.getItem(cacheKey(agent)) ?? "[]");
    return Array.isArray(parsed) ? parsed : [];
  } catch {
    return [];
  }
}

// The agent's face in the sidebar and the thread head: a generated color
// from its id — stable, no asset to ship.
export function AgentAvatar({
  agent,
  size = 36,
}: {
  agent: RosterAgent;
  size?: number;
}) {
  return (
    <span
      className="convo-avatar agent"
      style={{
        background: `hsl(${identityHue(agent.agent_id)} 45% 34%)`,
        color: "#fff",
        borderColor: "transparent",
        width: size,
        height: size,
      }}
    >
      {agent.name.slice(0, 1).toUpperCase()}
    </span>
  );
}

export function AgentThread({ agent }: { agent: RosterAgent }) {
  const tr = useT();
  const [thread, setThread] = useState<AgentMsg[]>(() =>
    loadCached(agent.agent_id),
  );
  const [draft, setDraft] = useState(() =>
    loadCompose(`agent-${agent.agent_id}`),
  );
  const [busy, setBusy] = useState(false);
  const endRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    localStorage.setItem(cacheKey(agent.agent_id), JSON.stringify(thread));
    endRef.current?.scrollIntoView?.({ block: "end" });
  }, [thread, agent.agent_id]);

  useEffect(() => {
    saveCompose(`agent-${agent.agent_id}`, draft);
  }, [draft, agent.agent_id]);

  // On mount, the host's thread replaces the warm cache (another device
  // may have talked since); history-less hosts 404 and the cache stands.
  useEffect(() => {
    void api
      .chatHistory(agent.agent_id)
      .then(({ items }) => {
        if (items && items.length > 0) {
          setThread(
            items
              .filter((t) => t.kind === "user" || t.kind === "assistant")
              .map((t): AgentMsg => ({ kind: t.kind as "user" | "assistant", text: t.body })),
          );
        }
      })
      .catch(() => {});
  }, [agent.agent_id]);

  async function send() {
    const message = draft.trim();
    if (!message || busy) return;
    setDraft("");
    setThread((t) => [...t, { kind: "user", text: message }]);
    setBusy(true);
    try {
      const history = thread.map((m) => ({
        role: m.kind,
        content: m.text,
      }));
      const turn = await api.chat(
        message,
        history,
        undefined,
        undefined,
        agent.agent_id,
      );
      setThread((t) => [
        ...t,
        { kind: "assistant", text: turn.reply, reasoning: turn.reasoning },
      ]);
    } catch (e) {
      setThread((t) => [
        ...t,
        {
          kind: "assistant",
          text: e instanceof Error ? e.message : tr("agent.sendFailed"),
        },
      ]);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="chat">
      <div className="chat-head">
        <AgentAvatar agent={agent} size={64} />
        <div className="chat-head-body">
          <div className="chat-head-name">{agent.name}</div>
          <div className="chat-head-sub">{agent.tagline}</div>
        </div>
      </div>
      <div className="chat-thread">
        {/* The card welcomes honestly: what this agent does today, and
            what phase brings the rest — the same words the server would
            answer with, shown before the first message is ever sent. */}
        {thread.length === 0 && (
          <div className="bubble assistant">
            {agent.scope} {agent.ahead}
          </div>
        )}
        {thread.map((m, i) => (
          <div key={i} className={`bubble ${m.kind}`}>
            {m.text}
          </div>
        ))}
        {busy && (
          <div className="bubble assistant thinking-bubble">
            <span className="thinking-dot" aria-hidden="true" />
            <span className="thinking-note">{tr("interact.thinking")}</span>
          </div>
        )}
        <div ref={endRef} />
      </div>
      <div className="chat-composer">
        <textarea
          placeholder={tf("agent.message", { name: agent.name })}
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
        <button disabled={busy} aria-label="Send" onClick={() => void send()}>
          {busy ? "…" : tr("send")}
        </button>
      </div>
    </div>
  );
}
