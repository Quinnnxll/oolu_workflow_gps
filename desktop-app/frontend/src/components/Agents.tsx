import { useEffect, useRef, useState } from "react";
import { api, session } from "../api";
import type {
  AdPlacement,
  CalendarEvent,
  Contribution,
  ExplorerBrief,
  ExplorerRow,
  PressGenre,
  RosterAgent,
  Story,
  StoryMetrics,
  StoryPreview,
  TravelBrief,
  TurnFileRef,
} from "../api";
import { identityHue } from "../avatar";
import { pickLocalFiles, saveToDevice } from "../device";
import { loadCompose, saveCompose, tf, useT } from "../ui";
import { AttachmentStrip } from "./Attachments";
import { Byline } from "./Byline";
import { MarketPanel } from "./Market";

// A roster agent's conversation (agents-expansion plan A0): the same
// messenger shape as the OoLu chat, deliberately leaner — words only. No
// runs, no tools, no reminders: those are OoLu's; an agent that needs
// hands earns them in its own phase, through its own seat. The thread is
// the server's (per-account, per-agent); localStorage stays the warm
// cache exactly like the OoLu chat.

// A structured piece riding an agent's reply — rendered in the bubble:
// the pushed edition's story previews (each expandable to the full
// story), genre chips, the Explorer's followable categories (a tap
// speaks "follow …"), or the closest products with the comparison's
// own deadline.
type ChatBlock =
  | { kind: "story"; items: StoryPreview[] }
  | { kind: "genres"; items: PressGenre[] }
  | { kind: "categories"; items: { category: string; followed: boolean }[] }
  | {
      kind: "chart";
      title: string;
      unit: string;
      points: { label: string; value: number }[];
    }
  | {
      kind: "products";
      items: {
        listing_id: string;
        title: string;
        unit_price_micros: number;
        currency: string;
        category: string;
      }[];
      mode: string;
      expires_at: string;
    };

