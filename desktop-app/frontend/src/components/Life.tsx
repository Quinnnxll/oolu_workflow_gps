import { useCallback, useEffect, useRef, useState } from "react";
import { api, TERMINAL_PHASES } from "../api";
import type {
  FriendConversation,
  FriendMessage,
  RepresentativeDraft,
} from "../api";
import { identityHue, updateAvatarSignals } from "../avatar";
import { useT } from "../ui";
import { conciseName } from "../naming";
import type { RunSummary, TimelineEvent } from "../types";
import { ForwardMenu } from "./ForwardMenu";
import { Chat } from "./Chat";
import { FilesPane } from "./FilesPane";
import { SettingsPane } from "./SettingsPane";
import { Work } from "./Work";

// The Life environment: a messenger. The left pane lists who you can talk
// to — OoLu (the assistant, full function access), Friends (people and
// legal entities), and Noder (one log thread per node interaction) — and
// the right pane is the open conversation. Work is the same architecture
// over a separate environment; it ships in the next build.

type Selection =
  | { kind: "oolu" }
  | { kind: "files" }
  | { kind: "settings" }
  | { kind: "friends" } // the start-a-conversation pane
  | { kind: "friend"; peer: string }
  | { kind: "noder"; run: RunSummary };

const GROUPS_KEY = "oolu_groups_open";

function loadGroups(): { friends: boolean; noder: boolean } {
  try {
    const raw = localStorage.getItem(GROUPS_KEY);
    const parsed = raw ? JSON.parse(raw) : {};
    return { friends: parsed.friends !== false, noder: parsed.noder !== false };
  } catch {
    return { friends: true, noder: true };
  }
}

