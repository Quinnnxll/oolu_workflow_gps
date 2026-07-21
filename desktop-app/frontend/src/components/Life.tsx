import { useCallback, useEffect, useRef, useState } from "react";
import QRCode from "qrcode";
import jsQR from "jsqr";
import { api, session, TERMINAL_PHASES } from "../api";
import type {
  FriendConversation,
  FriendMessage,
  RepresentativeDraft,
  RepresentativeStatus,
} from "../api";
import { identityHue, updateAvatarSignals } from "../avatar";
import {
  loadCompose,
  loadSidebarFolded,
  saveCompose,
  saveSidebarFolded,
  tf,
  useT,
} from "../ui";
import { conciseName } from "../naming";
import { orderThreads } from "../conversations";
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
  | { kind: "drafts" } // the representative's inbox
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
  // null = no representative door (or it's off); the Drafts entry follows.
  const [rep, setRep] = useState<RepresentativeStatus | null>(null);
  // Long lists fold away for a clear view; the choice survives restarts.
  const [groups, setGroups] = useState(loadGroups);
  // The whole list folds away too — a wide, clear conversation window on
  // demand (persisted). On a phone the layout is one pane at a time:
  // paneOpen decides whether the list or the open conversation shows.
  const [folded, setFolded] = useState(loadSidebarFolded);
  const [paneOpen, setPaneOpen] = useState(false);

  function open(next: Selection) {
    setSelected(next);
    setPaneOpen(true);
  }

  // Closing a thread from inside (hide/delete in its profile): back to
  // the list with OoLu selected — the thread is gone from the sidebar.
  function closeThread() {
    setSelected({ kind: "oolu" });
    setPaneOpen(false);
    void refreshRuns();
  }

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
    try {
      const status = await api.representative();
      setRep(status);
      if (status.mode !== "off") {
        // The busy person's pass: draft a reply for every waiting friend
        // message. Idempotent per message — free until something is new.
        const swept = await api.representativeSweep();
        if (swept.drafted.length > 0 || (swept.waiting ?? 0) > 0) {
          setRep({
            ...status,
            drafts_pending: swept.pending,
            drafts_waiting: swept.waiting ?? 0,
          });
        }
        // A reply that needs the user's own knowledge: OoLu just asked in
        // the conversation (the history has it) — surface it live too.
        if (swept.asked) {
          window.dispatchEvent(
            new CustomEvent("oolu-rep-question", {
              detail: { text: swept.asked.text },
            }),
          );
        }
      }
    } catch {
      setRep(null); // no representative on this host: no Drafts entry
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
    <div
      className={`life${folded ? " sidebar-folded" : ""}${
        paneOpen ? " pane-open" : ""
      }`}
    >
      <aside className="convo-list">
        <button
          type="button"
          className="sidebar-toggle"
          aria-label={folded ? tr("nav.showList") : tr("nav.hideList")}
          title={folded ? tr("nav.showList") : tr("nav.hideList")}
          onClick={() => {
            setFolded((f) => {
              saveSidebarFolded(!f);
              return !f;
            });
          }}
        >
          {folded ? "☰" : "«"}
        </button>
        <div className="mode-tabs">
          <button className="on">{tr("life")}</button>
          <button onClick={() => setMode("work")}>{tr("work")}</button>
        </div>

        <button
          className={`convo ${selected.kind === "oolu" ? "on" : ""}`}
          onClick={() => open({ kind: "oolu" })}
        >
          <span className="convo-avatar oolu">O</span>
          <span className="convo-body">
            <span className="convo-name">OoLu</span>
            <span className="convo-sub">{tr("assistantSub")}</span>
          </span>
        </button>

        <button
          className={`convo ${selected.kind === "files" ? "on" : ""}`}
          onClick={() => open({ kind: "files" })}
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
          onClick={() => open({ kind: "settings" })}
        >
          <span className="convo-avatar file">⚙</span>
          <span className="convo-body">
            <span className="convo-name">{tr("settings")}</span>
            <span className="convo-sub">{tr("settingsSub")}</span>
          </span>
        </button>

        {rep !== null && rep.mode !== "off" && (
          <button
            className={`convo ${selected.kind === "drafts" ? "on" : ""}`}
            onClick={() => open({ kind: "drafts" })}
          >
            <span className="convo-avatar file">✍</span>
            <span className="convo-body">
              <span className="convo-name">
                {rep.drafts_pending > 0
                  ? tf("rep.draftsNew", { n: rep.drafts_pending })
                  : tr("rep.drafts")}
              </span>
              <span className="convo-sub">{tr("rep.draftsSub")}</span>
            </span>
          </button>
        )}

        <button
          className="convo-group toggle"
          aria-expanded={groups.friends}
          onClick={() => toggleGroup("friends")}
        >
          {groups.friends ? "▾" : "▸"} {tr("friends")}
          {/* Muted threads keep their words but stop their nagging: they
              never count toward the group's unread number. */}
          {friends && friends.some((f) => f.unread > 0 && !f.muted)
            ? ` (${friends.reduce((n, f) => n + (f.muted ? 0 : f.unread), 0)})`
            : ""}
        </button>
        {groups.friends &&
          orderThreads(friends ?? [], (f) => f.last_at || f.since || "").map(
            (f) => (
              <button
                key={f.peer}
                className={`convo ${
                  selected.kind === "friend" && selected.peer === f.peer
                    ? "on"
                    : ""
                }`}
                onClick={() => open({ kind: "friend", peer: f.peer })}
              >
                <span
                  className="convo-avatar"
                  style={{
                    background: `hsl(${identityHue(f.peer)} 45% 34%)`,
                    color: "#fff",
                    borderColor: "transparent",
                  }}
                >
                  {(f.alias || f.peer).slice(0, 1).toUpperCase()}
                </span>
                <span className="convo-body">
                  <span className="convo-name">
                    {f.pinned ? "📌 " : ""}
                    {f.alias || f.peer}
                    {f.muted
                      ? " 🔕"
                      : f.unread > 0
                        ? ` · ${f.unread} new`
                        : ""}
                  </span>
                  <span className="convo-sub">
                    {/* A fresh friendship has no words yet — invite them. */}
                    {f.last_text
                      ? `${f.last_from === f.peer ? "" : "you: "}${f.last_text.slice(0, 40)}`
                      : tr("friends.sayHello")}
                  </span>
                </span>
              </button>
            ),
          )}
        {groups.friends && (
          <button
            className={`convo ${selected.kind === "friends" ? "on" : ""}`}
            onClick={() => open({ kind: "friends" })}
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
          orderThreads(runs, (r) => r.updated_at ?? "").map((r) => (
          <button
            key={r.run_id}
            className={`convo ${
              selected.kind === "noder" && selected.run.run_id === r.run_id
                ? "on"
                : ""
            }`}
            title={r.intent}
            onClick={() => open({ kind: "noder", run: r })}
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
              <span className="convo-name">
                {r.pinned ? "📌 " : ""}
                {conciseName(r.intent)}
                {r.muted ? " 🔕" : ""}
              </span>
              <span className="convo-sub">{r.awaiting ?? r.phase}</span>
            </span>
          </button>
        ))}

      </aside>

      <section className="convo-pane">
        <div className="pane-bar">
          <button
            type="button"
            className="pane-back"
            aria-label={tr("nav.back")}
            onClick={() => setPaneOpen(false)}
          >
            ‹ {tr("nav.back")}
          </button>
        </div>
        {selected.kind === "oolu" &&
          rep !== null &&
          rep.mode !== "off" &&
          (rep.drafts_pending > 0 || (rep.drafts_waiting ?? 0) > 0) && (
            <div className="rep-sweep">
              <DraftsInbox
                onActivity={refreshRuns}
                onOpenThread={(peer) => open({ kind: "friend", peer })}
              />
            </div>
          )}
        {selected.kind === "oolu" && (
          <Chat
            headerAside={
              rep !== null && (
                // The representative toggle rides the OoLu-name row.
                <button
                  type="button"
                  className={`rep-quick${rep.mode !== "off" ? " on" : ""}`}
                  aria-label={
                    rep.mode !== "off" ? tr("rep.toggleOn") : tr("rep.toggleOff")
                  }
                  title={tr("rep.toggleHint")}
                  onClick={async () => {
                    try {
                      const next = rep.mode === "off" ? "draft" : "off";
                      const status = await api.configureRepresentative({
                        mode: next,
                      });
                      setRep(status);
                      if (next !== "off") {
                        // Toggling back ON forgives earlier discards: the
                        // still-unread messages get a fresh draft pass.
                        const swept = await api.representativeSweep();
                        setRep({
                          ...status,
                          drafts_pending: swept.pending,
                          drafts_waiting: swept.waiting ?? 0,
                        });
                        if (swept.asked) {
                          window.dispatchEvent(
                            new CustomEvent("oolu-rep-question", {
                              detail: { text: swept.asked.text },
                            }),
                          );
                        }
                      }
                    } catch {
                      /* the poll will tell the truth shortly */
                    }
                  }}
                >
                  {rep.mode !== "off" ? tr("rep.toggleOn") : tr("rep.toggleOff")}
                </button>
              )
            }
          />
        )}
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
                open({ kind: "friend", peer });
                void refreshRuns();
              }}
            />
          ))}
        {selected.kind === "friend" && (
          <FriendThread
            key={selected.peer}
            peer={selected.peer}
            alias={
              friends?.find((f) => f.peer === selected.peer)?.alias ?? ""
            }
            since={
              friends?.find((f) => f.peer === selected.peer)?.since ?? ""
            }
            pinned={
              friends?.find((f) => f.peer === selected.peer)?.pinned ?? false
            }
            muted={
              friends?.find((f) => f.peer === selected.peer)?.muted ?? false
            }
            onActivity={refreshRuns}
            onClosed={closeThread}
          />
        )}
        {selected.kind === "noder" && (
          <NoderThread
            key={selected.run.run_id}
            run={
              runs.find((r) => r.run_id === selected.run.run_id) ??
              selected.run
            }
            onActivity={refreshRuns}
            onClosed={closeThread}
          />
        )}
        {selected.kind === "files" && <FilesPane />}
        {selected.kind === "settings" && <SettingsPane />}
        {selected.kind === "drafts" && (
          <DraftsInbox
            onActivity={refreshRuns}
            onOpenThread={(peer) => open({ kind: "friend", peer })}
          />
        )}
      </section>
    </div>
  );
}

