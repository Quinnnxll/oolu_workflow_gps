# Imitation learning: the honest record button

Issue 14 asked two things: an audit of whether OoLu can actually watch
the user drive OTHER software (mouse/keyboard input, other apps' logs),
and a learning function built on whatever the audit finds. This document
is both — the audit's findings, and the design they forced.

## The capability audit (2026-07)

**Global mouse/keyboard input: NO.** The desktop shell is Tauri 2 with a
deliberately minimal capability surface: its only plugin is the shell
sidecar that launches the local gateway (`desktop-app/src-tauri/`), its
CSP is locked to loopback, and no input-hook or screen-capture crate is
anywhere in the tree. Every `keydown` handler in the frontend is an
in-app UI handler on a specific input. There is no keylogging and no
screen recording — and mobile platforms (Android/iOS) will not permit a
backend screen-recorder for third-party apps at all, so building one was
never a path.

**Other software's logs: NO.** Nothing reads other applications' logs.

**Our OWN execution record: YES, completely.**

- The hash-chained durable audit log stores every run event with its
  full payload (`src/oolu/durable/audit.py`).
- Each node materializes a daily `execution-YYYY-MM-DD.log` file — the
  full-fidelity legal record (timestamps, run ids, executing node, raw
  event types), pruned by `account.log_retention_days`.
- Script runs capture stdout, stderr, and an error class.
- Nodes can forbid run-data reuse (`autodev_blocked`) — learning from
  logs is consent-gated on the node side.

**File types: YES, broadly.** Inline text up to 1 MB; content-addressed
binary blobs up to 100 MB; CSV/TSV, JSON, PDF, Office, images, audio,
video recognized and rendered; arbitrary binary accepted through the
blob door.

**Prior art in the tree:** a browser-demonstration recorder already
exists (`src/oolu/skills/` — `BrowserObserver` injects page listeners,
`DemonstrationRecorder` scores against audit events, the compiler turns
a demonstration into a `ReusableSkill`; password fields never captured)
but it is reachable only through the CLI (`oolu record`), not the app.

## The design the audit forces

The original vision — press record, drive the other software, learn from
the watching — cannot be honest on this platform. But the user's own
conclusion in the issue is the right one: **things are easier and retain
all the critical parts when the program is executed through our node.**
Everything that runs through a node is already recorded completely,
verifiably, and with consent. So the lesson moves to where the record is.

### Imitate (Work → My nodes → node window, on the tab row)

The **Imitate** button sits at the right edge of the
Activity/Interact/Files row. Pressing it opens a guided lesson:

1. **Name the goal** — what should the new node do, one sentence.
2. **Demonstrate in order** — describe each step in the user's own
   words; steps are stored exactly as typed, in sequence. Meanwhile,
   run the real work through the node's own window (Interact, Files):
   every run the node executes while the lesson records is **paired
   automatically** from the same audit-backed activity feed the window
   shows — the user's words matched with what the machine verifiably did.
3. **Stop & build** — the demonstration compiles into ONE node through
   the same gated build path as every other door
   (`_build_function_node`): the model is consulted once, told the
   numbered steps ARE the plan and to imitate them exactly — never to
   re-plan. The node lands on the user's desk needing verification and
   earns trust as its runs verify, like every node.

A refusal (conversation-shaped goal, no usable function, twin guard)
keeps the lesson recording — nothing demonstrated is lost. Discarding
closes the lesson but keeps the record.

### Node creation requirements as training data

Every lesson persists twice, verbatim:

- as rows in the `LessonStore` (`src/oolu/lessons.py`) — goal, ordered
  steps with kinds (`say` / `run` / `file`), timestamps, outcome, the
  built node's id — erased with the account like every personal store;
- as `lessons/lesson-<id>.json` in the **built node's own drawer** —
  goal, steps, paired executions, who taught it, where.

That is the corpus later training rides on: demonstrations paired with
execution logs, in one stable schema, gathered with the user's explicit
hand on the button every time.