export function Life() {
  const tr = useT(); // the chrome follows app.language live
  const [mode, setMode] = useState<"life" | "work">("life");
  const [selected, setSelected] = useState<Selection>({ kind: "oolu" });
  const [runs, setRuns] = useState<RunSummary[]>([]);
  // null = this host has no friends door (no server); [] = nobody yet.
  const [friends, setFriends] = useState<FriendConversation[] | null>(null);
  // Long lists fold away for a clear view; the choice survives restarts.
  const [groups, setGroups] = useState(loadGroups);

  function toggleGroup(name: "friends" | "noder") {
    setGroups((g) => {
      const next = { ...g, [name]: !g[name] };
      localStorage.setItem(GROUPS_KEY, JSON.stringify(next));
      return next;
    });
  }

  const refreshRuns = useCallback(async () => {
    try {
      const items = (await api.runs()).items;
      setRuns(items);
      // The avatar carries the workload on its face.
      updateAvatarSignals({
        workload: items.filter((r) => !TERMINAL_PHASES.includes(r.phase)).length,
      });
    } catch {
      setRuns([]);
    }
    try {
      setFriends((await api.friends()).items ?? []);
    } catch {
      setFriends(null); // no server door here: the group says so instead
    }
  }, []);

  useEffect(() => {
    void refreshRuns();
    const t = setInterval(refreshRuns, 5000);
    return () => clearInterval(t);
  }, [refreshRuns]);

  if (mode === "work") {
    return <Work onLife={() => setMode("life")} />;
  }

  return (
    <div className="life">
      <aside className="convo-list">
        <div className="mode-tabs">
          <button className="on">{tr("life")}</button>
          <button onClick={() => setMode("work")}>{tr("work")}</button>
        </div>

        <button
          className={`convo ${selected.kind === "oolu" ? "on" : ""}`}
          onClick={() => setSelected({ kind: "oolu" })}
        >
          <span className="convo-avatar oolu">O</span>
          <span className="convo-body">
            <span className="convo-name">OoLu</span>
            <span className="convo-sub">{tr("assistantSub")}</span>
          </span>
        </button>

        <button
          className={`convo ${selected.kind === "files" ? "on" : ""}`}
          onClick={() => setSelected({ kind: "files" })}
        >
          <span className="convo-avatar file">≡</span>
          <span className="convo-body">
            <span className="convo-name">{tr("files")}</span>
            <span className="convo-sub">{tr("filesSub")}</span>
          </span>
        </button>

        {/* Settings sits right below Files, above the conversations — a
            long Friends/Noder list must never hide it below the fold. */}
        <button
          className={`convo ${selected.kind === "settings" ? "on" : ""}`}
          onClick={() => setSelected({ kind: "settings" })}
        >
          <span className="convo-avatar file">⚙</span>
          <span className="convo-body">
            <span className="convo-name">{tr("settings")}</span>
            <span className="convo-sub">{tr("settingsSub")}</span>
          </span>
        </button>

        <button
          className="convo-group toggle"
          aria-expanded={groups.friends}
          onClick={() => toggleGroup("friends")}
        >
          {groups.friends ? "▾" : "▸"} {tr("friends")}
          {friends && friends.some((f) => f.unread > 0)
            ? ` (${friends.reduce((n, f) => n + f.unread, 0)})`
            : ""}
        </button>
        {groups.friends &&
          (friends ?? []).map((f) => (
            <button
              key={f.peer}
              className={`convo ${
                selected.kind === "friend" && selected.peer === f.peer
                  ? "on"
                  : ""
              }`}
              onClick={() => setSelected({ kind: "friend", peer: f.peer })}
            >
              <span
                className="convo-avatar"
                style={{
                  background: `hsl(${identityHue(f.peer)} 45% 34%)`,
                  color: "#fff",
                  borderColor: "transparent",
                }}
              >
                {f.peer.slice(0, 1).toUpperCase()}
              </span>
              <span className="convo-body">
                <span className="convo-name">
                  {f.peer}
                  {f.unread > 0 ? ` · ${f.unread} new` : ""}
                </span>
                <span className="convo-sub">
                  {f.last_from === f.peer ? "" : "you: "}
                  {f.last_text.slice(0, 40)}
                </span>
              </span>
            </button>
          ))}
        {groups.friends && (
          <button
            className={`convo ${selected.kind === "friends" ? "on" : ""}`}
            onClick={() => setSelected({ kind: "friends" })}
          >
            <span className="convo-avatar">+</span>
            <span className="convo-body">
              <span className="convo-sub">
                {friends === null
                  ? tr("friendsNeedServer")
                  : friends.length === 0
                    ? tr("startConversation")
                    : tr("newConversation")}
              </span>
            </span>
          </button>
        )}

        <button
          className="convo-group toggle"
          aria-expanded={groups.noder}
          onClick={() => toggleGroup("noder")}
        >
          {groups.noder ? "▾" : "▸"} {tr("noder")}
          {runs.length > 0 ? ` (${runs.length})` : ""}
        </button>
        {groups.noder && runs.length === 0 && (
          <div className="convo-empty">{tr("nodeActivityHere")}</div>
        )}
        {groups.noder &&
          runs.map((r) => (
          <button
            key={r.run_id}
            className={`convo ${
              selected.kind === "noder" && selected.run.run_id === r.run_id
                ? "on"
                : ""
            }`}
            title={r.intent}
            onClick={() => setSelected({ kind: "noder", run: r })}
          >
            <span
              className="convo-avatar node"
              style={{
                background: `hsl(${identityHue(r.intent)} 45% 34%)`,
                color: "#fff",
                borderColor: "transparent",
              }}
            >
              {conciseName(r.intent).slice(0, 1).toUpperCase()}
            </span>
            <span className="convo-body">
              {/* A name is a label, not a transcript: the keywords name
                  the thread; the full request lives in the tooltip and
                  the thread itself. */}
              <span className="convo-name">{conciseName(r.intent)}</span>
              <span className="convo-sub">{r.awaiting ?? r.phase}</span>
            </span>
          </button>
        ))}

      </aside>

      <section className="convo-pane">
        {selected.kind === "oolu" && <Chat />}
        {selected.kind === "friends" &&
          (friends === null ? (
            <div className="pane-empty">
              <p>Conversations with people and businesses will live here.</p>
              <p className="muted">
                Friends arrive with a server — OoLu Global, or your own private
                network server signed in from Edge.
              </p>
            </div>
          ) : (
            <StartConversation
              onOpen={(peer) => {
                setSelected({ kind: "friend", peer });
                void refreshRuns();
              }}
            />
          ))}
        {selected.kind === "friend" && (
          <FriendThread
            key={selected.peer}
            peer={selected.peer}
            onActivity={refreshRuns}
          />
        )}
        {selected.kind === "noder" && (
          <NoderThread key={selected.run.run_id} run={selected.run} />
        )}
        {selected.kind === "files" && <FilesPane />}
        {selected.kind === "settings" && <SettingsPane />}
      </section>
    </div>
  );
}

