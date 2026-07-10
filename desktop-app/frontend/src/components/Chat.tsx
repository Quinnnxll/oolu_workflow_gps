import { useCallback, useEffect, useRef, useState } from "react";
import { api, TERMINAL_PHASES } from "../api";
import type { ChatAction, ChatHistoryTurn } from "../api";
import { humanizeEvent, statusSentence } from "../humanize";
import { conciseName } from "../naming";
import { useT } from "../ui";
import type { TaskView, TimelineEvent } from "../types";
import {
  currentAvatarSignals,
  deriveTone,
  deriveUserMood,
  moodOf,
  onAvatarSignals,
  updateAvatarSignals,
} from "../avatar";
import type { Mood } from "../avatar";
import { OoLuAvatar } from "./OoLuAvatar";
import { createRecognizer, speak, speechInputSupported } from "../voice";
import type { Recognizer } from "../voice";
import {
  REMIND_CHECK_MS,
  reminderDue,
  reminderText,
} from "../reminders";
import { Clarification } from "./Clarification";
import { ForwardMenu } from "./ForwardMenu";

// The whole product face: one conversation with OoLu. Work the assistant
// starts appears inline as a live run card — status, questions, decisions,
// result — so the machinery (skills, paths, synthesis) stays invisible.

type Msg =
  | { kind: "user"; text: string }
  | { kind: "assistant"; text: string; actions?: ChatAction[] }
  // The chat's own nudge about unfinished work — not a model turn, so it
  // never enters the history sent to the assistant.
  | { kind: "reminder"; text: string }
  | { kind: "run"; runId: string };

const CHAT_KEY = "oolu_chat";
// Shown once, before the first conversation: three concrete steps from
// zero to a first real task. Dismissed forever the moment it's used.
const FIRST_RUN_KEY = "oolu_first_run_done";
const FIRST_TASK =
  "fetch https://example.com and keep what it says as a note";
const WELCOME =
  "Hey! ⚡ I'm OoLu, your get-it-done sidekick. What are we tackling first?";
// Holding Send this long starts a voice conversation instead of sending.
const LONG_PRESS_MS = 550;

// The presence line under the name — what the companion is up to,
// phrased like a friend's status, not a system state.
const MOOD_LINE: Record<Mood, string> = {
  calm: "here with you",
  happy: "loving how that went ✨",
  thinking: "heads-down on your tasks",
  worried: "on it — sorting a problem",
  excited: "fired up and all ears! ⚡",
};

// Quick starts: one tap into the real command surface — each maps to a
// deterministic command or a rule the assistant already answers.
const QUICK_STARTS: { label: string; message: string }[] = [
  { label: "What can you do?", message: "what can you do" },
  { label: "My tasks", message: "my tasks" },
  { label: "My files", message: "list files" },
  { label: "My nodes", message: "my nodes" },
  { label: "My settings", message: "settings" },
];

function loadThread(): Msg[] {
  try {
    const raw = localStorage.getItem(CHAT_KEY);
    const parsed = raw ? (JSON.parse(raw) as Msg[]) : [];
    return Array.isArray(parsed) ? parsed : [];
  } catch {
    return [];
  }
}

// One thread across devices: a host that keeps history is the source of
// truth (this phone shows what that laptop said); localStorage remains the
// warm cache and the whole story on hosts without a history store.
// Reminder bubbles are presence, not conversation — they stay client-side.
function fromServer(items: ChatHistoryTurn[]): Msg[] {
  return items.map((turn): Msg => {
    if (turn.kind === "run") return { kind: "run", runId: turn.body };
    if (turn.kind === "assistant") return { kind: "assistant", text: turn.body };
    return { kind: "user", text: turn.body };
  });
}

