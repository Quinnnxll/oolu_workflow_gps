import { useEffect, useRef, useState } from "react";
import { api, session } from "../api";
import type {
  AdPlacement,
  Contribution,
  ExplorerBrief,
  ExplorerRow,
  FileMeta,
  PollPair,
  PollVerdict,
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

// The ad slot (A4): one sponsored placement between the content — or a
// consent card, or nothing. The label is structural (the server sends
// it; the slot renders it); the impression posts once per placement;
// nothing sponsored exists for a member who has not accepted the
// current privacy version, and the accept flow names the version it
// accepts.
export function AdSlot({
  surface,
  content,
}: {
  surface: "edition" | "poll";
  content: string;
}) {
  const tr = useT();
  const [placement, setPlacement] = useState<AdPlacement | null>(null);
  const [needsConsent, setNeedsConsent] = useState(false);
  const impressed = useRef<string | null>(null);

  async function load() {
    try {
      const served = await api.pressAd(surface, content);
      setNeedsConsent(served.reason === "consent");
      setPlacement(served.placement);
    } catch {
      setPlacement(null); // no ad house on this host: an empty slot
      setNeedsConsent(false);
    }
  }

  useEffect(() => {
    void load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [surface, content]);

  // The impression: once per placement, from the member it was served to.
  useEffect(() => {
    if (placement && impressed.current !== placement.placement_id) {
      impressed.current = placement.placement_id;
      void api.adImpression(placement.placement_id).catch(() => {});
    }
  }, [placement]);

  async function accept() {
    try {
      const consent = await api.legalConsent();
      await api.legalAccept(consent.privacy_version);
      await load();
    } catch {
      /* the next load tells the truth */
    }
  }

  if (needsConsent) {
    return (
      <div className="ad-card consent">
        <div className="press-title">{tr("ads.consentTitle")}</div>
        <div className="press-body">{tr("ads.consentBody")}</div>
        <div className="row">
          <a
            href="/v1/legal/privacy"
            target="_blank"
            rel="noreferrer"
            className="linklike"
          >
            {tr("ads.readPolicy")}
          </a>
          <button onClick={() => void accept()}>{tr("ads.accept")}</button>
        </div>
      </div>
    );
  }
  if (!placement) return null;
  return (
    <div className="ad-card">
      {/* The label law: every placement renders its label, always. */}
      <span className="ad-label">{placement.label}</span>
      <div className="press-title">{placement.campaign_name}</div>
      <div className="press-body">{placement.creative}</div>
      <button
        className="linklike"
        title={placement.offer_ref}
        onClick={() => void api.adClick(placement.placement_id).catch(() => {})}
      >
        {tr("ads.offer")}
      </button>
    </div>
  );
}

// The poll floor's surface (A3): one pair at a time — two member pieces
// side by side, each with its byline; tap to vote; the reveal follows
// the server's honesty laws verbatim (vote first, floor second). The
// genre chips ARE the stream switch; no chip = the server's exploration
// pick.
export function PollPanel() {
  const tr = useT();
  const [genres, setGenres] = useState<PressGenre[] | null>(null);
  const [genre, setGenre] = useState<string | null>(null);
  const [pair, setPair] = useState<PollPair | null>(null);
  const [verdict, setVerdict] = useState<PollVerdict | null>(null);
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);

  async function nextPair(chosen: string | null) {
    setBusy(true);
    setVerdict(null);
    setNote("");
    try {
      setPair(await api.pressPollNext(chosen ?? undefined));
    } catch (e) {
      setPair(null);
      setNote(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  useEffect(() => {
    void api
      .pressGenres()
      .then((meta) => {
        setGenres(meta.items);
        void nextPair(null);
      })
      .catch(() => setGenres(null)); // no press on this host: no panel
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  if (genres === null) return null;

  async function vote(choice: "left" | "right") {
    if (!pair || busy) return;
    setBusy(true);
    try {
      setVerdict(await api.pressPollVote(pair.pair_id, choice));
    } catch (e) {
      setNote(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  function switchGenre(key: string) {
    const next = genre === key ? null : key;
    setGenre(next);
    void nextPair(next);
  }

  return (
    <div className="press-panel poll">
      <div className="press-head">
        <span className="press-heading">{tr("poll.heading")}</span>
      </div>
      <div className="press-genres">
        {genres.map((g) => (
          <button
            key={g.key}
            type="button"
            title={g.description}
            className={`press-chip${genre === g.key ? " on" : ""}`}
            onClick={() => switchGenre(g.key)}
          >
            {g.label}
          </button>
        ))}
      </div>
      {pair && (
        <div className="poll-pair">
          {(["left", "right"] as const).map((side) => (
            <button
              key={side}
              type="button"
              className={`poll-side${
                verdict?.choice === side ? " chosen" : ""
              }`}
              disabled={busy || verdict?.voted === true}
              onClick={() => void vote(side)}
            >
              <span className="press-title">{pair[side].title}</span>
              <span className="press-body">{pair[side].excerpt}</span>
              <Byline username={pair[side].author} size={20} />
            </button>
          ))}
        </div>
      )}
      {verdict && (
        <div className="poll-verdict">
          {verdict.revealed && verdict.counts && verdict.total ? (
            <>
              <span>
                {tf("poll.result", {
                  left: Math.round(
                    (100 * verdict.counts.left) / verdict.total,
                  ),
                  right: Math.round(
                    (100 * verdict.counts.right) / verdict.total,
                  ),
                  n: verdict.total,
                })}
              </span>
            </>
          ) : (
            // The server's honesty law, verbatim — "not enough votes
            // yet" and nothing noisier.
            <span className="muted">{verdict.reason}</span>
          )}
          {verdict.learning === false && (
            <span className="muted"> · {tr("press.notRecorded")}</span>
          )}
        </div>
      )}
      {note && <div className="muted press-empty">{note}</div>}
      {/* The magazine rule: the ad lives BETWEEN the content, labeled. */}
      {pair && <AdSlot surface="poll" content={pair.pair_id} />}
      <div className="row">
        <button
          className="ghost"
          disabled={busy}
          onClick={() => void nextPair(genre)}
        >
          {tr("poll.next")}
        </button>
      </div>
    </div>
  );
}

const EXPLORER_MODES = ["value", "balanced", "proven", "measured"] as const;

function money(micros: number, currency: string): string {
  return `${(micros / 1_000_000).toFixed(2)} ${currency}`;
}

// The explorer desk's surface (A6): one comparison matrix over verified
// evidence, the deterministic brief with its winner and every factor on
// demand, and the follow button that schedules the daily brief into
// this thread. Nothing sponsored is anywhere near these rows.
export function ExplorerPanel() {
  const tr = useT();
  const [category, setCategory] = useState("");
  const [mode, setMode] = useState<(typeof EXPLORER_MODES)[number]>("balanced");
  const [rows, setRows] = useState<ExplorerRow[]>([]);
  const [brief, setBrief] = useState<ExplorerBrief | null>(null);
  const [followed, setFollowed] = useState(false);
  const [absent, setAbsent] = useState(false);

  async function compare(cat: string, m: string) {
    try {
      const result = await api.explorerCompare(cat, m);
      setRows(result.rows ?? []);
      setBrief(result.brief ?? null);
    } catch {
      setAbsent(true); // no explorer on this host: no panel
    }
  }

  useEffect(() => {
    void compare("", "balanced");
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  if (absent) return null;

  const winner =
    brief?.winner_listing_id != null
      ? brief.ranked.find((r) => r.listing_id === brief.winner_listing_id)
      : undefined;

  return (
    <div className="press-panel explorer">
      <div className="press-head">
        <span className="press-heading">{tr("explorer.heading")}</span>
        <button
          className={`ghost${followed ? " on" : ""}`}
          title={tr("explorer.followHint")}
          onClick={() =>
            void api
              .explorerInterest({
                category,
                mode,
                ...(followed ? { enabled: false } : {}),
              })
              .then(({ interest }) => setFollowed(interest !== null))
              .catch(() => {})
          }
        >
          {followed ? tr("explorer.following") : tr("explorer.follow")}
        </button>
      </div>
      <div className="row">
        <input
          placeholder={tr("explorer.categoryPh")}
          value={category}
          onChange={(e) => setCategory(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") void compare(category, mode);
          }}
        />
      </div>
      <div className="press-genres">
        {EXPLORER_MODES.map((m) => (
          <button
            key={m}
            type="button"
            className={`press-chip${mode === m ? " on" : ""}`}
            onClick={() => {
              setMode(m);
              void compare(category, m);
            }}
          >
            {tr(`explorer.mode.${m}`)}
          </button>
        ))}
      </div>
      {winner && (
        <div className="press-card story">
          <span className="ad-label">{tr("explorer.winner")}</span>
          <div className="press-title">{winner.title}</div>
          <div className="muted">
            {winner.seller} · {tr("explorer.score")} {winner.score}
          </div>
          {/* The reasons, on demand — invariant 10's whole point. */}
          <details className="press-why">
            <summary>{tr("explorer.why")}</summary>
            <div className="muted">
              {Object.entries(winner.factors)
                .map(([name, value]) => `${name}: ${value}`)
                .join(" · ")}
            </div>
          </details>
        </div>
      )}
      {rows.map((row) => (
        <div key={row.listing_id} className="press-card explorer-row">
          <div className="press-byline">
            <span className="press-title">{row.title}</span>
            <span>
              {money(row.price_micros, row.currency)}
              {/* The discount renders only where it IS a fact. */}
              {row.discount_percent != null && (
                <span className="press-chip credit">
                  −{row.discount_percent}%
                </span>
              )}
            </span>
          </div>
          <div className="press-meta muted">
            <span>{row.seller}</span>
            <span>
              {tf("explorer.reviews", { n: row.feedback.count })}
              {row.feedback.mean != null ? ` · ${row.feedback.mean}/5` : ""}
            </span>
            <span>
              {tr("explorer.trust")} {row.trust.score}
            </span>
            <span>
              {tf("explorer.lab", { n: row.lab.count })}
              {row.lab.mean_score != null ? ` · ${row.lab.mean_score}` : ""}
            </span>
            {row.gaps.map((gap) => (
              <span key={gap} className="press-chip">
                {gap}
              </span>
            ))}
          </div>
        </div>
      ))}
    </div>
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
          {/* The ad between the stories — never inside the prose. */}
          {stories.length > 0 && (
            <AdSlot surface="edition" content={stories[0].story_id} />
          )}
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
        {/* The poll floor (A3) heads the Poll thread the same way. */}
        {agent.agent_id === "poll" && <PollPanel />}
        {/* And the explorer desk (A6) heads the Explorer thread. */}
        {agent.agent_id === "explorer" && <ExplorerPanel />}
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