// A node's side of the story: the raw audit log as a message history.
// Deliberately unpolished — this is developer material. Everyone else asks
// OoLu, who can read these logs and re-trigger the node on their behalf.
export function NoderThread({
  run,
}: {
  run: RunSummary;
}) {
  const [events, setEvents] = useState<TimelineEvent[] | null>(null);

  useEffect(() => {
    let cancelled = false;
    api
      .timeline(run.run_id)
      .then((t) => {
        if (!cancelled) setEvents(t.items);
      })
      .catch(() => {
        if (!cancelled) setEvents([]);
      });
    return () => {
      cancelled = true;
    };
  }, [run.run_id]);

  // The Noder view is a RECORD, not a control panel: no buttons here.
  // Re-running is OoLu's job — asked in the chat, it re-fires the SAME
  // task through its own route and node, never a stray duplicate from
  // a button.
  return (
    <div className="noder-thread">
      <div className="noder-head">
        <div>
          <div className="run-card-intent">{conciseName(run.intent)}</div>
          <div className="muted">“{run.intent}”</div>
          <div className="muted">
            {run.run_id} · {run.awaiting ?? run.phase}
          </div>
        </div>
      </div>

      <div className="noder-log">
        {events === null && <div className="muted">Loading log…</div>}
        {events !== null && events.length === 0 && (
          <div className="muted">No log entries recorded.</div>
        )}
        {events?.map((e, i) => (
          <div key={i} className="log-line">
            <span className="log-at">{e.at}</span>
            <span className="log-label">{e.label}</span>
            {e.detail && <span className="log-detail">{e.detail}</span>}
          </div>
        ))}
      </div>

      <p className="muted noder-hint">
        Raw node log — a record, not a control panel. To run this again,
        ask OoLu in the chat (“run again {conciseName(run.intent)}”): it
        re-fires the same task through its own route and node.
      </p>
    </div>
  );
}