type AgentMsg = {
  kind: "user" | "assistant";
  text: string;
  reasoning?: string | null;
  block?: ChatBlock | null;
  // A user turn's attachments — drawer refs rendered as previews with a
  // lossless download, on this device and every other one.
  files?: TurnFileRef[];
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
  surface: "edition";
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

// The data visualization panel, as a conversation block: the member's
// own book drawn as clean horizontal bars — pure CSS, no library, the
// widest bar is the scale. Shared by OoLu's chat and the agent threads.
export function ChartBlock({
  title,
  unit,
  points,
}: {
  title: string;
  unit: string;
  points: { label: string; value: number }[];
}) {
  const top = Math.max(...points.map((p) => Math.abs(p.value)), 1);
  return (
    <div className="chart-block">
      <div className="chart-title">
        {title}
        {unit ? <span className="muted"> · {unit}</span> : null}
      </div>
      {points.map((p, i) => (
        <div key={`${p.label}:${i}`} className="chart-row">
          <span className="chart-label" title={p.label}>
            {p.label}
          </span>
          <span
            className={`chart-bar${p.value < 0 ? " neg" : ""}`}
            style={{ width: `${(Math.abs(p.value) / top) * 100}%` }}
          />
          <span className="chart-value">
            {Number.isInteger(p.value) ? p.value : p.value.toFixed(2)}
          </span>
        </div>
      ))}
    </div>
  );
}

// The genre picking, as a message block: tapping a chip SPEAKS — the
// pick goes back through the conversation, and the desk deals from
// that stream.
export function GenreChipsBlock({
  items,
  onPick,
}: {
  items: PressGenre[];
  onPick: (label: string) => void;
}) {
  return (
    <div className="press-genres">
      {items.map((g) => (
        <button
          key={g.key}
          type="button"
          title={g.description}
          className="press-chip"
          onClick={() => onPick(g.label)}
        >
          {g.label}
        </button>
      ))}
    </div>
  );
}

// The pushed edition, as a MESSAGE BLOCK (N0): each story a preview
// card — headline, first lines, bylines — expandable to the full story.
// The tap is the honest moment the reading measurement starts.
export function StoryPreviewBlock({
  items,
  onOpen,
}: {
  items: StoryPreview[];
  onOpen: (storyId: string) => void;
}) {
  const tr = useT();
  return (
    <div className="story-block">
      {items.map((s) => (
        <button
          key={s.story_id}
          type="button"
          className="story-preview"
          title={tr("press.open")}
          onClick={() => onOpen(s.story_id)}
        >
          <span className="press-title">{s.headline}</span>
          <span className="press-body muted">{s.preview}</span>
          <span className="story-preview-meta muted">
            {s.bylines.join(", ")}
          </span>
        </button>
      ))}
    </div>
  );
}

// The full story, as the pane (the FileView pattern: the reader IS the
// view, with a way back — never a window popping on top). The reading
// is measured honestly while it is open: dwell from open to leave,
// completion when the reader reaches the end — sent ONCE as a `read`
// receipt on the way out, recorded server-side only under the member's
// own consent. The like tap and the story's k-anonymous benchmark
// numbers live here too.
export function StoryReader({
  storyId,
  onBack,
}: {
  storyId: string;
  onBack: () => void;
}) {
  const tr = useT();
  const [story, setStory] = useState<Story | null>(null);
  const [metrics, setMetrics] = useState<StoryMetrics | null>(null);
  const [error, setError] = useState("");
  const [noted, setNoted] = useState("");
  const openedAt = useRef(Date.now());
  const finished = useRef(false);
  const receiptSent = useRef(false);
  const bodyRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    let cancelled = false;
    api
      .pressStoryDetail(storyId)
      .then((s) => {
        if (!cancelled) setStory(s);
      })
      .catch((e) => {
        if (!cancelled) setError((e as Error).message);
      });
    api
      .pressStoryMetrics(storyId)
      .then((m) => {
        if (!cancelled) setMetrics(m);
      })
      .catch(() => {}); // a host without metrics keeps an honest silence
    return () => {
      cancelled = true;
    };
  }, [storyId]);

  // A story short enough to show whole was read whole.
  useEffect(() => {
    const el = bodyRef.current;
    if (story && el && el.scrollHeight <= el.clientHeight + 8) {
      finished.current = true;
    }
  }, [story]);

  function sendReceipt() {
    if (receiptSent.current) return;
    receiptSent.current = true;
    void api
      .pressStoryFeedback(storyId, "read", {
        dwellMs: Date.now() - openedAt.current,
        completed: finished.current,
      })
      .catch(() => {}); // consent-off or offline: the server said its piece
  }

  // Leaving by ANY road (back, unmount) sends the one receipt.
  useEffect(() => sendReceipt, []); // eslint-disable-line react-hooks/exhaustive-deps

  if (error) return <div className="pane-empty">{error}</div>;
  if (!story) return <div className="pane-empty muted">…</div>;
  return (
    <div className="story-reader">
      <div className="file-head">
        <button
          className="linklike"
          onClick={() => {
            sendReceipt();
            onBack();
          }}
        >
          {tr("press.backToThread")}
        </button>
        <span className="press-title">{story.headline}</span>
      </div>
      <div
        ref={bodyRef}
        className="story-reader-body"
        onScroll={(e) => {
          const el = e.currentTarget;
          if (el.scrollTop + el.clientHeight >= el.scrollHeight - 8) {
            finished.current = true;
          }
        }}
      >
        {story.prose.split(/\n{2,}/).map((block, i) => (
          <p key={i}>{block}</p>
        ))}
        {(story.media ?? []).length > 0 && (
          <div className="press-media-strip">
            {(story.media ?? []).map((m) => (
              <PressMedia
                key={`${m.contribution_id}:${m.index}`}
                contributionId={m.contribution_id}
                index={m.index}
                mediaType={m.media_type}
                name={m.name}
              />
            ))}
          </div>
        )}
        <div className="press-bylines">
          {[...new Set(story.lineage.map((share) => share.author))].map(
            (author) => (
              <Byline key={author} username={author} size={20} />
            ),
          )}
        </div>
        {/* The end marker: reaching it IS completion. */}
        <div className="story-end" aria-hidden="true" />
      </div>
      <div className="press-meta story-reader-foot">
        <button
          className="linklike press-tap"
          title={tr("press.like")}
          aria-label={tr("press.like")}
          onClick={() =>
            void api
              .pressStoryFeedback(storyId, "like")
              .then((v) =>
                setNoted(
                  v.recorded ? tr("press.noted") : tr("press.notRecorded"),
                ),
              )
              .catch(() => {})
          }
        >
          👍
        </button>
        {noted && <span className="muted">{noted}</span>}
        {/* The benchmark numbers, k-anonymous — or the honest reason. */}
        {metrics &&
          (metrics.revealed ? (
            <span className="muted">
              {tf("press.metricsLine", {
                opens: metrics.opens ?? 0,
                pct: Math.round(100 * (metrics.completion_rate ?? 0)),
                likes: metrics.likes ?? 0,
              })}
            </span>
          ) : (
            <span className="muted">{metrics.reason}</span>
          ))}
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

// The travel desk's surface (A7): the plan form (window, nights, party,
// budget), the brief with feasible plans ranked and broken constraints
// NAMED, and the member's own calendar beneath — one calendar, the same
// records a confirmed booking lands on. A party member who hasn't
// shared availability refuses the plan by name; the server's words
// render verbatim.
export function TravelPanel() {
  const tr = useT();
  const [start, setStart] = useState("");
  const [end, setEnd] = useState("");
  const [nights, setNights] = useState("3");
  const [party, setParty] = useState(session.principal ?? "");
  const [budget, setBudget] = useState("500");
  const [brief, setBrief] = useState<TravelBrief | null>(null);
  const [note, setNote] = useState("");
  const [events, setEvents] = useState<CalendarEvent[]>([]);
  const [newTitle, setNewTitle] = useState("");
  const [newDay, setNewDay] = useState("");

  const refreshCalendar = () =>
    api
      .calendarList()
      .then(({ items }) => setEvents(items ?? []))
      .catch(() => setEvents([]));

  useEffect(() => {
    void refreshCalendar();
  }, []);

  async function plan() {
    setNote("");
    setBrief(null);
    try {
      setBrief(
        await api.travelPlan({
          window_start: new Date(start).toISOString(),
          window_end: new Date(end).toISOString(),
          nights: parseInt(nights, 10) || 1,
          party,
          budget_micros: Math.round(parseFloat(budget) * 1_000_000) || 0,
        }),
      );
    } catch (e) {
      // "bob has not shared their availability" arrives verbatim.
      setNote(e instanceof Error ? e.message : String(e));
    }
  }

  async function addEvent() {
    if (!newTitle.trim() || !newDay) return;
    const day = new Date(newDay);
    try {
      await api.calendarAdd({
        title: newTitle,
        starts_at: day.toISOString(),
        ends_at: new Date(day.getTime() + 24 * 3600 * 1000).toISOString(),
      });
      setNewTitle("");
      void refreshCalendar();
    } catch {
      /* the list's next refresh tells the truth */
    }
  }

  return (
    <div className="press-panel travel">
      <div className="press-head">
        <span className="press-heading">{tr("travel.heading")}</span>
      </div>
      <div className="travel-form">
        <input
          type="date"
          aria-label={tr("travel.from")}
          value={start}
          onChange={(e) => setStart(e.target.value)}
        />
        <input
          type="date"
          aria-label={tr("travel.to")}
          value={end}
          onChange={(e) => setEnd(e.target.value)}
        />
        <input
          type="number"
          aria-label={tr("travel.nights")}
          min={1}
          value={nights}
          onChange={(e) => setNights(e.target.value)}
        />
        <input
          placeholder={tr("travel.partyPh")}
          value={party}
          onChange={(e) => setParty(e.target.value)}
        />
        <input
          type="number"
          aria-label={tr("travel.budget")}
          value={budget}
          onChange={(e) => setBudget(e.target.value)}
        />
        <button disabled={!start || !end} onClick={() => void plan()}>
          {tr("travel.plan")}
        </button>
      </div>
      {note && <div className="error">{note}</div>}
      {brief && (
        <>
          {brief.feasible.map((c, i) => (
            <div key={c.listing_id} className="press-card">
              <div className="press-byline">
                <span className="press-title">
                  {i === 0 ? "★ " : ""}
                  {c.title}
                </span>
                <span>{money(c.party_cost_micros, "USD")}</span>
              </div>
              <div className="muted">
                {c.seller} · {tr("explorer.score")} {c.score}
              </div>
              <details className="press-why">
                <summary>{tr("explorer.why")}</summary>
                <div className="muted">
                  {Object.entries(c.factors)
                    .map(([name, value]) => `${name}: ${value}`)
                    .join(" · ")}
                </div>
              </details>
            </div>
          ))}
          {brief.infeasible.map((c) => (
            <div key={c.listing_id} className="press-card infeasible">
              <div className="press-title muted">{c.title}</div>
              {/* Broken constraints carry names, never silent ranks. */}
              <div className="press-meta">
                {c.violations.map((v) => (
                  <span key={v} className="press-chip">
                    {v}
                  </span>
                ))}
              </div>
            </div>
          ))}
          {brief.feasible.length === 0 && brief.infeasible.length === 0 && (
            <div className="muted press-empty">{tr("travel.noSupply")}</div>
          )}
        </>
      )}
      <div className="press-head">
        <span className="press-heading">{tr("travel.calendar")}</span>
      </div>
      <div className="row">
        <input
          placeholder={tr("travel.eventPh")}
          value={newTitle}
          onChange={(e) => setNewTitle(e.target.value)}
        />
        <input
          type="date"
          aria-label={tr("travel.eventDay")}
          value={newDay}
          onChange={(e) => setNewDay(e.target.value)}
        />
        <button className="ghost" onClick={() => void addEvent()}>
          {tr("travel.addEvent")}
        </button>
      </div>
      {events.map((event) => (
        <div key={event.event_id} className="press-byline">
          <span>
            {event.source === "trip" ? "✈ " : ""}
            {event.title}
            <span className="muted">
            {" "}· {event.starts_at.slice(0, 10)}
            </span>
          </span>
          <button
            className="linklike"
            onClick={() =>
              void api
                .calendarDelete(event.event_id)
                .then(() => refreshCalendar())
                .catch(() => {})
            }
          >
            {tr("travel.remove")}
          </button>
        </div>
      ))}
    </div>
  );
}

// One attached piece of media, fetched with the bearer token and shown
// by its true type: a photo inline, a clip or a sound with controls, an
// honest named card for anything else (a PDF, a document) — and every
// shape carries the lossless download, straight from the fetched bytes.
// A reference whose file is gone (refs, never copies) renders nothing —
// honestly absent, never a broken box.
function PressMedia({
  contributionId,
  index,
  mediaType,
  name,
}: {
  contributionId: string;
  index: number;
  mediaType: string;
  name: string;
}) {
  const tr = useT();
  const [blob, setBlob] = useState<Blob | null>(null);
  const [url, setUrl] = useState<string | null>(null);
  useEffect(() => {
    let cancelled = false;
    let held: string | null = null;
    void api.pressMediaBlob(contributionId, index).then((bytes) => {
      if (!bytes || cancelled) return;
      held = URL.createObjectURL(bytes);
      setBlob(bytes);
      setUrl(held);
    });
    return () => {
      cancelled = true;
      if (held) URL.revokeObjectURL(held);
    };
  }, [contributionId, index]);
  if (!url || !blob) return null;
  const download = (
    <button
      type="button"
      className="linklike attachment-download"
      onClick={() => saveToDevice(name, blob)}
    >
      {tr("file.download")}
    </button>
  );
  if (mediaType.startsWith("image/")) {
    return (
      <figure className="attachment">
        <img className="press-media" src={url} alt={name} />
        <figcaption>{download}</figcaption>
      </figure>
    );
  }
  if (mediaType.startsWith("video/")) {
    return (
      <figure className="attachment">
        <video className="press-media" src={url} controls title={name} />
        <figcaption>{download}</figcaption>
      </figure>
    );
  }
  if (mediaType.startsWith("audio/")) {
    return (
      <figure className="attachment">
        <audio className="press-media" src={url} controls title={name} />
        <figcaption>{download}</figcaption>
      </figure>
    );
  }
  return (
    <span className="attachment attachment-card">
      📎 {name} {download}
    </span>
  );
}

// The contribution spine's surface (A1, amended): the shelf of live
// pieces and the caller's edition. Everything renders from the server's
// own words — the taxonomy, the bylines — never hardcoded copies; a
// host from before the press (404 on genres) renders nothing at all.
// There is NO contribute form: publishing happens in the conversation
// below — the desk detects raw material, reviews it, and a plain yes
// publishes (the OoLu build-a-node shape, applied to the press).
export function PressPanel({
  onOpenStory,
}: {
  // Opening a story swaps the pane for the reader (N0) — where the
  // reading is honestly measured. Optional: a panel without a reader
  // still lists the edition.
  onOpenStory?: (storyId: string) => void;
} = {}) {
  const tr = useT();
  const [genres, setGenres] = useState<PressGenre[] | null>(null);
  const [shelf, setShelf] = useState<Contribution[]>([]);
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
      .then(({ items }) => setShelf(items ?? []))
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
        void refreshShelf();
        void refreshStories();
      })
      .catch(() => setGenres(null)); // no press on this host: no panel
  }, []);

  if (genres === null) return null;

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
              {onOpenStory ? (
                <button
                  type="button"
                  className="linklike press-title story-open"
                  title={tr("press.open")}
                  onClick={() => onOpenStory(story.story_id)}
                >
                  {story.headline}
                </button>
              ) : (
                <div className="press-title">{story.headline}</div>
              )}
              <div className="press-body">{story.prose}</div>
              {/* The lineage's attached media — photos, clips, sound —
                  by reference through the press media door. */}
              {(story.media ?? []).length > 0 && (
                <div className="press-media-strip">
                  {(story.media ?? []).map((m) => (
                    <PressMedia
                      key={`${m.contribution_id}:${m.index}`}
                      contributionId={m.contribution_id}
                      index={m.index}
                      mediaType={m.media_type}
                      name={m.name}
                    />
                  ))}
                </div>
              )}
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
                {/* The taps speak emoji; the words stay for the screen
                    reader. A tap adjusts the member's semantic taste —
                    under their own consent, immediately. */}
                <button
                  className="linklike press-tap"
                  title={tr("press.like")}
                  aria-label={tr("press.like")}
                  onClick={() => void feedback(story, "like")}
                >
                  👍
                </button>
                <button
                  className="linklike press-tap"
                  title={tr("press.skip")}
                  aria-label={tr("press.skip")}
                  onClick={() => void feedback(story, "skip")}
                >
                  ⏭️
                </button>
                {noted[story.story_id] && (
                  <span className="muted">{noted[story.story_id]}</span>
                )}
              </div>
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
      </div>

      {shelf.length === 0 && (
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
          {(c.media ?? []).length > 0 && (
            <div className="press-media-strip">
              {(c.media ?? []).map((m, index) => (
                <PressMedia
                  key={`${c.contribution_id}:${index}`}
                  contributionId={c.contribution_id}
                  index={index}
                  mediaType={m.media_type}
                  name={m.name}
                />
              ))}
            </div>
          )}
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

// One server turn as the thread renders it — kinds filtered, block and
// files riding along (they persist with the turn now).
function fromServerTurns(items: import("../api").ChatHistoryTurn[]): AgentMsg[] {
  return items
    .filter((t) => t.kind === "user" || t.kind === "assistant")
    .map(
      (t): AgentMsg => ({
        kind: t.kind as "user" | "assistant",
        text: t.body,
        files: t.files,
        block: (t.block as ChatBlock | null | undefined) ?? null,
      }),
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
  // The open story: when set, the reader IS the pane (a way back, the
  // FileView discipline) and the reading measurement is running.
  const [reading, setReading] = useState<string | null>(null);
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
          setThread(fromServerTurns(items));
        }
      })
      .catch(() => {});
  }, [agent.agent_id]);

  // Arrival (N0): a pushed edition (or brief) lands as the agent's own
  // turn server-side — the thread re-reads on a quiet rhythm so it is
  // SEEN without a manual click. Never while a send is in flight, and
  // only a real change re-renders.
  useEffect(() => {
    if (busy || reading !== null) return;
    const t = setInterval(() => {
      void api
        .chatHistory(agent.agent_id)
        .then(({ items }) => {
          if (!items || items.length === 0) return;
          const mapped = fromServerTurns(items);
          setThread((current) =>
            mapped.length !== current.length ? mapped : current,
          );
        })
        .catch(() => {});
    }, 30_000);
    return () => clearInterval(t);
  }, [agent.agent_id, busy, reading]);

  // Attachments on the News thread: raw material for the desk — a
  // photo, a clip, a song — uploaded from the device (the blob door
  // keeps the true bytes; a host without one takes what fits inline)
  // and sent WITH the message, so the desk reviews words and evidence
  // together.
  const [pending, setPending] = useState<
    { id: string; name: string; mediaType: string }[]
  >([]);

  async function attach() {
    const picked = await pickLocalFiles();
    for (const file of picked) {
      try {
        // Blob door first — TRUE bytes, lossless preview and download;
        // only a blob-less host falls back inline (saveToDrawer).
        const saved = await api.saveToDrawer(file);
        setPending((p) =>
          p.some((f) => f.id === saved.file_id) || p.length >= 6
            ? p
            : [
                ...p,
                {
                  id: saved.file_id,
                  name: saved.name,
                  mediaType: saved.media_type,
                },
              ],
        );
      } catch (e) {
        setThread((t) => [
          ...t,
          {
            kind: "assistant",
            text: e instanceof Error ? e.message : tr("agent.sendFailed"),
          },
        ]);
      }
    }
  }

  async function send(spoken?: string) {
    const message = (spoken ?? draft).trim();
    if ((!message && pending.length === 0) || busy) return;
    const fileIds = pending.map((f) => f.id);
    const sent: TurnFileRef[] = pending.map((f) => ({
      file_id: f.id,
      name: f.name,
      media_type: f.mediaType,
    }));
    const shown =
      message ||
      pending.map((f) => f.name).join(", "); // a bare attachment still shows
    if (spoken === undefined) setDraft("");
    setPending([]);
    setThread((t) => [
      ...t,
      {
        kind: "user",
        text: shown,
        files: sent.length > 0 ? sent : undefined,
      },
    ]);
    setBusy(true);
    try {
      const history = thread.map((m) => ({
        role: m.kind,
        content: m.text,
      }));
      const turn = await api.chat(
        message || shown,
        history,
        undefined,
        undefined,
        agent.agent_id,
        fileIds.length > 0 ? fileIds : undefined,
      );
      setThread((t) => [
        ...t,
        {
          kind: "assistant",
          text: turn.reply,
          reasoning: turn.reasoning,
          block: (turn.block as ChatBlock | null | undefined) ?? null,
        },
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

  // An open story replaces the thread with the reader — the whole pane
  // for the whole story, with a way back (the FileView pattern).
  if (reading !== null) {
    return (
      <StoryReader
        key={reading}
        storyId={reading}
        onBack={() => setReading(null)}
      />
    );
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
        {agent.agent_id === "news" && <PressPanel onOpenStory={setReading} />}
        {/* The marketplace: every function block as a form block IN
            this conversation — shop, requests, approvals, orders,
            sell — plus the brief and the list-out. */}
        {agent.agent_id === "market" && <MarketPanel />}
        {/* And the explorer desk (A6) heads the Explorer thread. */}
        {agent.agent_id === "explorer" && <ExplorerPanel />}
        {/* The travel desk (A7) heads the Travel Plan thread. */}
        {agent.agent_id === "travel" && <TravelPanel />}
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
            {/* What rode along: previews with a lossless download. */}
            {m.kind === "user" && <AttachmentStrip files={m.files} />}
            {/* The pushed edition (N0): previews expandable to the
                full story — opening one starts the honest reading
                measurement. */}
            {m.block?.kind === "story" && (
              <StoryPreviewBlock
                items={m.block.items}
                onOpen={setReading}
              />
            )}
            {/* The block in the bubble: the genre chips whose tap
                speaks back into the conversation. */}
            {m.block?.kind === "genres" && (
              <GenreChipsBlock
                items={m.block.items}
                onPick={(label) => void send(label)}
              />
            )}
            {m.block?.kind === "chart" && (
              <ChartBlock
                title={m.block.title}
                unit={m.block.unit}
                points={m.block.points}
              />
            )}
            {/* The Explorer's followable categories: the tap SPEAKS. */}
            {m.block?.kind === "categories" && (
              <div className="press-genres">
                {m.block.items.map((c) => (
                  <button
                    key={c.category}
                    type="button"
                    className={`press-chip${c.followed ? " on" : ""}`}
                    onClick={() => void send(`follow ${c.category}`)}
                  >
                    {c.category}
                    {c.followed ? " ✓" : ""}
                  </button>
                ))}
              </div>
            )}
            {/* The closest products, under the inferred lens — with the
                comparison's own honest deadline. */}
            {m.block?.kind === "products" && (
              <div className="press-media-strip products-block">
                {m.block.items.map((x) => (
                  <div key={x.listing_id} className="press-card">
                    <span className="press-title">{x.title}</span>
                    <span className="muted">
                      {(x.unit_price_micros / 1_000_000).toFixed(2)}{" "}
                      {x.currency}
                      {x.category ? ` · ${x.category}` : ""}
                    </span>
                  </div>
                ))}
                <span className="muted">
                  {m.block.mode} · until{" "}
                  {m.block.expires_at.slice(0, 16).replace("T", " ")}
                </span>
              </div>
            )}
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
      {pending.length > 0 && (
        <div className="press-pending">
          {pending.map((f) => (
            <button
              key={f.id}
              type="button"
              className="press-chip"
              title={tr("press.detach")}
              onClick={() =>
                setPending((p) => p.filter((x) => x.id !== f.id))
              }
            >
              {f.name} ✕
            </button>
          ))}
        </div>
      )}
      <div className="chat-composer">
        {/* The News desk takes raw material: 📎 rides the message, and
            the desk reviews words and attachments together. */}
        {agent.agent_id === "news" && (
          <button
            type="button"
            className="ghost press-attach-btn"
            title={tr("press.upload")}
            aria-label={tr("press.upload")}
            onClick={() => void attach()}
          >
            📎
          </button>
        )}
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
