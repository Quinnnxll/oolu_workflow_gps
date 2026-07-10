import { useCallback, useEffect, useRef, useState } from "react";
import { api, TERMINAL_PHASES } from "../api";
import type { FriendConversation, FriendMessage } from "../api";
import { identityHue, updateAvatarSignals } from "../avatar";
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
          <button className="on">Life</button>
          <button onClick={() => setMode("work")}>Work</button>
        </div>

        <button
          className={`convo ${selected.kind === "oolu" ? "on" : ""}`}
          onClick={() => setSelected({ kind: "oolu" })}
        >
          <span className="convo-avatar oolu">O</span>
          <span className="convo-body">
            <span className="convo-name">OoLu</span>
            <span className="convo-sub">your assistant</span>
          </span>
        </button>

        <button
          className={`convo ${selected.kind === "files" ? "on" : ""}`}
          onClick={() => setSelected({ kind: "files" })}
        >
          <span className="convo-avatar file">≡</span>
          <span className="convo-body">
            <span className="convo-name">Files</span>
            <span className="convo-sub">documents & sheets</span>
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
            <span className="convo-name">Settings</span>
            <span className="convo-sub">app, account, model, budget</span>
          </span>
        </button>

        <button
          className="convo-group toggle"
          aria-expanded={groups.friends}
          onClick={() => toggleGroup("friends")}
        >
          {groups.friends ? "▾" : "▸"} Friends
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
                  ? "Friends need a server"
                  : friends.length === 0
                    ? "Start a conversation"
                    : "New conversation"}
              </span>
            </span>
          </button>
        )}

        <button
          className="convo-group toggle"
          aria-expanded={groups.noder}
          onClick={() => toggleGroup("noder")}
        >
          {groups.noder ? "▾" : "▸"} Noder
          {runs.length > 0 ? ` (${runs.length})` : ""}
        </button>
        {groups.noder && runs.length === 0 && (
          <div className="convo-empty">Node activity appears here.</div>
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
          <NoderThread
            key={selected.run.run_id}
            run={selected.run}
            onRunAgain={refreshRuns}
          />
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
  onRunAgain,
}: {
  run: RunSummary;
  onRunAgain: () => void;
}) {
  const [events, setEvents] = useState<TimelineEvent[] | null>(null);
  const [rerunning, setRerunning] = useState(false);
  const [notice, setNotice] = useState("");

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
        <button
          disabled={rerunning}
          onClick={async () => {
            setRerunning(true);
            setNotice("");
            try {
              await api.submitTask(run.intent);
              setNotice("Triggered again — the new interaction appears in the list.");
              onRunAgain();
            } catch (e) {
              setNotice(`Could not trigger: ${(e as Error).message}`);
            } finally {
              setRerunning(false);
            }
          }}
        >
          {rerunning ? "Triggering…" : "Run again"}
        </button>
      </div>
      {notice && <div className="muted">{notice}</div>}

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
        Raw node log — ask OoLu to review it or dig into it for you.
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
  const endRef = useRef<HTMLDivElement | null>(null);

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
      const sent = await api.sendFriendMessage(peer, text);
      setMessages((m) => [...(m ?? []), sent]);
      setDraft("");
      onActivity();
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
        <button disabled={busy || !draft.trim()} onClick={() => void send()}>
          {busy ? "…" : "Send"}
        </button>
      </div>
    </div>
  );
}
