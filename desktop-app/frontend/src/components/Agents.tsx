import { useEffect, useRef, useState } from "react";
import { api, session } from "../api";
import type {
  Contribution,
  FileMeta,
  PressGenre,
  PressLicense,
  RosterAgent,
  Story,
} from "../api";
import { identityHue } from "../avatar";
import { loadCompose, saveCompose, tf, useT } from "../ui";
import { Byline } from "./Byline";

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

// The contribution spine's surface (A1): the shelf of live pieces and
// the contribute form. Everything renders from the server's own words —
// the taxonomy, the license terms — never hardcoded copies; a host from
// before the press (404 on genres) renders nothing at all.
export function PressPanel() {
  const tr = useT();
  const [genres, setGenres] = useState<PressGenre[] | null>(null);
  const [license, setLicense] = useState<PressLicense | null>(null);
  const [shelf, setShelf] = useState<Contribution[]>([]);
  const [writing, setWriting] = useState(false);
  const [title, setTitle] = useState("");
  const [body, setBody] = useState("");
  const [picked, setPicked] = useState<string[]>([]);
  const [consent, setConsent] = useState(false);
  const [drawer, setDrawer] = useState<FileMeta[]>([]);
  const [attached, setAttached] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  // The newsroom (A2): the caller's edition, and whether it is theirs.
  const [stories, setStories] = useState<Story[]>([]);
  const [personalized, setPersonalized] = useState(false);
  const [editionOn, setEditionOn] = useState(false);
  const [newsroom, setNewsroom] = useState(true); // false: pre-A2 host
  // The last feedback verdict per story — the tap's honest echo.
  const [noted, setNoted] = useState<Record<string, string>>({});
  const me = session.principal;

  const refreshShelf = () =>
    api
      .pressContributions()
      .then(({ items }) => setShelf(items))
      .catch(() => setShelf([]));

  const refreshStories = () =>
    api
      .pressStories()
      .then((edition) => {
        setStories(edition.items ?? []);
        setPersonalized(edition.personalized === true);
        setEditionOn(
          edition.edition_schedule !== null &&
            edition.edition_schedule !== undefined,
        );
      })
      .catch(() => setNewsroom(false));

  useEffect(() => {
    void api
      .pressGenres()
      .then((meta) => {
        setGenres(meta.items);
        setLicense(meta.licenses[0] ?? null);
        void refreshShelf();
        void refreshStories();
      })
      .catch(() => setGenres(null)); // no press on this host: no panel
  }, []);

  // The drawer list loads when the form opens — attach is refs to YOUR
  // files, never copies; the picker shows exactly what you own.
  useEffect(() => {
    if (!writing) return;
    void api
      .files()
      .then(({ items }) => setDrawer(items ?? []))
      .catch(() => setDrawer([]));
  }, [writing]);

  if (genres === null || license === null) return null;

  function toggleGenre(key: string) {
    setPicked((p) =>
      p.includes(key)
        ? p.filter((k) => k !== key)
        : p.length < 3
          ? [...p, key]
          : p,
    );
  }

  function toggleFile(fileId: string) {
    setAttached((a) =>
      a.includes(fileId)
        ? a.filter((f) => f !== fileId)
        : a.length < 6
          ? [...a, fileId]
          : a,
    );
  }

  async function publish() {
    if (busy || license === null) return;
    setError("");
    setBusy(true);
    try {
      const record = await api.pressPublish({
        title,
        body,
        genres: picked,
        license: license.key,
        consent,
        ...(attached.length > 0 ? { file_ids: attached } : {}),
      });
      setShelf((s) => [record, ...s]);
      setWriting(false);
      setTitle("");
      setBody("");
      setPicked([]);
      setAttached([]);
      setConsent(false);
    } catch (e) {
      // The gate's refusal IS the direction — shown verbatim.
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  async function feedback(story: Story, signal: "like" | "skip") {
    try {
      const verdict = await api.pressStoryFeedback(story.story_id, signal);
      setNoted((n) => ({
        ...n,
        [story.story_id]: verdict.recorded
          ? tr("press.noted")
          : tr("press.notRecorded"),
      }));
      if (verdict.recorded) void refreshStories();
    } catch {
      /* the panel's next refresh tells the truth */
    }
  }

  return (
    <div className="press-panel">
      {newsroom && (
        <>
          <div className="press-head">
            <span className="press-heading">
              {tr("press.stories")}
              {personalized ? ` · ${tr("press.yours")}` : ""}
            </span>
            <span className="press-actions">
              <button
                className="ghost"
                onClick={() =>
                  void api
                    .pressNewsroomRun()
                    .then(() => refreshStories())
                    .catch(() => {})
                }
              >
                {tr("press.compose")}
              </button>
              <button
                className={`ghost${editionOn ? " on" : ""}`}
                title={tr("press.editionHint")}
                onClick={() =>
                  void api
                    .pressEditionSchedule({ enabled: !editionOn })
                    .then(({ edition_schedule }) =>
                      setEditionOn(edition_schedule !== null),
                    )
                    .catch(() => {})
                }
              >
                {editionOn ? tr("press.editionOn") : tr("press.editionOff")}
              </button>
            </span>
          </div>
          {stories.length === 0 && (
            <div className="muted press-empty">{tr("press.noStories")}</div>
          )}
          {stories.map((story) => (
            <div key={story.story_id} className="press-card story">
              <div className="press-title">{story.headline}</div>
              <div className="press-body">{story.prose}</div>
              {/* Every cited contributor's byline — the attribution the
                  lineage recorded at composition time. */}
              <div className="press-bylines">
                {[
                  ...new Set(story.lineage.map((share) => share.author)),
                ].map((author) => (
                  <Byline key={author} username={author} size={20} />
                ))}
              </div>
              <div className="press-meta">
                {story.genres.map((key) => (
                  <span key={key} className="press-chip on">
                    {genres.find((g) => g.key === key)?.label ?? key}
                  </span>
                ))}
                <button
                  className="linklike"
                  onClick={() => void feedback(story, "like")}
                >
                  {tr("press.like")}
                </button>
                <button
                  className="linklike"
                  onClick={() => void feedback(story, "skip")}
                >
                  {tr("press.skip")}
                </button>
                {noted[story.story_id] && (
                  <span className="muted">{noted[story.story_id]}</span>
                )}
              </div>
              {/* The reasons, on demand: the rubric's factor breakdown. */}
              <details className="press-why">
                <summary>{tr("press.why")}</summary>
                <div className="muted">
                  {Object.entries(story.breakdown)
                    .map(([factor, value]) => `${factor}: ${value}`)
                    .join(" · ")}
                </div>
              </details>
            </div>
          ))}
        </>
      )}

      <div className="press-head">
        <span className="press-heading">{tr("press.contributions")}</span>
        <button
          className="ghost"
          onClick={() => {
            setError("");
            setWriting((w) => !w);
          }}
        >
          {writing ? tr("press.close") : tr("press.write")}
        </button>
      </div>

      {writing && (
        <div className="press-compose">
          <input
            placeholder={tr("press.titlePh")}
            value={title}
            maxLength={120}
            onChange={(e) => setTitle(e.target.value)}
          />
          <textarea
            placeholder={tr("press.bodyPh")}
            value={body}
            rows={5}
            onChange={(e) => setBody(e.target.value)}
          />
          <div className="press-genres">
            {genres.map((g) => (
              <button
                key={g.key}
                type="button"
                title={g.description}
                className={`press-chip${picked.includes(g.key) ? " on" : ""}`}
                onClick={() => toggleGenre(g.key)}
              >
                {g.label}
              </button>
            ))}
          </div>
          {drawer.length > 0 && (
            <details className="press-attach">
              <summary>
                {tr("press.attach")}
                {attached.length > 0 ? ` (${attached.length})` : ""}
              </summary>
              {drawer.map((f) => (
                <label key={f.file_id} className="press-file">
                  <input
                    type="checkbox"
                    checked={attached.includes(f.file_id)}
                    onChange={() => toggleFile(f.file_id)}
                  />
                  {f.name}
                </label>
              ))}
            </details>
          )}
          {/* Consent renders the SERVER's license terms — the words the
              record will actually stand under. */}
          <details className="press-license">
            <summary>{license.name}</summary>
            <p className="muted">{license.terms}</p>
          </details>
          <label className="press-consent">
            <input
              type="checkbox"
              checked={consent}
              onChange={(e) => setConsent(e.target.checked)}
            />
            {tr("press.consent")}
          </label>
          <div className="row">
            <button disabled={busy} onClick={() => void publish()}>
              {busy ? "…" : tr("press.publish")}
            </button>
          </div>
          {error && <div className="error">{error}</div>}
        </div>
      )}

      {shelf.length === 0 && !writing && (
        <div className="muted press-empty">{tr("press.empty")}</div>
      )}
      {shelf.map((c) => (
        <div key={c.contribution_id} className="press-card">
          <div className="press-byline">
            <Byline username={c.author} size={24} />
            {c.author === me && !c.superseded_at && (
              <button
                className="linklike"
                onClick={() =>
                  void api
                    .pressUnpublish(c.contribution_id)
                    .then(() => refreshShelf())
                    .catch(() => {})
                }
              >
                {tr("press.unpublish")}
              </button>
            )}
          </div>
          <div className="press-title">{c.title}</div>
          <div className="press-body">{c.body}</div>
          <div className="press-meta">
            {c.genres.map((key) => (
              <span key={key} className="press-chip on">
                {genres.find((g) => g.key === key)?.label ?? key}
              </span>
            ))}
            {c.similar_to && (
              <span className="press-chip credit" title={c.similar_to}>
                {tr("press.retold")}
              </span>
            )}
          </div>
        </div>
      ))}
    </div>
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
        {/* The press (A1) lives at the head of the News thread: the
            contribution shelf and the contribute form — in-thread, the
            inlineBlock pattern, never a window popping on top. */}
        {agent.agent_id === "news" && <PressPanel />}
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
