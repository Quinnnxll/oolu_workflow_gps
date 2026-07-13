import { useCallback, useEffect, useRef, useState } from "react";
import { api, TERMINAL_PHASES } from "../api";
import type { ChatAction, ChatHistoryTurn } from "../api";
import { humanizeEvent, statusSentence } from "../humanize";
import { conciseName } from "../naming";
import { t, tf, useT } from "../ui";
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
import {
  capturePhoto,
  currentPosition,
  fileToDrawerContent,
  photoName,
  photoToDataUrl,
  pickLocalFiles,
} from "../device";
import { createRecognizer, speak, speechInputSupported } from "../voice";
import type { Recognizer } from "../voice";
import {
  REMIND_CHECK_MS,
  reminderDue,
  reminderRuns,
  reminderText,
  returnedFromAway,
} from "../reminders";
import { Clarification } from "./Clarification";
import { ForwardMenu } from "./ForwardMenu";

// The whole product face: one conversation with OoLu. Work the assistant
// starts appears inline as a live run card — status, questions, decisions,
// result — so the machinery (skills, paths, synthesis) stays invisible.

type Msg =
  | { kind: "user"; text: string }
  | {
      kind: "assistant";
      text: string;
      actions?: ChatAction[];
      // The model's own thinking behind this reply, when it showed it.
      reasoning?: string;
    }
  // The chat's own nudge about unfinished work — not a model turn, so it
  // never enters the history sent to the assistant. Each mentioned task
  // rides along as an arrow pointing back to its action window.
  | { kind: "reminder"; text: string; runs?: { runId: string; label: string }[] }
  // OoLu asking for one of THIS device's senses. The request renders as
  // grant/decline buttons; only a grant runs the sense — the user decides,
  // never a silent sensor read. `done` freezes the buttons afterwards.
  | { kind: "device"; device: string; done?: boolean }
  | { kind: "run"; runId: string };

const CHAT_KEY = "oolu_chat";
// Shown once, before the first conversation: three concrete steps from
// zero to a first real task. Dismissed forever the moment it's used.
const FIRST_RUN_KEY = "oolu_first_run_done";
const FIRST_TASK =
  "fetch https://example.com and keep what it says as a note";
// Holding Send this long starts a voice conversation instead of sending.
const LONG_PRESS_MS = 550;

// The presence line under the name — what the companion is up to,
// phrased like a friend's status, not a system state. Dictionary keys,
// so the presence follows app.language like the rest of the chrome.
const MOOD_KEY: Record<Mood, string> = {
  calm: "mood.calm",
  happy: "mood.happy",
  thinking: "mood.thinking",
  worried: "mood.worried",
  excited: "mood.excited",
};