// The representative's inbox: every pending draft across conversations —
// including ones auto mode filed while unearned or gated. Each card is
// decided in place; deciding is the same door the thread's ✍ uses, so
// send delivers and an edit teaches.
export function DraftsInbox({
  onActivity,
  onOpenThread,
}: {
  onActivity: () => void;
  onOpenThread: (peer: string) => void;
}) {
  const tr = useT();
  const [drafts, setDrafts] = useState<RepresentativeDraft[] | null>(null);
  // Drafts waiting on information only the user can give: their
  // generated_text is OoLu's QUESTIONS, answered in the OoLu chat.
  const [waiting, setWaiting] = useState<RepresentativeDraft[]>([]);
  const [editing, setEditing] = useState<{ id: string; text: string } | null>(
    null,
  );
  const [note, setNote] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  const refresh = useCallback(async () => {
    try {
      const got = await api.representativeDrafts();
      setDrafts(got.items);
      setWaiting(got.waiting ?? []);
    } catch (e) {
      setError((e as Error).message);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  async function decide(draftId: string, action: string, text?: string) {
    setError("");
    setBusy(true);
    try {
      // A DISCARDED draft is kept, not buried: its words land in the
      // friend conversation's typing block, and the message is drafted
      // again when the peer writes anew, the representative is toggled
      // back on, or it still sits unread after a day.
      if (action === "discard") {
        const discarded = drafts?.find((d) => d.draft_id === draftId);
        if (discarded) {
          saveCompose(discarded.conversation_id, discarded.generated_text);
          setNote(tf("rep.discarded", { peer: discarded.conversation_id }));
        }
      }
      await api.decideRepresentativeDraft(draftId, action, text);
      setEditing(null);
      await refresh();
      onActivity();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="drafts-inbox">
      <div className="convo-group">{tr("rep.inboxTitle")}</div>
      {error && <div className="error">{error}</div>}
      {drafts === null && <div className="muted">Loading…</div>}
      {drafts !== null && drafts.length === 0 && (
        <div className="pane-empty">
          <p>{tr("rep.nothingWaiting")}</p>
          <p className="muted">{tr("rep.inboxIntro")}</p>
        </div>
      )}
      {drafts?.map((d) => (
        <div key={d.draft_id} className="settings-group draft-card">
          <div className="muted">
            <button
              className="linklike"
              onClick={() => onOpenThread(d.conversation_id)}
            >
              {tf("rep.answering", {
                peer: d.conversation_id,
                text: d.inbound_text,
              })}
            </button>
          </div>
          {editing?.id === d.draft_id ? (
            <>
              <textarea
                aria-label={`Edit draft to ${d.conversation_id}`}
                value={editing.text}
                rows={3}
                onChange={(e) =>
                  setEditing({ id: d.draft_id, text: e.target.value })
                }
              />
              <div className="setting-control row">
                <button
                  disabled={busy || !editing.text.trim()}
                  onClick={() => void decide(d.draft_id, "edit", editing.text)}
                >
                  {tr("rep.sendEdited")}
                </button>
                <button
                  className="linklike"
                  disabled={busy}
                  onClick={() => setEditing(null)}
                >
                  {tr("rep.cancel")}
                </button>
              </div>
            </>
          ) : (
            <>
              <div className="bubble user">{d.generated_text}</div>
              <div className="setting-control row">
                <button
                  aria-label={`Send draft to ${d.conversation_id}`}
                  disabled={busy}
                  onClick={() => void decide(d.draft_id, "send")}
                >
                  {tr("rep.send")}
                </button>
                <button
                  disabled={busy}
                  onClick={() =>
                    setEditing({ id: d.draft_id, text: d.generated_text })
                  }
                >
                  {tr("rep.edit")}
                </button>
                <button
                  className="linklike"
                  disabled={busy}
                  onClick={() => void decide(d.draft_id, "discard")}
                >
                  {tr("rep.discard")}
                </button>
                <button
                  className="linklike"
                  disabled={busy}
                  onClick={() => void decide(d.draft_id, "ignore")}
                >
                  {tr("rep.ignore")}
                </button>
              </div>
            </>
          )}
        </div>
      ))}
      {note && <div className="muted rep-note">{note}</div>}
      {waiting.length > 0 && (
        <>
          <div className="convo-group">{tr("rep.waitingTitle")}</div>
          {waiting.map((w) => (
            <div key={w.draft_id} className="settings-group draft-card">
              <div className="muted">
                <button
                  className="linklike"
                  onClick={() => onOpenThread(w.conversation_id)}
                >
                  {tf("rep.waitingCard", {
                    peer: w.conversation_id,
                    text: w.inbound_text,
                  })}
                </button>
              </div>
              {/* The QUESTIONS, spoken by OoLu — never words for the peer. */}
              <div className="bubble">{w.generated_text}</div>
              <div className="setting-control row">
                <span className="muted">{tr("rep.waitingHint")}</span>
                <button
                  className="linklike"
                  disabled={busy}
                  onClick={() => void decide(w.draft_id, "ignore")}
                >
                  {tr("rep.ignore")}
                </button>
              </div>
            </div>
          ))}
        </>
      )}
    </div>
  );
}

// The profile behind the photo: who this thread is, and every margin the
// owner holds on it — the name note (friends), pin, mute, hide, delete.
// One panel serves both kinds of thread; only the hands differ.
export function ContactProfile({
  title,
  subtitle,
  detail,
  hue,
  initial,
  alias,
  onAlias,
  pinned,
  muted,
  onPrefs,
  onHide,
  onDelete,
  deleteLabel,
  deleteHint,
  onBack,
}: {
  title: string;
  subtitle: string;
  detail?: string;
  hue: number;
  initial: string;
  // Friends carry a name note; run threads pass no onAlias and get none.
  alias?: string;
  onAlias?: (alias: string) => Promise<void>;
  pinned: boolean;
  muted: boolean;
  onPrefs: (prefs: { pinned?: boolean; muted?: boolean }) => Promise<void>;
  onHide: () => Promise<void>;
  onDelete: () => Promise<void>;
  deleteLabel: string;
  deleteHint: string;
  onBack: () => void;
}) {
  const tr = useT();
  const [aliasDraft, setAliasDraft] = useState(alias ?? "");
  const [confirming, setConfirming] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  async function act(fn: () => Promise<void>) {
    setError("");
    setBusy(true);
    try {
      await fn();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="contact-profile">
      <span
        className="convo-avatar profile-photo"
        style={{
          background: `hsl(${hue} 45% 34%)`,
          color: "#fff",
          borderColor: "transparent",
        }}
      >
        {initial}
      </span>
      <div className="profile-name">{title}</div>
      <div className="muted">{subtitle}</div>
      {detail && <div className="muted">{detail}</div>}

      {onAlias && (
        <form
          className="setting-control row profile-alias"
          onSubmit={(e) => {
            e.preventDefault();
            void act(() => onAlias(aliasDraft.trim()));
          }}
        >
          <input
            aria-label={tr("friends.rename")}
            placeholder={tr("friends.namePlaceholder")}
            title={tr("friends.renameHint")}
            value={aliasDraft}
            onChange={(e) => setAliasDraft(e.target.value)}
          />
          <button type="submit" disabled={busy}>
            {tr("friends.save")}
          </button>
        </form>
      )}

      <div className="profile-actions">
        <button
          disabled={busy}
          onClick={() => void act(() => onPrefs({ pinned: !pinned }))}
        >
          {pinned ? tr("profile.unpin") : tr("profile.pin")}
        </button>
        <button
          disabled={busy}
          onClick={() => void act(() => onPrefs({ muted: !muted }))}
        >
          {muted ? tr("profile.unmute") : tr("profile.mute")}
        </button>
        <button disabled={busy} onClick={() => void act(onHide)}>
          {tr("profile.hide")}
        </button>
        <button
          className="danger"
          disabled={busy}
          onClick={() => setConfirming(true)}
        >
          {deleteLabel}
        </button>
      </div>
      <p className="muted profile-hint">{tr("profile.hideHint")}</p>
      {confirming && (
        <div className="setting-control row profile-confirm">
          <span className="muted">{deleteHint}</span>
          <button
            className="danger"
            disabled={busy}
            onClick={() => void act(onDelete)}
          >
            {tr("profile.confirmDelete")}
          </button>
          <button
            className="linklike"
            disabled={busy}
            onClick={() => setConfirming(false)}
          >
            {tr("cancel")}
          </button>
        </div>
      )}
      {error && <div className="error">{error}</div>}

      <button type="button" className="linklike" onClick={onBack}>
        ‹ {tr("profile.backToChat")}
      </button>
    </div>
  );
}

// A node's side of the story: the raw audit log as a message history.
// Deliberately unpolished — this is developer material. Everyone else asks
// OoLu, who can read these logs and re-trigger the node on their behalf.
export function NoderThread({
  run,
  onActivity = () => {},
  onClosed = () => {},
}: {
  run: RunSummary;
  onActivity?: () => void;
  onClosed?: () => void;
}) {
  const tr = useT();
  const [events, setEvents] = useState<TimelineEvent[] | null>(null);
  const [profileOpen, setProfileOpen] = useState(false);

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

  // The node's own margins live behind its photo — the same profile door
  // a friend's photo opens; the log below stays a record, not a panel.
  if (profileOpen) {
    return (
      <ContactProfile
        title={conciseName(run.intent)}
        subtitle={run.intent}
        detail={`${run.run_id} · ${run.awaiting ?? run.phase}`}
        hue={identityHue(run.intent)}
        initial={conciseName(run.intent).slice(0, 1).toUpperCase()}
        pinned={run.pinned ?? false}
        muted={run.muted ?? false}
        onPrefs={async (prefs) => {
          await api.setRunPrefs(run.run_id, prefs);
          onActivity();
        }}
        onHide={async () => {
          await api.setRunPrefs(run.run_id, { hidden: true });
          onClosed();
        }}
        onDelete={async () => {
          // A run log is an audit record: "delete" removes the thread
          // from the list; the record itself is preserved, and the
          // thread returns if the node ever speaks again.
          await api.setRunPrefs(run.run_id, { hidden: true });
          onClosed();
        }}
        deleteLabel={tr("profile.delete")}
        deleteHint={tr("noder.deleteHint")}
        onBack={() => setProfileOpen(false)}
      />
    );
  }
  return (
    <div className="noder-thread">
      <div className="noder-head">
        <button
          type="button"
          className="convo-avatar node clickable"
          aria-label={`${tr("profile.open")} ${conciseName(run.intent)}`}
          title={tr("profile.openHint")}
          onClick={() => setProfileOpen(true)}
          style={{
            background: `hsl(${identityHue(run.intent)} 45% 34%)`,
            color: "#fff",
            borderColor: "transparent",
          }}
        >
          {conciseName(run.intent).slice(0, 1).toUpperCase()}
        </button>
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
  const tr = useT();
  const [query, setQuery] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  // The found account and where we stand with them, so the button offers
  // the right next step — send a request, accept theirs, or open the chat.
  const [found, setFound] = useState<{ username: string; relationship: string } | null>(
    null,
  );
  const [incoming, setIncoming] = useState<string[]>([]);
  // Side by side, physically: one shows their QR code, the other scans
  // it — the friend request sends itself. The code carries only the
  // username (oolu:friend:<name>), nothing secret. The code IS the
  // opening move: it shows itself the moment this pane opens, and one
  // button flips the same spot between showing and scanning.
  const me = session.principal ?? "";
  const [qrMode, setQrMode] = useState<"none" | "show" | "scan">(
    me ? "show" : "none",
  );
  const [qrUrl, setQrUrl] = useState("");
  const videoRef = useRef<HTMLVideoElement | null>(null);

  const loadIncoming = useCallback(async () => {
    try {
      setIncoming((await api.friendRequests()).items);
    } catch {
      setIncoming([]);
    }
  }, []);

  useEffect(() => {
    void loadIncoming();
  }, [loadIncoming]);

  useEffect(() => {
    if (qrMode !== "show" || !me) return;
    let cancelled = false;
    QRCode.toDataURL(`oolu:friend:${me}`, { margin: 1, width: 220 })
      .then((url) => {
        if (!cancelled) setQrUrl(url);
      })
      .catch(() => setQrUrl(""));
    return () => {
      cancelled = true;
    };
  }, [qrMode, me]);

  const connectScanned = useCallback(async (username: string) => {
    setError("");
    setBusy(true);
    try {
      setQuery(username);
      let hit = await api.friendLookup(username);
      if (hit.relationship === "none") {
        // The scan IS the intent — being handed the code in person is
        // the invitation, so the request goes out without a second tap.
        await api.sendFriendRequest(hit.username);
        hit = await api.friendLookup(hit.username);
      }
      setFound(hit);
      void loadIncoming();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }, [loadIncoming]);

  useEffect(() => {
    if (qrMode !== "scan") return;
    let stopped = false;
    let timer: number | undefined;
    let stream: MediaStream | null = null;
    const canvas = document.createElement("canvas");
    async function start() {
      try {
        stream = await navigator.mediaDevices.getUserMedia({
          video: { facingMode: "environment" },
        });
        const video = videoRef.current;
        if (stopped || !video) return;
        video.srcObject = stream;
        await video.play();
        timer = window.setInterval(() => {
          const v = videoRef.current;
          if (!v || v.videoWidth === 0) return;
          canvas.width = v.videoWidth;
          canvas.height = v.videoHeight;
          const ctx = canvas.getContext("2d");
          if (!ctx) return;
          ctx.drawImage(v, 0, 0);
          const image = ctx.getImageData(0, 0, canvas.width, canvas.height);
          const code = jsQR(image.data, image.width, image.height);
          const match = code?.data.match(/^oolu:friend:(.+)$/);
          if (match) {
            // The cleanup below stops the camera; the spot flips back
            // to the user's own code.
            setQrMode(me ? "show" : "none");
            void connectScanned(match[1]);
          }
        }, 350);
      } catch {
        setError(tr("friends.cameraError"));
        setQrMode(me ? "show" : "none");
      }
    }
    void start();
    return () => {
      stopped = true;
      if (timer) clearInterval(timer);
      stream?.getTracks().forEach((t) => t.stop());
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [qrMode, connectScanned]);

  async function find() {
    setError("");
    setFound(null);
    setBusy(true);
    try {
      setFound(await api.friendLookup(query.trim()));
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  async function act(fn: () => Promise<unknown>) {
    setError("");
    setBusy(true);
    try {
      await fn();
      if (found) setFound(await api.friendLookup(found.username));
      void loadIncoming();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="pane-empty start-conversation">
      {/* The code first, upper middle: side by side, one screen shows
          while the other scans — one button flips the same spot, so the
          window keeps one symmetric shape either way. */}
      {qrMode === "show" && (
        <div className="qr-panel">
          {qrUrl && (
            <img
              src={qrUrl}
              alt={`QR code for ${me}`}
              width={220}
              height={220}
            />
          )}
          <div className="muted">{me}</div>
        </div>
      )}
      {qrMode === "scan" && (
        <div className="qr-panel">
          <video ref={videoRef} muted playsInline className="qr-video" />
          <div className="muted">{tr("friends.scanning")}</div>
        </div>
      )}
      <div className="qr-connect setting-control row">
        <button
          type="button"
          onClick={() =>
            setQrMode(qrMode === "scan" ? (me ? "show" : "none") : "scan")
          }
        >
          {qrMode === "scan" ? tr("friends.myQr") : tr("friends.scanQr")}
        </button>
      </div>
      <p className="muted">{tr("friends.qrHint")}</p>

      <p>{tr("friends.who")}</p>
      <p className="muted">{tr("friends.whoHint")}</p>
      <form
        className="setting-control row"
        onSubmit={(e) => {
          e.preventDefault();
          void find();
        }}
      >
        <input
          aria-label={tr("friends.usernameOrEmail")}
          placeholder={tr("friends.usernameOrEmail")}
          value={query}
          onChange={(e) => setQuery(e.target.value)}
        />
        <button type="submit" disabled={busy || !query.trim()}>
          {busy ? tr("friends.looking") : tr("friends.find")}
        </button>
      </form>

      {found && (
        <div className="found-account setting-control row">
          <span className="convo-name">{found.username}</span>
          {found.relationship === "friends" ? (
            <button onClick={() => onOpen(found.username)}>
              {tr("friends.openChat")}
            </button>
          ) : found.relationship === "pending_out" ? (
            <>
              <button disabled={busy} onClick={() => onOpen(found.username)}>
                {tr("friends.message")}
              </button>
              <span className="muted">{tr("friends.requestSent")}</span>
            </>
          ) : found.relationship === "pending_in" ? (
            <>
              <button
                disabled={busy}
                onClick={() =>
                  void act(() =>
                    api.decideFriendRequest(found.username, "accept"),
                  )
                }
              >
                {tr("friends.accept")}
              </button>
              <button
                className="linklike"
                disabled={busy}
                onClick={() => onOpen(found.username)}
              >
                {tr("friends.message")}
              </button>
            </>
          ) : found.relationship === "blocked" ? (
            <button
              disabled={busy}
              onClick={() =>
                void act(() =>
                  api.decideFriendRequest(found.username, "unblock"),
                )
              }
            >
              {tr("friends.unblock")}
            </button>
          ) : (
            <>
              {/* Messaging does not WAIT on friendship: recipients who
                  accept non-friend messages (the default) get it right
                  away; a friends-only recipient answers the send with
                  "send a friend request first" — their choice, enforced
                  by the server, not preempted by the UI. */}
              <button
                disabled={busy}
                onClick={() => onOpen(found.username)}
              >
                {tr("friends.message")}
              </button>
              <button
                className="linklike"
                disabled={busy}
                onClick={() =>
                  void act(() => api.sendFriendRequest(found.username))
                }
              >
                {tr("friends.sendRequest")}
              </button>
            </>
          )}
        </div>
      )}
      {error && <div className="error">{error}</div>}

      {incoming.length > 0 && (
        <div className="incoming-requests">
          <p className="muted">{tr("friends.incoming")}</p>
          {incoming.map((who) => (
            <div key={who} className="setting-control row">
              <span className="convo-name">{who}</span>
              <button
                disabled={busy}
                onClick={() =>
                  void act(() => api.decideFriendRequest(who, "accept"))
                }
              >
                {tr("friends.accept")}
              </button>
              <button
                className="linklike"
                disabled={busy}
                onClick={() =>
                  void act(() => api.decideFriendRequest(who, "decline"))
                }
              >
                {tr("friends.decline")}
              </button>
              <button
                className="linklike"
                disabled={busy}
                onClick={() =>
                  void act(() => api.decideFriendRequest(who, "block"))
                }
              >
                {tr("friends.block")}
              </button>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

// One person's thread. Opening it marks their messages read (the server
// does that on GET); a short poll keeps both sides fresh — same rhythm as
// the run list.
export function FriendThread({
  peer,
  alias = "",
  since = "",
  pinned = false,
  muted = false,
  onActivity,
  onClosed = () => {},
}: {
  peer: string;
  // The user's own name note for this friend (empty = plain username),
  // and when the friendship began — both worn on the header.
  alias?: string;
  since?: string;
  // The margins this owner holds on the thread, shown in the profile.
  pinned?: boolean;
  muted?: boolean;
  onActivity: () => void;
  // Called when the thread leaves the list from inside (hide/delete).
  onClosed?: () => void;
}) {
  const tr = useT();
  const [messages, setMessages] = useState<FriendMessage[] | null>(null);
  const [profileOpen, setProfileOpen] = useState(false);
  // The typing block survives pane switches and restarts — and a
  // discarded representative draft lands HERE, kept for reworking.
  const [draft, setDraft] = useState(() => loadCompose(peer));
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  // The representative: ✍ appears only when the account turned it on.
  const [rep, setRep] = useState<RepresentativeStatus | null>(null);
  const [suggestion, setSuggestion] = useState<RepresentativeDraft | null>(
    null,
  );
  // Set while the composer holds a draft being edited: the send button
  // then decides the draft (recording the edit) instead of a plain send.
  const [editingDraft, setEditingDraft] = useState<string | null>(null);
  const endRef = useRef<HTMLDivElement | null>(null);
  const repOn = rep !== null && rep.mode !== "off";

  useEffect(() => {
    api
      .representative()
      .then(setRep)
      .catch(() => setRep(null)); // no door on this host: no button
  }, []);

  async function setPeerAuto(allowed: boolean) {
    setError("");
    try {
      setRep(await api.setRepresentativePeerAuto(peer, allowed));
    } catch (e) {
      setError((e as Error).message);
    }
  }

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

  // Persist the typing block as it changes; sending clears it below.
  useEffect(() => {
    saveCompose(peer, draft);
  }, [peer, draft]);

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
      // A discarded suggestion is kept, not buried: it lands in THIS
      // conversation's typing block, ready to be reworked by hand.
      if (action === "discard") setDraft(suggestion.generated_text);
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

  // The profile behind the photo: the name note, pin, mute, hide, and
  // the unfriending — every margin in one place, reached from the face.
  if (profileOpen) {
    return (
      <ContactProfile
        title={alias || peer}
        subtitle={peer}
        detail={
          since ? tf("friends.since", { date: since.slice(0, 10) }) : undefined
        }
        hue={identityHue(peer)}
        initial={(alias || peer).slice(0, 1).toUpperCase()}
        alias={alias}
        onAlias={async (next) => {
          await api.setFriendAlias(peer, next);
          onActivity();
        }}
        pinned={pinned}
        muted={muted}
        onPrefs={async (prefs) => {
          await api.setFriendPrefs(peer, prefs);
          onActivity();
        }}
        onHide={async () => {
          await api.setFriendPrefs(peer, { hidden: true });
          onClosed();
        }}
        onDelete={async () => {
          await api.deleteFriend(peer);
          onClosed();
        }}
        deleteLabel={tr("profile.deleteFriend")}
        deleteHint={tr("profile.deleteFriendHint")}
        onBack={() => setProfileOpen(false)}
      />
    );
  }
  return (
    <div className="chat friend-thread">
      {/* Who you are talking to — worn on the window, just like OoLu's
          own header: the face, the name (your note first), the username
          underneath, and when you became friends. The face is the door
          to their profile — rename, pin, mute, hide, delete live there. */}
      <div className="chat-head">
        <button
          type="button"
          className="convo-avatar clickable"
          aria-label={`${tr("profile.open")} ${peer}`}
          title={tr("profile.openHint")}
          onClick={() => setProfileOpen(true)}
          style={{
            background: `hsl(${identityHue(peer)} 45% 34%)`,
            color: "#fff",
            borderColor: "transparent",
          }}
        >
          {(alias || peer).slice(0, 1).toUpperCase()}
        </button>
        <div className="chat-head-body">
          <div className="chat-head-name">
            {pinned ? "📌 " : ""}
            {alias || peer}
            {muted ? " 🔕" : ""}
          </div>
          <div className="chat-head-sub">
            {[
              alias ? peer : "",
              since ? tf("friends.since", { date: since.slice(0, 10) }) : "",
            ]
              .filter(Boolean)
              .join(" · ")}
          </div>
        </div>
      </div>
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
          <div className="muted">{tr("rep.drafted")}</div>
          <div className="bubble user">{suggestion.generated_text}</div>
          <div className="setting-control row">
            <button
              aria-label="Send drafted reply"
              disabled={busy}
              onClick={() => void decide("send")}
            >
              {tr("rep.send")}
            </button>
            <button
              disabled={busy}
              onClick={() => {
                setDraft(suggestion.generated_text);
                setEditingDraft(suggestion.draft_id);
              }}
            >
              {tr("rep.edit")}
            </button>
            <button
              className="linklike"
              disabled={busy}
              onClick={() => void decide("discard")}
            >
              {tr("rep.discard")}
            </button>
          </div>
        </div>
      )}
      {editingDraft && (
        <div className="muted">{tr("rep.editing")}</div>
      )}
      {rep !== null && rep.mode === "auto" && (
        <label className="muted rep-peer-toggle">
          <input
            type="checkbox"
            aria-label={`Auto-replies to ${peer}`}
            checked={!rep.muted_peers.includes(peer)}
            onChange={(e) => void setPeerAuto(e.target.checked)}
          />{" "}
          {tf("rep.autoToPeer", { peer })}
        </label>
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
            title={tr("rep.draftButton")}
            aria-label="Draft a reply in your voice"
            disabled={busy || messages === null || messages.length === 0}
            onClick={() => void suggest()}
          >
            ✍
          </button>
        )}
        <button disabled={busy || !draft.trim()} onClick={() => void send()}>
          {busy ? "…" : editingDraft ? tr("rep.sendEdited") : tr("rep.send")}
        </button>
      </div>
    </div>
  );
}