export function Chat() {
  const tr = useT();
  const [thread, setThread] = useState<Msg[]>(loadThread);
  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState(false);
  // The one-time first-run guide: gone the moment it's used or dismissed.
  const [firstRun, setFirstRun] = useState(
    () => localStorage.getItem(FIRST_RUN_KEY) === null,
  );
  function finishFirstRun() {
    localStorage.setItem(FIRST_RUN_KEY, "1");
    setFirstRun(false);
  }
  const [listening, setListening] = useState(false);
  // Replies are spoken along with the message BY DEFAULT; the switch to
  // silence lives in Settings (app.voice_replies), not in the chat.
  const [speakReplies, setSpeakReplies] = useState(true);
  const endRef = useRef<HTMLDivElement>(null);
  const recognizerRef = useRef<Recognizer | null>(null);
  const pressRef = useRef<{ timer: number | null; long: boolean }>({
    timer: null,
    long: false,
  });
  const speakRef = useRef(speakReplies);
  speakRef.current = speakReplies;

  useEffect(() => {
    void api
      .settings()
      .then(({ items }) => {
        const voice = (items ?? []).find((i) => i.key === "app.voice_replies");
        if (voice) setSpeakReplies(voice.value === true);
      })
      .catch(() => {}); // settings unreachable: keep the spoken default
  }, []);
  // The companion lives here, at the head of the conversation.
  const [mood, setMood] = useState<Mood>(
    () => moodOf(currentAvatarSignals()).mood,
  );
  useEffect(() => onAvatarSignals((s) => setMood(moodOf(s).mood)), []);

  useEffect(() => {
    localStorage.setItem(CHAT_KEY, JSON.stringify(thread));
    endRef.current?.scrollIntoView?.({ block: "end" });
  }, [thread]);

  // On mount, ask the host for the account's thread. Present and
  // non-empty → it replaces the local cache (another device may have
  // talked since); absent (404 on history-less hosts) → local stands.
  useEffect(() => {
    void api
      .chatHistory()
      .then(({ items }) => {
        if (items && items.length > 0) setThread(fromServer(items));
      })
      .catch(() => {}); // no server history here: the cache is the thread
  }, []);

  // The conversation is endless, so unfinished work surfaces ITSELF: once
  // the user has been idle a while, a reminder bubble lists what is still
  // running and what waits on them — at a bounded cadence, never a storm.
  const clockRef = useRef({ lastActivityAt: Date.now(), lastReminderAt: 0 });
  useEffect(() => {
    const t = setInterval(async () => {
      const now = Date.now();
      if (!reminderDue(clockRef.current, now)) return;
      try {
        const text = reminderText((await api.runs()).items);
        if (!text) return;
        clockRef.current.lastReminderAt = now;
        setThread((thread_) => [...thread_, { kind: "reminder", text }]);
      } catch {
        /* the run list being unreachable is never worth a chat error */
      }
    }, REMIND_CHECK_MS);
    return () => clearInterval(t);
  }, []);

  async function send(message?: string) {
    const text = (message ?? draft).trim();
    if (!text || busy) return;
    setDraft("");
    setBusy(true);
    clockRef.current.lastActivityAt = Date.now();
    updateAvatarSignals({ userMood: deriveUserMood(text) });
    setThread((t) => [...t, { kind: "user", text }]);
    try {
      const history = thread
        .filter(
          (m): m is Extract<Msg, { kind: "user" } | { kind: "assistant" }> =>
            m.kind === "user" || m.kind === "assistant",
        )
        .slice(-12)
        .map((m) => ({
          role: m.kind === "user" ? ("user" as const) : ("assistant" as const),
          content: m.text,
        }));
      const turn = await api.chat(text, history, undefined, mood);
      updateAvatarSignals({ tone: deriveTone(turn.reply) });
      if (speakRef.current) {
        speak(turn.reply, moodOf(currentAvatarSignals()).mood);
        // The face mouths along for roughly as long as the reply lasts.
        updateAvatarSignals({ speaking: true });
        const ms = Math.min(8000, Math.max(1200, turn.reply.length * 55));
        setTimeout(() => updateAvatarSignals({ speaking: false }), ms);
      }
      setThread((t) => {
        const next: Msg[] = [
          ...t,
          { kind: "assistant", text: turn.reply, actions: turn.actions },
        ];
        if (turn.run_id) next.push({ kind: "run", runId: turn.run_id });
        return next;
      });
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

  function setEars(open: boolean) {
    setListening(open);
    updateAvatarSignals({ listening: open });
  }

  function startListening() {
    if (listening || !speechInputSupported()) return;
    // A fresh recognizer per press: dictation ends on the final result.
    recognizerRef.current = createRecognizer({
      onFinal: (text) => {
        setEars(false);
        void send(text);
      },
      onInterim: (text) => setDraft(text),
      onEnd: () => setEars(false),
    });
    if (recognizerRef.current) {
      setEars(true);
      recognizerRef.current.start();
    }
  }

  // The Send button is also the voice button: hold it to start a voice
  // conversation, tap to send (or to stop listening). The click after a
  // long press is swallowed so the hold never double-fires.
  function pressStart() {
    if (!speechInputSupported() || listening) return;
    pressRef.current.long = false;
    pressRef.current.timer = window.setTimeout(() => {
      pressRef.current.long = true;
      pressRef.current.timer = null;
      startListening();
    }, LONG_PRESS_MS);
  }

  function pressCancel() {
    if (pressRef.current.timer !== null) {
      clearTimeout(pressRef.current.timer);
      pressRef.current.timer = null;
    }
  }

  function pressFinish() {
    pressCancel();
    if (pressRef.current.long) {
      pressRef.current.long = false; // the hold already did its job
      return;
    }
    if (listening) {
      recognizerRef.current?.stop();
      setEars(false);
      return;
    }
    void send();
  }

  return (
    <div className="chat">
      <div className="chat-head">
        <OoLuAvatar size={64} />
        <div className="chat-head-body">
          <div className="chat-head-name">OoLu</div>
          <div className="chat-head-sub">{MOOD_LINE[mood]}</div>
        </div>
      </div>
      <div className="chat-thread">
        {thread.length === 0 && (
          <>
            <div className="bubble assistant">{WELCOME}</div>
            <div className="quickstarts">
              {QUICK_STARTS.map((q) => (
                <button
                  key={q.label}
                  className="quickstart"
                  onClick={() => void send(q.message)}
                >
                  {q.label}
                </button>
              ))}
            </div>
            {firstRun && (
              <div className="bubble assistant first-run">
                <div>First time here? A minute to your first task:</div>
                <ol>
                  <li>
                    <button
                      className="linklike"
                      onClick={() => {
                        finishFirstRun();
                        void send("hi");
                      }}
                    >
                      Say hi
                    </button>{" "}
                    — hear how I talk.
                  </li>
                  <li>
                    <button
                      className="linklike"
                      onClick={() => {
                        finishFirstRun();
                        setDraft(FIRST_TASK);
                      }}
                    >
                      Try a first task
                    </button>{" "}
                    — I'll put it in the box; press Send and watch it run
                    in Noder.
                  </li>
                  <li>
                    Give me a brain: open <b>Settings</b> in the list to add
                    a model key or point me at a local model — tasks run
                    without one, conversation gets smarter with one.
                  </li>
                </ol>
                <button className="linklike" onClick={finishFirstRun}>
                  Got it — hide this
                </button>
              </div>
            )}
          </>
        )}
        {thread.map((m, i) =>
          m.kind === "run" ? (
            <RunCard key={m.runId} runId={m.runId} />
          ) : m.kind === "reminder" ? (
            <div key={i} className="bubble assistant reminder">
              <span className="reminder-chip">reminder</span>
              {m.text}
            </div>
          ) : (
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
                from={m.kind === "user" ? "me" : "OoLu"}
              />
            </div>
          ),
        )}
        <div ref={endRef} />
      </div>
      <div className="chat-composer">
        <textarea
          placeholder={listening ? "Listening…" : tr("messageOoLu")}
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
        <button
          className={listening ? "listening" : ""}
          disabled={busy}
          title={
            speechInputSupported()
              ? listening
                ? "Listening — tap to stop"
                : "Tap to send · hold to speak"
              : "Send"
          }
          aria-label={listening ? "Stop listening" : "Send"}
          onPointerDown={pressStart}
          onPointerUp={pressCancel}
          onPointerLeave={pressCancel}
          onClick={pressFinish}
        >
          {listening ? "◉" : busy ? "…" : tr("send")}
        </button>
      </div>
    </div>
  );
}

// One piece of work, living inside the conversation. Polls while active;
// pauses surface as questions/decisions the user answers in place.
export function RunCard({ runId }: { runId: string }) {
  const [task, setTask] = useState<TaskView | null>(null);
  const [gone, setGone] = useState(false);
  const [steps, setSteps] = useState<TimelineEvent[] | null>(null);
  const [showSteps, setShowSteps] = useState(false);
  // A pressed decision shows it was pressed: buttons disable while the
  // call is out, and a refusal lands in the card instead of vanishing.
  const [acting, setActing] = useState(false);
  const [actError, setActError] = useState("");
  const lastTaskJson = useRef("");

  const refresh = useCallback(async () => {
    try {
      const fresh = await api.task(runId);
      // Only re-render on real change: the 2.5s poll must never rebuild
      // the DOM under the user's finger — that is how a button gets a
      // mousedown and then loses its mouseup, i.e. "cannot be pressed".
      const json = JSON.stringify(fresh);
      if (json !== lastTaskJson.current) {
        lastTaskJson.current = json;
        setTask(fresh);
      }
    } catch {
      // A vanished run (host wiped, other device) shouldn't wedge the chat.
      setGone(true);
    }
  }, [runId]);

  const decide = useCallback(async (call: () => Promise<TaskView>) => {
    setActing(true);
    setActError("");
    try {
      const fresh = await call();
      lastTaskJson.current = JSON.stringify(fresh);
      setTask(fresh);
    } catch (e) {
      setActError((e as Error).message);
    } finally {
      setActing(false);
    }
  }, []);

  const refreshSteps = useCallback(async () => {
    try {
      setSteps((await api.timeline(runId)).items);
    } catch {
      setSteps([]);
    }
  }, [runId]);

  const terminal = task !== null && TERMINAL_PHASES.includes(task.phase);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  useEffect(() => {
    if (terminal || gone) return;
    const t = setInterval(() => {
      void refresh();
      if (showSteps) void refreshSteps();
    }, 2500);
    return () => clearInterval(t);
  }, [terminal, gone, refresh, refreshSteps, showSteps]);

  if (gone) {
    return <div className="run-card muted">This task is no longer available.</div>;
  }
  if (!task) {
    return <div className="run-card muted">Starting…</div>;
  }

  return (
    <div className="run-card">
      <div className="run-card-head">
        <span className="run-card-intent" title={task.intent}>
          {conciseName(task.intent)}
        </span>
        <span className={`phase phase-${task.awaiting ?? task.phase}`}>
          {statusLabel(task)}
        </span>
      </div>
      <div className="run-voice">{statusSentence(task)}</div>

      {task.awaiting === "clarification" && (
        <Clarification task={task} onResolved={setTask} />
      )}

      {task.awaiting === "confirmation" && (
        <div className="decision">
          {task.prompt && <p>{task.prompt}</p>}
          <div className="row">
            <button
              disabled={acting}
              onClick={() => void decide(() => api.confirm(task.run_id, true))}
            >
              {acting ? "…" : "Approve"}
            </button>
            <button
              className="ghost"
              disabled={acting}
              onClick={() => void decide(() => api.confirm(task.run_id, false))}
            >
              Reject
            </button>
          </div>
        </div>
      )}

      {task.awaiting === "incident" && (
        <div className="decision">
          {task.prompt && <p>{task.prompt}</p>}
          {task.user_retries > 0 && (
            <p className="muted">
              {task.user_retries}{" "}
              {task.user_retries === 1 ? "retry" : "retries"} so far
              {task.user_retries >= 2
                ? " — the next retry lets OoLu plan and rebuild the path"
                : ""}
            </p>
          )}
          <div className="row">
            <button
              disabled={acting}
              onClick={() =>
                void decide(() => api.resolveIncident(task.run_id, "retry"))
              }
            >
              {acting ? "Retrying…" : "Retry"}
            </button>
            <button
              className="ghost"
              disabled={acting}
              onClick={() =>
                void decide(() => api.resolveIncident(task.run_id, "abort"))
              }
            >
              Abort
            </button>
          </div>
        </div>
      )}

      {actError && <div className="error">{actError}</div>}

      {task.failure_reason && <div className="error">{task.failure_reason}</div>}
      {task.result && (
        <pre className="result">{JSON.stringify(task.result, null, 2)}</pre>
      )}

      <div className="run-card-foot">
        <button
          className="linklike"
          onClick={() => {
            const next = !showSteps;
            setShowSteps(next);
            if (next && steps === null) void refreshSteps();
          }}
        >
          {showSteps ? "hide what I did" : "what I did"}
        </button>
        {task.can_cancel && !terminal && (
          <button
            className="linklike"
            onClick={async () => setTask(await api.cancel(task.run_id))}
          >
            cancel
          </button>
        )}
      </div>

      {showSteps && (
        <div className="run-steps">
          {steps === null && <div className="muted">Fetching the record…</div>}
          {steps !== null && steps.length === 0 && (
            <div className="muted">Nothing recorded yet.</div>
          )}
          {steps?.map((s, i) => (
            <div key={i} className="run-step" title={s.label}>
              <span className="run-step-dot">•</span>
              <span>{humanizeEvent(s.label)}</span>
              <span className="run-step-at">
                {new Date(s.at).toLocaleTimeString()}
              </span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

// The chip's verb: what the assistant actually touched this turn.
export function actionLabel(action: ChatAction): string {
  if (action.tool === "list_files") return "listed your files";
  if (action.tool === "read_file") return `read ${action.name ?? "a file"}`;
  if (action.tool === "write_file") return `updated ${action.name ?? "a file"}`;
  if (action.tool === "list_runs") return "checked your tasks";
  if (action.tool === "list_nodes") return "checked your nodes";
  if (action.tool === "run_log") return `reviewed run ${action.name ?? ""}`.trim();
  if (action.tool === "run_again") return `re-ran ${action.name ?? "a task"}`;
  if (action.tool === "node_holds") return "checked the pending requests";
  if (action.tool === "decide_hold")
    return `decided ${action.name ?? "a held request"}`;
  if (action.tool === "reply_hold")
    return `replied on ${action.name ?? "a held request"}`;
  if (action.tool === "build_node") return "built a node on the path";
  return action.tool;
}

function statusLabel(task: TaskView): string {
  if (task.awaiting === "clarification") return "needs an answer";
  if (task.awaiting === "confirmation") return "needs a decision";
  if (task.awaiting === "incident") return "hit a snag";
  if (task.phase === "completed") return "done";
  if (task.phase === "failed") return "failed";
  if (task.phase === "cancelled") return "cancelled";
  return "working…";
}