// Quick starts: one tap into the real command surface. The LABEL follows
// the interface language; the message stays the deterministic English
// command the assistant's rule matcher already answers.
const QUICK_STARTS: { labelKey: string; message: string }[] = [
  { labelKey: "quick.whatCanYouDo", message: "what can you do" },
  { labelKey: "quick.myTasks", message: "my tasks" },
  { labelKey: "quick.myFiles", message: "list files" },
  { labelKey: "quick.myNodes", message: "my nodes" },
  { labelKey: "quick.mySettings", message: "settings" },
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
  // the user has been idle a while, ONE reminder bubble lists what is
  // still running and what waits on them — it stays at the bottom of the
  // thread, never repeated while it sits there. After a long absence the
  // loop goes dormant; the user's return earns the next look (in send).
  const clockRef = useRef({ lastActivityAt: Date.now(), lastReminderAt: 0 });
  useEffect(() => {
    const t = setInterval(async () => {
      const now = Date.now();
      if (!reminderDue(clockRef.current, now)) return;
      try {
        const items = (await api.runs()).items;
        const text = reminderText(items);
        if (!text) return;
        clockRef.current.lastReminderAt = now;
        setThread((thread_) => [
          ...thread_,
          { kind: "reminder", text, runs: reminderRuns(items) },
        ]);
      } catch {
        /* the run list being unreachable is never worth a chat error */
      }
    }, REMIND_CHECK_MS);
    return () => clearInterval(t);
  }, []);

  // The device's senses are OoLu's to REQUEST and the user's to grant:
  // a turn may carry a device ask (location / camera / file), rendered
  // as grant buttons — the browser/app permission prompt appears only
  // after the user grants, never at startup, never from a hidden menu.
  function settleDeviceRequest(index: number) {
    setThread((t) =>
      t.map((m, i) =>
        i === index && m.kind === "device" ? { ...m, done: true } : m,
      ),
    );
  }

  async function shareLocation() {
    try {
      const here = await currentPosition();
      void send(
        `my location right now: ${here.lat.toFixed(5)}, ${here.lon.toFixed(5)}` +
          ` (±${here.accuracy_m} m)`,
      );
    } catch (e) {
      setThread((t) => [...t, { kind: "assistant", text: (e as Error).message }]);
    }
  }

  async function takePhoto() {
    try {
      const shot = await capturePhoto();
      if (!shot) return; // a cancelled camera is not an event
      const dataUrl = await photoToDataUrl(shot);
      const name = photoName();
      await api.createFile(name, dataUrl, undefined, "camera", "image/jpeg");
      void send(
        `I just took a photo on this device — it is in Files as “${name}”` +
          " (folder: camera).",
      );
    } catch (e) {
      setThread((t) => [...t, { kind: "assistant", text: (e as Error).message }]);
    }
  }

  async function pickDeviceFile() {
    try {
      const picked = await pickLocalFiles();
      if (picked.length === 0) return; // a cancelled picker is not an event
      const landed: string[] = [];
      for (const file of picked) {
        try {
          const { content, mediaType } = await fileToDrawerContent(file);
          await api.createFile(file.name, content, undefined, "", mediaType);
        } catch (e) {
          // Past the inline cap: the blob door carries the full bytes.
          if (!/too large/i.test((e as Error).message)) throw e;
          await api.uploadFileBytes(file);
        }
        landed.push(file.name);
      }
      void send(
        `I picked ${landed.length === 1 ? "a file" : `${landed.length} files`}` +
          ` from this device — in Files as ${landed
            .map((n) => `“${n}”`)
            .join(", ")}.`,
      );
    } catch (e) {
      setThread((t) => [...t, { kind: "assistant", text: (e as Error).message }]);
    }
  }

  // The reminder's arrow: straight back to the task's action window. If
  // its run card is in this thread, scroll to it and flash it; if not,
  // bring the card here — the card IS the action window.
  function jumpToRun(runId: string) {
    const existing = document.getElementById(`run-${runId}`);
    if (existing) {
      existing.scrollIntoView?.({ block: "center", behavior: "smooth" });
      existing.classList.add("flash");
      window.setTimeout(() => existing.classList.remove("flash"), 1600);
      return;
    }
    setThread((t) => [...t, { kind: "run", runId }]);
  }

  async function send(message?: string) {
    const text = (message ?? draft).trim();
    if (!text || busy) return;
    setDraft("");
    setBusy(true);
    // Coming back from a long absence (the reminder loop went dormant)
    // earns one fresh look at the open work — posted after the reply.
    const wasAway = returnedFromAway(clockRef.current, Date.now());
    clockRef.current.lastActivityAt = Date.now();
    // The face glows and breathes while the model reasons — the user
    // knows OoLu is still working on it, not hung.
    updateAvatarSignals({ userMood: deriveUserMood(text), thinking: true });
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
          {
            kind: "assistant",
            text: turn.reply,
            actions: turn.actions,
            reasoning: turn.reasoning || undefined,
          },
        ];
        // OoLu asked for a device sense: the request lands as grant
        // buttons right under its words — the user decides.
        if (
          turn.device === "location" ||
          turn.device === "camera" ||
          turn.device === "file"
        ) {
          next.push({ kind: "device", device: turn.device });
        }
        if (turn.run_id) next.push({ kind: "run", runId: turn.run_id });
        return next;
      });
      // The welcome-back reminder: the user was gone long enough for the
      // idle loop to go dormant, so their return is the moment the open
      // work surfaces again — once, right after their answer.
      if (wasAway) {
        try {
          const items = (await api.runs()).items;
          const text = reminderText(items);
          if (text) {
            clockRef.current.lastReminderAt = Date.now();
            setThread((t) => [
              ...t,
              { kind: "reminder", text, runs: reminderRuns(items) },
            ]);
          }
        } catch {
          /* the run list being unreachable is never worth a chat error */
        }
      }
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
      updateAvatarSignals({ thinking: false });
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
          <div className="chat-head-sub">{tr(MOOD_KEY[mood])}</div>
        </div>
      </div>
      <div className="chat-thread">
        {thread.length === 0 && (
          <>
            <div className="bubble assistant">{tr("chat.welcome")}</div>
            <div className="quickstarts">
              {QUICK_STARTS.map((q) => (
                <button
                  key={q.labelKey}
                  className="quickstart"
                  onClick={() => void send(q.message)}
                >
                  {tr(q.labelKey)}
                </button>
              ))}
            </div>
            {firstRun && (
              <div className="bubble assistant first-run">
                <div>{tr("chat.firstRunTitle")}</div>
                <ol>
                  <li>
                    <button
                      className="linklike"
                      onClick={() => {
                        finishFirstRun();
                        void send("hi");
                      }}
                    >
                      {tr("chat.sayHi")}
                    </button>{" "}
                    {tr("chat.sayHiTail")}
                  </li>
                  <li>
                    <button
                      className="linklike"
                      onClick={() => {
                        finishFirstRun();
                        setDraft(FIRST_TASK);
                      }}
                    >
                      {tr("chat.tryTask")}
                    </button>{" "}
                    {tr("chat.tryTaskTail")}
                  </li>
                  <li>{tr("chat.brainTip")}</li>
                </ol>
                <button className="linklike" onClick={finishFirstRun}>
                  {tr("chat.gotIt")}
                </button>
              </div>
            )}
          </>
        )}
        {thread.map((m, i) =>
          m.kind === "run" ? (
            <div key={`${m.runId}-${i}`} id={`run-${m.runId}`} className="run-anchor">
              <RunCard runId={m.runId} />
            </div>
          ) : m.kind === "reminder" ? (
            <div key={i} className="bubble assistant reminder">
              <span className="reminder-chip">{tr("chat.reminderChip")}</span>
              {m.text}
              {m.runs && m.runs.length > 0 && (
                <div className="reminder-links">
                  {m.runs.map((r) => (
                    <button
                      key={r.runId}
                      className="linklike"
                      title={tr("chat.openTask")}
                      onClick={() => jumpToRun(r.runId)}
                    >
                      ↦ {r.label}
                    </button>
                  ))}
                </div>
              )}
            </div>
          ) : m.kind === "device" ? (
            // OoLu's ask for a device sense: the user grants or declines.
            // Only a grant touches the sensor; a settled request keeps a
            // quiet record of the decision instead of live buttons.
            <div key={i} className="bubble assistant device-request">
              {m.done ? (
                <span className="muted">
                  {m.device === "location"
                    ? tr("device.locationSettled")
                    : m.device === "camera"
                      ? tr("device.cameraSettled")
                      : tr("device.fileSettled")}
                </span>
              ) : (
                <div className="reminder-links">
                  {m.device === "location" && (
                    <button
                      onClick={() => {
                        settleDeviceRequest(i);
                        void shareLocation();
                      }}
                    >
                      {tr("device.shareLocation")}
                    </button>
                  )}
                  {m.device === "camera" && (
                    <button
                      onClick={() => {
                        settleDeviceRequest(i);
                        void takePhoto();
                      }}
                    >
                      {tr("device.takePhoto")}
                    </button>
                  )}
                  {m.device === "file" && (
                    <button
                      onClick={() => {
                        settleDeviceRequest(i);
                        void pickDeviceFile();
                      }}
                    >
                      {tr("device.chooseFile")}
                    </button>
                  )}
                  <button
                    className="linklike"
                    onClick={() => settleDeviceRequest(i)}
                  >
                    {tr("device.notNow")}
                  </button>
                </div>
              )}
            </div>
          ) : (
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
                from={m.kind === "user" ? "me" : "OoLu"}
              />
            </div>
          ),
        )}
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
          placeholder={listening ? tr("chat.listening") : tr("messageOoLu")}
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
                ? tr("chat.tapToStop")
                : tr("chat.tapHold")
              : tr("send")
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
export function RunCard({ runId: initialRunId }: { runId: string }) {
  const tr = useT(); // the card's chrome follows app.language live
  // "Run again" resubmits the SAME intent as a fresh run and the card
  // follows the new attempt in place — one card, the latest truth.
  const [runId, setRunId] = useState(initialRunId);
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
    return <div className="run-card muted">{tr("run.gone")}</div>;
  }
  if (!task) {
    return <div className="run-card muted">{tr("run.starting")}</div>;
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
              {acting ? "…" : tr("run.approve")}
            </button>
            <button
              className="ghost"
              disabled={acting}
              onClick={() => void decide(() => api.confirm(task.run_id, false))}
            >
              {tr("run.reject")}
            </button>
          </div>
        </div>
      )}

      {task.awaiting === "incident" && (
        <div className="decision">
          {task.prompt && <p>{task.prompt}</p>}
          {task.user_retries > 0 && (
            <p className="muted">
              {task.user_retries === 1
                ? tr("run.retriesOne")
                : tf("run.retriesMany", { n: task.user_retries })}
              {task.user_retries >= 2 ? tr("run.nextRebuilds") : ""}
            </p>
          )}
          <div className="row">
            <button
              disabled={acting}
              onClick={() =>
                void decide(() => api.resolveIncident(task.run_id, "retry"))
              }
            >
              {acting ? tr("run.retrying") : tr("run.retry")}
            </button>
            <button
              className="ghost"
              disabled={acting}
              onClick={() =>
                void decide(() => api.resolveIncident(task.run_id, "abort"))
              }
            >
              {tr("run.abort")}
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
          {showSteps ? tr("run.hideSteps") : tr("run.showSteps")}
        </button>
        {task.can_cancel && !terminal && (
          <button
            className="linklike"
            onClick={async () => setTask(await api.cancel(task.run_id))}
          >
            {tr("cancel")}
          </button>
        )}
        {terminal && task.phase !== "completed" && (
          <button
            className="linklike"
            disabled={acting}
            onClick={async () => {
              // A dead run has nothing to resolve — resubmit the same
              // intent and let the engine re-plan (a node may exist now,
              // a dependency may have healed).
              setActing(true);
              setActError("");
              try {
                const fresh = await api.submitTask(task.intent);
                lastTaskJson.current = JSON.stringify(fresh);
                setTask(fresh);
                setRunId(fresh.run_id);
                setSteps(null);
                setShowSteps(false);
              } catch (e) {
                setActError((e as Error).message);
              } finally {
                setActing(false);
              }
            }}
          >
            {acting ? tr("run.retrying") : tr("run.runAgain")}
          </button>
        )}
      </div>

      {showSteps && (
        <div className="run-steps">
          {steps === null && <div className="muted">{tr("run.fetching")}</div>}
          {steps !== null && steps.length === 0 && (
            <div className="muted">{tr("run.nothingYet")}</div>
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

// The model's thinking behind a reply, dimmed and folded: one brief line
// by default (the "brief reasoning process"), the full monologue a tap
// away. Proof of work, never mistaken for the answer.
export function Reasoning({ text }: { text: string }) {
  const brief = text.replace(/\s+/g, " ").trim();
  return (
    <details className="reasoning">
      <summary className="reasoning-brief">
        {brief.length > 140 ? `${brief.slice(0, 140)}…` : brief}
      </summary>
      <div className="reasoning-full">{text}</div>
    </details>
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
  if (task.awaiting === "clarification") return t("status.needsAnswer");
  if (task.awaiting === "confirmation") return t("status.needsDecision");
  if (task.awaiting === "incident") return t("status.snag");
  if (task.phase === "completed") return t("status.done");
  if (task.phase === "failed") return t("status.failed");
  if (task.phase === "cancelled") return t("status.cancelled");
  return t("status.working");
}