// Start a conversation: you address a person by their EXACT username or
// e-mail — there is no directory to browse (a public host holds strangers).
export function StartConversation({
  onOpen,
}: {
  onOpen: (peer: string) => void;
}) {
  const [query, setQuery] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  async function find() {
    setError("");
    setBusy(true);
    try {
      const { username } = await api.friendLookup(query.trim());
      onOpen(username);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="pane-empty start-conversation">
      <p>Who do you want to talk to?</p>
      <p className="muted">
        Enter their exact username or e-mail — there is no directory to
        browse, so nobody finds you unless you gave them your name.
      </p>
      <form
        className="setting-control row"
        onSubmit={(e) => {
          e.preventDefault();
          void find();
        }}
      >
        <input
          aria-label="Username or e-mail"
          placeholder="username or e-mail"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
        />
        <button type="submit" disabled={busy || !query.trim()}>
          {busy ? "Looking…" : "Find"}
        </button>
      </form>
      {error && <div className="error">{error}</div>}
    </div>
  );
}

// One person's thread. Opening it marks their messages read (the server
// does that on GET); a short poll keeps both sides fresh — same rhythm as
// the run list.
export function FriendThread({
  peer,
  onActivity,
}: {
  peer: string;
  onActivity: () => void;
}) {
  const [messages, setMessages] = useState<FriendMessage[] | null>(null);
  const [draft, setDraft] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  // The representative: ✍ appears only when the account turned it on.
  const [repOn, setRepOn] = useState(false);
  const [suggestion, setSuggestion] = useState<RepresentativeDraft | null>(
    null,
  );
  // Set while the composer holds a draft being edited: the send button
  // then decides the draft (recording the edit) instead of a plain send.
  const [editingDraft, setEditingDraft] = useState<string | null>(null);
  const endRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    api
      .representative()
      .then((s) => setRepOn(s.mode !== "off"))
      .catch(() => setRepOn(false)); // no door on this host: no button
  }, []);

  const refresh = useCallback(async () => {
    try {
      setMessages((await api.friendMessages(peer)).items);
    } catch (e) {
      setError((e as Error).message);
    }
  }, [peer]);

  useEffect(() => {
    void refresh();
    const t = setInterval(refresh, 4000);
    return () => clearInterval(t);
  }, [refresh]);

  useEffect(() => {
    endRef.current?.scrollIntoView?.({ block: "end" });
  }, [messages?.length]);

  async function send() {
    const text = draft.trim();
    if (!text) return;
    setError("");
    setBusy(true);
    try {
      if (editingDraft) {
        // An edited suggestion goes through its decision — the outcome
        // (and the rewrite itself) is what teaches the representative.
        await api.decideRepresentativeDraft(editingDraft, "edit", text);
        setEditingDraft(null);
        setSuggestion(null);
        await refresh();
      } else {
        const sent = await api.sendFriendMessage(peer, text);
        setMessages((m) => [...(m ?? []), sent]);
      }
      setDraft("");
      onActivity();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  async function suggest() {
    setError("");
    setBusy(true);
    try {
      setSuggestion(await api.representativeDraft(peer));
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  async function decide(action: "send" | "discard") {
    if (!suggestion) return;
    setError("");
    setBusy(true);
    try {
      await api.decideRepresentativeDraft(suggestion.draft_id, action);
      setSuggestion(null);
      if (action === "send") {
        await refresh();
        onActivity();
      }
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="chat friend-thread">
      <div className="chat-thread">
        {messages === null && <div className="muted">Loading…</div>}
        {messages !== null && messages.length === 0 && (
          <div className="pane-empty">
            <p>
              Say hello — this is the start of your conversation with {peer}.
            </p>
          </div>
        )}
        {messages?.map((m) => (
          <div key={m.message_id} className={`bubble ${m.mine ? "user" : "assistant"}`}>
            {m.text}
            {m.file_id && (
              <span className="badge" title="attached file">
                📄 file
              </span>
            )}
            <ForwardMenu text={m.text} from={m.mine ? "me" : peer} />
          </div>
        ))}
        <div ref={endRef} />
      </div>
      {error && <div className="error">{error}</div>}
      {suggestion && !editingDraft && (
        <div className="rep-suggestion">
          <div className="muted">Your representative drafted:</div>
          <div className="bubble user">{suggestion.generated_text}</div>
          <div className="setting-control row">
            <button
              aria-label="Send drafted reply"
              disabled={busy}
              onClick={() => void decide("send")}
            >
              Send
            </button>
            <button
              disabled={busy}
              onClick={() => {
                setDraft(suggestion.generated_text);
                setEditingDraft(suggestion.draft_id);
              }}
            >
              Edit
            </button>
            <button
              className="linklike"
              disabled={busy}
              onClick={() => void decide("discard")}
            >
              Discard
            </button>
          </div>
        </div>
      )}
      {editingDraft && (
        <div className="muted">
          Editing the drafted reply — Send records your version.
        </div>
      )}
      <div className="chat-composer">
        <textarea
          aria-label={`Message ${peer}`}
          placeholder={`Message ${peer}…`}
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
        {repOn && !suggestion && !editingDraft && (
          <button
            title="Draft a reply in your voice"
            aria-label="Draft a reply in your voice"
            disabled={busy || messages === null || messages.length === 0}
            onClick={() => void suggest()}
          >
            ✍
          </button>
        )}
        <button disabled={busy || !draft.trim()} onClick={() => void send()}>
          {busy ? "…" : editingDraft ? "Send edited" : "Send"}
        </button>
      </div>
    </div>
  );
}
