import { useCallback, useEffect, useState, type FormEvent } from "react";
import { api, isRemote } from "../api";
import type {
  FileDoc,
  FileMeta,
  HoldItem,
  KycApplication,
  KycView,
  Lesson,
  NodeAccountView,
  NodeRunSteps,
  OrgTemplateView,
  WorkNode,
} from "../api";
import { identityHue } from "../avatar";
import { orderThreads } from "../conversations";
import { pickLocalFiles } from "../device";
import { humanizeEvent } from "../humanize";
import {
  displayNodeName,
  loadSidebarFolded,
  saveSidebarFolded,
  t,
  tf,
  useT,
} from "../ui";
import { FilesPane } from "./FilesPane";
import { NodeInteract, reliabilityLine } from "./NodeInteract";

// The Work environment: where a noder manages and observes their nodes.
// A node's REGIME — Supernode or not, under which Supernode, audit or
// not, auto-growing or not, and its authority level — is fixed when it is
// created and can never be changed afterwards, not even by the Supernode's
// humans; authority levels exist only under a Supernode. The
// list is node accounts (name, cumulative earnings, health); the thread is
// the node's execution feed plus, where humans are in control (audit nodes
// and Supernodes), the allow / sign / reply desk for held requests.

export function Work({ onLife }: { onLife: () => void }) {
  const tr = useT();
  const [nodes, setNodes] = useState<WorkNode[]>([]);
  const [selected, setSelected] = useState<string | "add" | null>(null);
  // Same fold + one-pane-on-a-phone behavior as Life — one shared choice.
  const [folded, setFolded] = useState(loadSidebarFolded);
  const [paneOpen, setPaneOpen] = useState(false);

  function open(next: string | "add") {
    setSelected(next);
    setPaneOpen(true);
  }

  const refresh = useCallback(async () => {
    try {
      setNodes((await api.workNodes()).items);
    } catch {
      setNodes([]);
    }
  }, []);

  useEffect(() => {
    void refresh();
    const t = setInterval(refresh, 7000);
    return () => clearInterval(t);
  }, [refresh]);

  const active = nodes.find((n) => n.node_id === selected);

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
          <button onClick={onLife}>{tr("life")}</button>
          <button className="on">{tr("work")}</button>
        </div>

        <div className="work-head">
          <span className="convo-group">{tr("work.myNodes")}</span>
          <button
            className="add-node"
            title={tr("work.addNodeTitle")}
            onClick={() => open("add")}
          >
            +
          </button>
        </div>

        {nodes.length === 0 && (
          <div className="convo-empty">{tr("work.empty")}</div>
        )}
        {orderThreads(nodes, (n) => n.last_activity ?? "").map((n) => (
          <button
            key={n.node_id}
            className={`convo ${selected === n.node_id ? "on" : ""}`}
            onClick={() => open(n.node_id)}
          >
            <span
              className="convo-avatar node"
              style={{
                background: `hsl(${identityHue(n.title)} 45% 34%)`,
                color: "#fff",
                borderColor: "transparent",
              }}
            >
              {n.account.is_supernode ? "◆" : n.title.slice(0, 1).toUpperCase()}
            </span>
            <span className="convo-body">
              <span className="convo-name">
                {n.pinned ? "📌 " : ""}
                {displayNodeName(n.title)}
                {n.muted ? " 🔕" : ""}
              </span>
              <span className="convo-sub">
                {money(n.earnings_micros)} · {healthLabel(n)}
              </span>
            </span>
          </button>
        ))}
      </aside>

      <section className="convo-pane">
        {/* One fold control only — it lives on the My-nodes column (and
            survives folding as the thin rail's toggle), so the
            conversation window carries no duplicate. */}
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
        {selected === "add" && (
          <AddNode
            onDone={(nodeId) => {
              void refresh();
              setSelected(nodeId);
            }}
          />
        )}
        {active && (
          <NodeThread
            key={active.node_id}
            node={active}
            allNodes={nodes}
            onOpenNode={(id) => open(id)}
            onChanged={refresh}
            onClosed={() => {
              setSelected(null);
              setPaneOpen(false);
              void refresh();
            }}
          />
        )}
        {!selected && (
          <div className="pane-empty">
            <KycReviewInbox />
            <p>{tr("work.pick")}</p>
            <p className="muted">{tr("work.pickSub")}</p>
          </div>
        )}
      </section>
    </div>
  );
}

function money(micros: number): string {
  return `$${(micros / 1_000_000).toFixed(2)}`;
}

// A node ID is a long uuid — noise the user never memorizes and shouldn't have
// to see in full. It shows masked (***-last six), and reveals only when the
// user presses the eye, or copies straight to the clipboard on the button.
function NodeIdChip({ value }: { value: string }): JSX.Element {
  const [shown, setShown] = useState(false);
  const [copied, setCopied] = useState(false);
  const masked = "***-" + value.slice(-6);
  return (
    <span className="node-id-chip">
      <span className="node-id-value" title={shown ? value : undefined}>
        {shown ? value : masked}
      </span>
      <button
        type="button"
        className="node-id-btn"
        aria-label={shown ? "Hide the node ID" : "Reveal the node ID"}
        title={shown ? "Hide" : "Reveal"}
        onClick={() => setShown((s) => !s)}
      >
        {shown ? "🙈" : "👁"}
      </button>
      <button
        type="button"
        className="node-id-btn"
        aria-label="Copy the full node ID"
        title="Copy"
        onClick={async () => {
          try {
            await navigator.clipboard.writeText(value);
            setCopied(true);
            setTimeout(() => setCopied(false), 1500);
          } catch {
            /* clipboard blocked: the eye still reveals it to copy by hand */
          }
        }}
      >
        {copied ? "✓" : "⧉"}
      </button>
    </span>
  );
}

// The feed's clock: down-to-the-second, nothing more — full ISO detail
// lives in the tooltip and the daily log files.
function clock(iso: string): string {
  return iso.includes("T") ? iso.split("T")[1].slice(0, 8) : iso;
}

// "(L4, Audit, Auto-growing)": say only what the node IS; silence for
// what it isn't (no auto-growing => no mention at all).
export function regimeTag(account: WorkNode["account"]): string {
  const parts: string[] = [];
  if (account.is_supernode) parts.push(t("regime.supernode"));
  if (account.authority_level != null) parts.push(`L${account.authority_level}`);
  if (account.audit_mode) parts.push(t("regime.audit"));
  if (account.allow_autodev_data) parts.push(t("regime.autogrow"));
  return parts.length ? `(${parts.join(", ")})` : `(${t("regime.standalone")})`;
}

// The owner's list margins on one node: pin, mute — and DELETE, which
// is real: the node leaves the desk, its Supernode's roster, and run
// resolution at once, revivable by an administrator for 7 days.
export function NodeMargins({
  node,
  onChanged,
  onClosed,
}: {
  node: WorkNode;
  onChanged: () => void;
  onClosed: () => void;
}) {
  const tr = useT();
  const [confirming, setConfirming] = useState(false);
  const [error, setError] = useState("");

  async function set(prefs: { pinned?: boolean; muted?: boolean }) {
    setError("");
    try {
      await api.setWorkNodePrefs(node.node_id, prefs);
      onChanged();
    } catch (e) {
      setError((e as Error).message);
    }
  }

  async function destroy() {
    setError("");
    try {
      await api.deleteWorkNode(node.node_id);
      onClosed();
    } catch (e) {
      setError((e as Error).message);
    }
  }

  return (
    <span className="node-margins">
      <button
        type="button"
        className="linklike"
        onClick={() => void set({ pinned: !node.pinned })}
      >
        {node.pinned ? tr("profile.unpin") : tr("profile.pin")}
      </button>
      <button
        type="button"
        className="linklike"
        onClick={() => void set({ muted: !node.muted })}
      >
        {node.muted ? tr("profile.unmute") : tr("profile.mute")}
      </button>
      {confirming ? (
        <>
          <span className="muted">{tr("work.deleteNodeHint")}</span>
          <button
            type="button"
            className="linklike danger"
            onClick={() => void destroy()}
          >
            {tr("profile.confirmDelete")}
          </button>
          <button
            type="button"
            className="linklike"
            onClick={() => setConfirming(false)}
          >
            {tr("cancel")}
          </button>
        </>
      ) : (
        <button
          type="button"
          className="linklike danger"
          onClick={() => setConfirming(true)}
        >
          {tr("profile.delete")}
        </button>
      )}
      {error && <span className="error">{error}</span>}
    </span>
  );
}

// Each regime trait wears its OWN tag — Supernode, L{n}, Audit,
// Auto-growing — never a parenthesized sentence.
export function RegimeTags({
  account,
}: {
  account: WorkNode["account"];
}) {
  const tags: string[] = [];
  if (account.is_supernode) tags.push(t("regime.supernode"));
  if (account.authority_level != null) tags.push(`L${account.authority_level}`);
  if (account.audit_mode) tags.push(t("regime.audit"));
  if (account.allow_autodev_data) tags.push(t("regime.autogrow"));
  if (tags.length === 0) tags.push(t("regime.standalone"));
  return (
    <span className="regime-tags">
      {tags.map((tag) => (
        <span key={tag} className="badge regime-tag">
          {tag}
        </span>
      ))}
    </span>
  );
}

function healthLabel(n: WorkNode): string {
  if (n.health.score === null) return t("work.noRunsYet");
  return tf("work.healthy", { pct: Math.round(n.health.score * 100) });
}

// ---- create (regime fixed forever) / onboard (no choices) -----------------
export function AddNode({
  onDone,
}: {
  onDone: (nodeId: string) => void;
}) {
  const tr = useT();
  const [mode, setMode] = useState<"create" | "onboard">("create");
  const [title, setTitle] = useState("");
  const [summary, setSummary] = useState("");
  const [nodeId, setNodeId] = useState("");
  const [isSupernode, setIsSupernode] = useState(false);
  const [audit, setAudit] = useState(false);
  const [autoGrow, setAutoGrow] = useState(true);
  const [policyOk, setPolicyOk] = useState(false);
  // A developer's own function, brought from outside OoLu. The node is
  // born WITH it (a script the sandbox runs and verifies, screened before
  // it is stored); left empty, the node starts as a draft as before.
  const [fnScript, setFnScript] = useState("");
  const [fnName, setFnName] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  async function submit(event: FormEvent) {
    event.preventDefault();
    setError("");
    if (mode === "create" && !policyOk) {
      // The Node Policy is agreed upfront or the node is not created.
      setError(t("work.policyFirst"));
      return;
    }
    setBusy(true);
    try {
      if (mode === "onboard") {
        await api.workOnboard(nodeId.trim());
        onDone(nodeId.trim());
        return;
      }
      const id = (
        await api.createNode(
          title.trim(),
          summary.trim(),
          fnScript.trim() || undefined,
        )
      ).node_id;
      await api.workAccountCreate(id, {
        is_supernode: isSupernode,
        // Membership is minted on the Supernode's own Access desk, so
        // the + makes standalone nodes (and org roots, which always
        // audit — humans in full control).
        supernode_id: null,
        audit_mode: isSupernode ? true : audit,
        allow_autodev_data: autoGrow,
        authority_level: null,
        accept_policy: policyOk,
      });
      onDone(id);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    // autoComplete off, here and per-field: the browser must not offer
    // values typed into earlier node forms as recommendations — that
    // history is machine-global (it ignores which account is signed in),
    // and every node is created for a different purpose anyway.
    <form className="add-pane" onSubmit={submit} autoComplete="off">
      <div className="mode-tabs">
        <button
          type="button"
          className={mode === "create" ? "on" : ""}
          onClick={() => setMode("create")}
        >
          {tr("work.createTab")}
        </button>
        <button
          type="button"
          className={mode === "onboard" ? "on" : ""}
          onClick={() => setMode("onboard")}
        >
          {tr("work.onboardTab")}
        </button>
      </div>

      {mode === "create" ? (
        <>
          <label htmlFor="node-title">{tr("work.name")}</label>
          <input
            id="node-title"
            value={title}
            required
            autoComplete="off"
            onChange={(e) => setTitle(e.target.value)}
          />
          <label htmlFor="node-summary">{tr("work.whatItDoes")}</label>
          <input
            id="node-summary"
            value={summary}
            autoComplete="off"
            onChange={(e) => setSummary(e.target.value)}
          />

          <label htmlFor="node-function">{tr("work.fnLabel")}</label>
          <div className="row">
            <button
              type="button"
              className="ghost"
              onClick={async () => {
                const [file] = await pickLocalFiles();
                if (!file) return;
                setFnName(file.name);
                setFnScript(await file.text());
              }}
            >
              {tr("work.uploadPy")}
            </button>
            {fnName && <span className="muted">{fnName}</span>}
          </div>
          <textarea
            id="node-function"
            className="node-function"
            placeholder={tr("work.fnPlaceholder")}
            value={fnScript}
            rows={6}
            onChange={(e) => setFnScript(e.target.value)}
          />

          <p className="muted fixed-note">{tr("work.fixedNote")}</p>

          <label className="checkline">
            <input
              type="checkbox"
              checked={isSupernode}
              onChange={(e) => setIsSupernode(e.target.checked)}
            />
            {tr("work.supernodeCheck")}
          </label>

          {/* Membership is minted on the Supernode's own Access desk
              (Access → member nodes); the + makes standalone nodes. */}
          <label className="checkline">
            <input
              type="checkbox"
              checked={isSupernode ? true : audit}
              disabled={isSupernode}
              onChange={(e) => setAudit(e.target.checked)}
            />
            {tr("work.auditCheck")}
          </label>
          <label className="checkline">
            <input
              type="checkbox"
              checked={autoGrow}
              onChange={(e) => setAutoGrow(e.target.checked)}
            />
            {tr("work.autogrowCheck")}
          </label>

          <label className="checkline policy">
            <input
              type="checkbox"
              checked={policyOk}
              onChange={(e) => setPolicyOk(e.target.checked)}
            />
            {tr("work.policyCheck")}
          </label>
        </>
      ) : (
        <>
          <p className="muted">{tr("work.onboardNote")}</p>
          <label htmlFor="node-id">{tr("work.nodeId")}</label>
          <input
            id="node-id"
            value={nodeId}
            required
            autoComplete="off"
            onChange={(e) => setNodeId(e.target.value)}
          />
        </>
      )}

      <button type="submit" disabled={busy}>
        {busy
          ? tr("work.working")
          : mode === "create"
            ? tr("work.createNode")
            : tr("work.onboard")}
      </button>
      {error ? <div className="error">{error}</div> : null}
    </form>
  );
}

// ---- the SOP dial: a member's execution order, set by the org's owner -----
// Work flows in ascending numbers — an explicit hand-off to the next node,
// like an SOP; members sharing a number run in PARALLEL; empty means no
// fixed place (called whenever needed). The server enforces WHO may turn
// the dial: only the parent Supernode's own humans.
export function MemberOrderDial({ member }: { member: WorkNode }) {
  const tr = useT();
  const [value, setValue] = useState(
    member.account.exec_order == null ? "" : String(member.account.exec_order),
  );
  const [saved, setSaved] = useState(member.account.exec_order ?? null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  async function save() {
    const order = value.trim() === "" ? null : Number(value.trim());
    if (order !== null && (!Number.isInteger(order) || order < 1)) {
      setError(tr("work.orderBad"));
      return;
    }
    if (order === saved) return;
    setError("");
    setBusy(true);
    try {
      const account = await api.setNodeOrder(member.node_id, order);
      setSaved(account.exec_order ?? null);
      setValue(account.exec_order == null ? "" : String(account.exec_order));
    } catch (e) {
      setError((e as Error).message);
      setValue(saved == null ? "" : String(saved));
    } finally {
      setBusy(false);
    }
  }

  return (
    <span className="order-dial" title={tr("work.orderHint")}>
      <input
        aria-label={`${tr("work.orderLabel")} ${displayNodeName(member.title)}`}
        placeholder={tr("work.onDemand")}
        inputMode="numeric"
        value={value}
        disabled={busy}
        onChange={(e) => setValue(e.target.value)}
        onBlur={() => void save()}
        onKeyDown={(e) => {
          if (e.key === "Enter") {
            e.preventDefault();
            void save();
          }
        }}
      />
      <span className="muted">
        {saved == null ? tr("work.onDemand") : tf("work.orderStep", { n: saved })}
      </span>
      {error && <span className="error">{error}</span>}
    </span>
  );
}

// ---- Imitate: a guided lesson in the node's window builds a node ----------
// The honest version of the record button: OoLu owns no screen or key
// recording (and mobile never will), so the user teaches through what the
// platform can verifiably see — name the goal, describe each step in
// order, run the real work through this node (the execution logs pair
// automatically), then stop-and-build. The demonstration persists as a
// training data log in the built node's drawer.
export function ImitatePanel({
  node,
  lesson,
  onLesson,
}: {
  node: WorkNode;
  lesson: Lesson | null;
  onLesson: (lesson: Lesson | null) => void;
}) {
  const tr = useT();
  const [goal, setGoal] = useState("");
  const [step, setStep] = useState("");
  const [say, setSay] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  async function run(fn: () => Promise<void>) {
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
    <div className="imitate-panel">
      <p className="muted">{tr("work.imitateHint")}</p>
      {!lesson && (
        <form
          className="setting-control row"
          onSubmit={(e) => {
            e.preventDefault();
            void run(async () => {
              onLesson(
                (await api.imitateStart(node.node_id, goal.trim())).lesson,
              );
              setGoal("");
              setSay("");
            });
          }}
        >
          <input
            aria-label={tr("work.imitateGoal")}
            placeholder={tr("work.imitateGoal")}
            value={goal}
            onChange={(e) => setGoal(e.target.value)}
          />
          <button type="submit" disabled={busy || !goal.trim()}>
            {tr("work.imitateStart")}
          </button>
        </form>
      )}
      {lesson && (
        <>
          <div className="convo-group">“{lesson.goal}”</div>
          {lesson.steps.length > 0 && (
            <ol className="imitate-steps">
              {lesson.steps.map((s) => (
                <li key={s.seq} className={s.kind}>
                  {s.kind === "say" ? s.text : `⚙ ${s.text}`}
                </li>
              ))}
            </ol>
          )}
          <form
            className="setting-control row"
            onSubmit={(e) => {
              e.preventDefault();
              void run(async () => {
                onLesson(
                  (await api.imitateStep(node.node_id, step.trim())).lesson,
                );
                setStep("");
              });
            }}
          >
            <input
              aria-label={tr("work.imitateStepPh")}
              placeholder={tr("work.imitateStepPh")}
              value={step}
              onChange={(e) => setStep(e.target.value)}
            />
            <button type="submit" disabled={busy || !step.trim()}>
              {tr("work.imitateAdd")}
            </button>
          </form>
          <div className="setting-control row">
            <button
              disabled={busy || lesson.steps.length === 0}
              onClick={() =>
                void run(async () => {
                  const done = await api.imitateStop(node.node_id, true);
                  setSay(done.say);
                  // A refusal keeps the lesson recording — nothing
                  // demonstrated is lost; a build closes it.
                  onLesson(
                    done.lesson.status === "recording" ? done.lesson : null,
                  );
                })
              }
            >
              {tr("work.imitateBuild")}
            </button>
            <button
              className="linklike"
              disabled={busy}
              onClick={() =>
                void run(async () => {
                  await api.imitateStop(node.node_id, false);
                  onLesson(null);
                  setSay("");
                })
              }
            >
              {tr("work.imitateDiscard")}
            </button>
          </div>
        </>
      )}
      {say && (
        <div className={say.startsWith("error:") ? "error" : "imitate-built"}>
          {say}
        </div>
      )}
      {error && <div className="error">{error}</div>}
    </div>
  );
}

// ---- one node's thread: fixed regime, feed, and the human desk ------------
export function NodeThread({
  node,
  allNodes,
  onOpenNode = () => {},
  onChanged = () => {},
  onClosed = () => {},
}: {
  node: WorkNode;
  allNodes: WorkNode[];
  // Access tab hands: open a member node's own card; tell the parent
  // the roster changed (a member was created here).
  onOpenNode?: (nodeId: string) => void;
  onChanged?: () => void;
  // Called when the node leaves the list from inside (delete).
  onClosed?: () => void;
}) {
  const tr = useT();
  const [activity, setActivity] = useState<NodeRunSteps[] | null>(null);
  const [holds, setHolds] = useState<HoldItem[]>([]);
  const [tab, setTab] = useState<
    "activity" | "interact" | "files" | "access" | "code"
  >("activity");
  // The Access tab's create-a-member form (Supernodes only).
  const [memberTitle, setMemberTitle] = useState("");
  const [memberAuthority, setMemberAuthority] = useState(1);
  const [memberSuper, setMemberSuper] = useState(false);
  const [memberBusy, setMemberBusy] = useState(false);
  const [memberError, setMemberError] = useState("");
  // Recently deleted members: the administrator's 7-day revival list.
  const [deletedMembers, setDeletedMembers] = useState<
    {
      node_id: string;
      title: string;
      deleted_at: string;
      revivable_until: string;
    }[]
  >([]);
  // A large fleet folds away for a clear view of the thread itself.
  const [showMembers, setShowMembers] = useState(true);
  // Imitate: the guided lesson recording in THIS node's window, if any.
  const [lesson, setLesson] = useState<Lesson | null>(null);
  const [imitateOpen, setImitateOpen] = useState(false);
  const account = node.account;
  const members = allNodes.filter(
    (n) => n.account.supernode_id === node.node_id,
  );
  const watchesHolds = account.audit_mode || account.is_supernode;

  const refresh = useCallback(async () => {
    try {
      setActivity((await api.workActivity(node.node_id)).items);
    } catch {
      setActivity([]);
    }
    if (account.is_supernode) {
      try {
        setDeletedMembers(
          (await api.deletedMembers(node.node_id)).items ?? [],
        );
      } catch {
        setDeletedMembers([]);
      }
    }
    if (watchesHolds) {
      // The human desk: an audit node's own holds — and for a Supernode,
      // every member node's holds (managing many nodes is the point).
      const watched = new Set(
        [node.node_id, ...members.map((m) => m.node_id)].map(
          (id) => `audit-node:${id}`,
        ),
      );
      try {
        const all = (await api.holds()).items;
        setHolds(all.filter((h) => h.reserved.some((r) => watched.has(r))));
      } catch {
        setHolds([]);
      }
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [node.node_id, watchesHolds, members.map((m) => m.node_id).join(",")]);

  useEffect(() => {
    void refresh();
    const t = setInterval(refresh, 6000);
    return () => clearInterval(t);
  }, [refresh]);

  // A lesson that was already recording re-opens its panel — leaving
  // the window never loses a demonstration in progress.
  useEffect(() => {
    let cancelled = false;
    api
      .imitateStatus(node.node_id)
      .then((r) => {
        if (cancelled) return;
        setLesson(r.lesson);
        if (r.lesson) setImitateOpen(true);
      })
      .catch(() => {
        if (!cancelled) setLesson(null);
      });
    return () => {
      cancelled = true;
    };
  }, [node.node_id]);

  return (
    <div className="noder-thread">
      <div className="noder-head">
        <div>
          <div className="run-card-intent">
            {account.is_supernode ? "◆ " : ""}
            {displayNodeName(node.title)}
          </div>
          <div className="muted">
            <NodeIdChip value={node.node_id} /> ·{" "}
            {account.responsible
              ? `${tr("work.responsible")} ${account.responsible}`
              : tr("work.notOnboarded")}
            {account.admin ? ` · ${tr("work.admin")} ${account.admin}` : ""}
          </div>
        </div>
        <span className={`phase phase-${account.status}`}>
          {account.status.replace("_", " ")}
        </span>
      </div>

      {!account.responsible && (
        <p className="muted fixed-note">{tr("work.unclaimedNote")}</p>
      )}

      {/* The regime, fixed at creation — each trait its own tag — and
          the owner's list margins: pin, mute, delete-from-list. */}
      <div className="account-row regime">
        <RegimeTags account={account} />
        <NodeMargins node={node} onChanged={onChanged} onClosed={onClosed} />
        {account.supernode_id && (
          <span className="muted">
            {tr("work.under")}{" "}
            {/* The org's NAME, for the onboarder exactly as for the
                owner — the server resolves it even when the Supernode
                is not on this desk; the id is the last resort. */}
            {node.supernode_title ||
              (allNodes.find((n) => n.node_id === account.supernode_id)
                ?.title ??
                account.supernode_id.slice(0, 8))}
          </span>
        )}
      </div>

      {tab !== "interact" && watchesHolds && holds.length > 0 && (
        <div className="commits">
          <div className="convo-group">{tr("work.pending")}</div>
          {holds.map((h) => (
            <HoldDesk key={h.pending_id} hold={h} onDecided={refresh} />
          ))}
        </div>
      )}

      <div className="dev-nav">
        <button
          className={tab === "activity" ? "on" : ""}
          onClick={() => setTab("activity")}
        >
          {tr("work.tabActivity")}
        </button>
        <button
          className={tab === "interact" ? "on" : ""}
          onClick={() => setTab("interact")}
        >
          {tr("work.tabInteract")}
        </button>
        <button
          className={tab === "files" ? "on" : ""}
          onClick={() => setTab("files")}
        >
          {tr("files")}
        </button>
        <button
          className={tab === "code" ? "on" : ""}
          onClick={() => setTab("code")}
        >
          {tr("work.tabCode")}
        </button>
        <button
          className={tab === "access" ? "on" : ""}
          onClick={() => setTab("access")}
        >
          {tr("work.tabAccess")}
        </button>
        {/* Imitate rides the tab row's right edge: the guided lesson —
            name the goal, describe each step, run the real work through
            this node — that builds a capable node from the demonstration. */}
        <button
          className={`imitate${lesson ? " rec" : ""}`}
          title={tr("work.imitateHint")}
          onClick={() => setImitateOpen((o) => !o)}
        >
          {lesson
            ? `● ${tr("work.imitateRecording")}`
            : `◉ ${tr("work.imitate")}`}
        </button>
      </div>

      {imitateOpen && (
        <ImitatePanel node={node} lesson={lesson} onLesson={setLesson} />
      )}

      {/* The Access tab: everything about WHO and WHAT may reach this
          node — org verification, the template, the member roster (and
          minting new members), the block lists, the egress grant — in
          one place, so none of it ever crowds the activity log. */}
      {tab === "access" && (
        <div className="access-pane">
          {account.is_supernode && <KycSection nodeId={node.node_id} />}
          {account.is_supernode && account.responsible && (
            <OrgTemplateSection nodeId={node.node_id} onImported={refresh} />
          )}
          {account.is_supernode && (
            <div className="commits">
              <button
                className="convo-group toggle"
                aria-expanded={showMembers}
                onClick={() => setShowMembers((s) => !s)}
              >
                {showMembers ? "▾" : "▸"} {tr("work.memberNodes")} (
                {members.length})
              </button>
              {showMembers && (
                <p className="muted fixed-note">{tr("work.orderHint")}</p>
              )}
              {showMembers &&
                members.map((m) => (
                  <div key={m.node_id} className="commit-row">
                    <span>
                      {/* The name IS the door: clicking a member opens
                          that node's own card. */}
                      <button
                        type="button"
                        className="linklike member-link"
                        onClick={() => onOpenNode(m.node_id)}
                      >
                        {displayNodeName(m.title)}
                      </button>{" "}
                      · {m.account.responsible || tr("work.keepIdPrivate")}
                    </span>
                    {/* The one seat block: theme-colored when a human
                        answers (onboard), blue when the seat runs on
                        demand — and the blue block is the org's
                        staffing hand: click to assign a user. */}
                    <RegimeTags account={m.account} />
                    <SeatBlock
                      member={m}
                      canAssign={account.responsible !== ""}
                      onAssigned={onChanged}
                    />
                  </div>
                ))}
              {/* The administrator's undo: deleted members stay OFF the
                  roster above but revivable here for 7 days — then the
                  purge makes the delete final. */}
              {showMembers && deletedMembers.length > 0 && (
                <>
                  <span className="convo-group deleted-group">
                    {tr("work.recentlyDeleted")} ({deletedMembers.length})
                  </span>
                  <p className="muted fixed-note">{tr("work.reviveHint")}</p>
                  {deletedMembers.map((m) => (
                    <div key={m.node_id} className="commit-row deleted-row">
                      <span className="muted">
                        {displayNodeName(m.title)} ·{" "}
                        {m.deleted_at.slice(0, 10)}
                      </span>
                      <button
                        type="button"
                        className="linklike"
                        onClick={() => {
                          void (async () => {
                            try {
                              await api.reviveWorkNode(m.node_id);
                              onChanged();
                            } catch {
                              /* the poll corrects the list shortly */
                            }
                          })();
                        }}
                      >
                        {tr("work.revive")}
                      </button>
                    </div>
                  ))}
                </>
              )}
              {/* Minting a member happens HERE, on the org's own access
                  desk — the + in the sidebar makes standalone nodes. */}
              {account.responsible && (
                <form
                  className="setting-control row member-create"
                  onSubmit={(e) => {
                    e.preventDefault();
                    const title = memberTitle.trim();
                    if (!title || memberBusy) return;
                    setMemberError("");
                    setMemberBusy(true);
                    void (async () => {
                      try {
                        const id = (await api.createNode(title, title))
                          .node_id;
                        await api.workAccountCreate(id, {
                          is_supernode: memberSuper,
                          supernode_id: node.node_id,
                          audit_mode: false,
                          allow_autodev_data: true,
                          authority_level: memberAuthority,
                          accept_policy: true,
                        });
                        setMemberTitle("");
                        onChanged();
                        void refresh();
                      } catch (e) {
                        setMemberError((e as Error).message);
                      } finally {
                        setMemberBusy(false);
                      }
                    })();
                  }}
                >
                  <input
                    aria-label={tr("work.newMemberName")}
                    placeholder={tr("work.newMemberName")}
                    value={memberTitle}
                    onChange={(e) => setMemberTitle(e.target.value)}
                  />
                  <label className="checkline">
                    <input
                      type="checkbox"
                      aria-label={tr("work.memberSupernode")}
                      checked={memberSuper}
                      onChange={(e) => setMemberSuper(e.target.checked)}
                    />
                    ◆
                  </label>
                  <select
                    aria-label={tr("work.authority")}
                    value={memberAuthority}
                    onChange={(e) =>
                      setMemberAuthority(Number(e.target.value))
                    }
                  >
                    {[1, 2, 3, 4, 5].map((level) => (
                      <option key={level} value={level}>
                        L{level}
                      </option>
                    ))}
                  </select>
                  <button
                    type="submit"
                    disabled={memberBusy || !memberTitle.trim()}
                  >
                    {tr("work.createMember")}
                  </button>
                </form>
              )}
              {memberError && <div className="error">{memberError}</div>}
            </div>
          )}
          {/* Egress consent lives with the rest of the access desk: what
              this node may reach on the web. A Supernode edits its block
              lists instead — its choice is what to REFUSE (hosts) and
              whom not to hear (users). */}
          {account.responsible &&
            (account.is_supernode ? (
              <SupernodeBlocks nodeId={node.node_id} account={account} />
            ) : (
              <NetworkGrant nodeId={node.node_id} account={account} />
            ))}
        </div>
      )}

      {tab === "code" && <CodeView node={node} />}
      {tab === "interact" && <NodeInteract node={node} />}
      {tab === "files" && <FilesPane nodeId={node.node_id} />}

      {tab === "activity" && (
        <div className="noder-log">
          <div className="muted">{reliabilityLine(node)}</div>
          {activity === null && (
            <div className="muted">{tr("work.loadingActivity")}</div>
          )}
          {activity !== null && activity.length === 0 && (
            <div className="muted">{tr("work.noExecutions")}</div>
          )}
          {activity?.map((run) => (
            <div key={run.run_id} className="feed-run">
              <div className="feed-run-head">
                {/* Who executed it, by NAME — the raw run id stays in the
                    tooltip and the legal log files. */}
                <span className="log-label" title={`run ${run.run_id}`}>
                  {run.node_title ?? `run ${run.run_id.slice(0, 8)}`}
                </span>
                <span className="muted">
                  {money(Math.round(run.gross * 1e6))}
                </span>
              </div>
              {run.steps.map((s) => (
                <div
                  key={s.seq}
                  className="log-line"
                  title={`${s.at} · ${s.event_type} · run ${run.run_id}`}
                >
                  <span className="log-at">{clock(s.at)}</span>
                  <span className="log-label">
                    {humanizeEvent(s.event_type)}
                  </span>
                </div>
              ))}
            </div>
          ))}
        </div>
      )}

      <p className="muted noder-hint">
        {account.responsible
          ? tr("work.yoursToAnswer")
          : tr("work.nooneAnswers")}
      </p>
    </div>
  );
}

// ---- the node's egress consent: exact hosts, given and withdrawable -------
// The Code tab: what is actually BUILT in this node, read like a repo —
// the description up top (the README's first line), then the drawer's
// files with language badges, each opening read-only. Any mainstream
// language may live here: Python runs natively; JavaScript, C, C++ and
// shell entries run through the sandbox's polyglot wrapper; JSON, HTML,
// Markdown and React sources ride as staged assets.
const LANGUAGE_BADGES: Record<string, string> = {
  py: "Python",
  js: "JavaScript",
  mjs: "JavaScript",
  jsx: "React",
  tsx: "React",
  ts: "TypeScript",
  c: "C",
  cpp: "C++",
  cc: "C++",
  h: "C/C++",
  sh: "Shell",
  json: "JSON",
  html: "HTML",
  htm: "HTML",
  css: "CSS",
  md: "Markdown",
};

export function languageOf(name: string): string {
  const ext = name.includes(".") ? name.split(".").pop()!.toLowerCase() : "";
  return LANGUAGE_BADGES[ext] ?? (ext ? ext.toUpperCase() : "file");
}

export function CodeView({ node }: { node: WorkNode }) {
  const tr = useT();
  const [files, setFiles] = useState<FileMeta[] | null>(null);
  const [openFile, setOpenFile] = useState<FileDoc | null>(null);
  const [error, setError] = useState("");

  useEffect(() => {
    let cancelled = false;
    api
      .files(node.node_id)
      .then((r) => {
        if (cancelled) return;
        const sorted = [...r.items].sort((a, b) =>
          `${a.folder ?? ""}/${a.name}`.localeCompare(
            `${b.folder ?? ""}/${b.name}`,
          ),
        );
        setFiles(sorted);
      })
      .catch(() => {
        if (!cancelled) setFiles([]);
      });
    return () => {
      cancelled = true;
    };
  }, [node.node_id]);

  async function view(meta: FileMeta) {
    setError("");
    try {
      setOpenFile(await api.file(meta.file_id));
    } catch (e) {
      setError((e as Error).message);
    }
  }

  if (openFile) {
    return (
      <div className="code-view">
        <div className="code-file-head">
          <button
            type="button"
            className="linklike"
            onClick={() => setOpenFile(null)}
          >
            ‹ {tr("work.codeAllFiles")}
          </button>
          <span className="code-path">
            {openFile.folder ? `${openFile.folder}/` : ""}
            {openFile.name}
          </span>
          <span className="badge">{languageOf(openFile.name)}</span>
        </div>
        <pre className="code-content">{openFile.content}</pre>
      </div>
    );
  }
  return (
    <div className="code-view">
      {/* The README head: the name and what it was built to do. */}
      <div className="code-readme">
        <div className="run-card-intent">{displayNodeName(node.title)}</div>
        <p className="muted">{node.summary || tr("work.codeNoSummary")}</p>
      </div>
      {files === null && <div className="muted">…</div>}
      {files !== null && files.length === 0 && (
        <p className="muted">{tr("work.codeEmpty")}</p>
      )}
      {files !== null && files.length > 0 && (
        <div className="code-list">
          {files.map((f) => (
            <button
              key={f.file_id}
              type="button"
              className="commit-row code-row"
              onClick={() => void view(f)}
            >
              <span className="code-path">
                {f.folder ? `${f.folder}/` : ""}
                {f.name}
              </span>
              <span className="muted">
                {languageOf(f.name)} · {f.size.toLocaleString()} B
              </span>
            </button>
          ))}
        </div>
      )}
      <p className="muted fixed-note">{tr("work.codeLanguagesNote")}</p>
      {error && <div className="error">{error}</div>}
    </div>
  );
}

// The seat block: the one mark a member row wears. Theme-colored when a
// human answers for the seat ("onboard"); blue when it runs on demand —
// and the blue block is also the Supernode's staffing hand: clicking it
// assigns a user to the seat.
export function SeatBlock({
  member,
  canAssign,
  onAssigned,
}: {
  member: WorkNode;
  canAssign: boolean;
  onAssigned: () => void;
}) {
  const tr = useT();
  const [asking, setAsking] = useState(false);
  const [who, setWho] = useState("");
  const [error, setError] = useState("");
  if (member.account.responsible) {
    return <span className="seat-chip onboard">{tr("work.seatOnboard")}</span>;
  }
  if (!asking || !canAssign) {
    return (
      <button
        type="button"
        className="seat-chip on-demand"
        title={tr("work.assignHint")}
        onClick={() => canAssign && setAsking(true)}
      >
        {tr("work.seatOnDemand")}
      </button>
    );
  }
  return (
    <form
      className="seat-assign"
      onSubmit={(e) => {
        e.preventDefault();
        const name = who.trim();
        if (!name) return;
        setError("");
        void api
          .assignNode(member.node_id, name)
          .then(() => {
            setAsking(false);
            onAssigned();
          })
          .catch((err) => setError((err as Error).message));
      }}
    >
      <input
        aria-label={`${tr("work.assignUser")} ${displayNodeName(member.title)}`}
        placeholder={tr("work.assignUser")}
        value={who}
        autoFocus
        onChange={(e) => setWho(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Escape") setAsking(false);
        }}
      />
      <button type="submit">{tr("work.assign")}</button>
      {error && <span className="error">{error}</span>}
    </form>
  );
}

function NetworkGrant({
  nodeId,
  account,
}: {
  nodeId: string;
  account: NodeAccountView;
}) {
  const tr = useT();
  // The grant answers to the server; local state tracks the last account
  // the server returned, so a save updates the list without a full reload.
  const [hosts, setHosts] = useState<string[]>(account.network_hosts ?? []);
  const [draft, setDraft] = useState("");
  const [error, setError] = useState("");

  async function save(next: string[]) {
    setError("");
    try {
      const saved = await api.workAccount(nodeId, { network_hosts: next });
      setHosts(saved.network_hosts ?? []);
      setDraft("");
    } catch (e) {
      setError((e as Error).message);
    }
  }

  return (
    <div className="commits network-grant">
      <div className="convo-group">{tr("net.header")}</div>
      {/* On the global service a signed-in account needs no grants: the
          web stands open by default; a grant list, when set, NARROWS it. */}
      {isRemote() && (
        <div className="muted">{tr("net.globalOpenNote")}</div>
      )}
      {hosts.length === 0 && !isRemote() && (
        <div className="muted">{tr("net.none")}</div>
      )}
      {hosts.map((h) => (
        <div key={h} className="commit-row">
          <span>{h}</span>
          <button onClick={() => void save(hosts.filter((x) => x !== h))}>
            {tr("net.withdraw")}
          </button>
        </div>
      ))}
      <form
        className="grant-row"
        onSubmit={(e) => {
          e.preventDefault();
          const host = draft.trim().toLowerCase();
          if (host && !hosts.includes(host)) void save([...hosts, host]);
        }}
      >
        <input
          aria-label={tr("net.hostLabel")}
          placeholder="api.example.com"
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
        />
        <button type="submit" disabled={!draft.trim()}>
          {tr("net.grant")}
        </button>
      </form>
      {error ? <div className="error">{error}</div> : null}
    </div>
  );
}

// ---- the Supernode's block lists: hosts refused, users unheard -------------
// A verified Supernode under the global account has its web OPEN — no host
// grant limits it. What remains is choice: which hosts the org refuses
// (binding every node down the chain) and which users it will not hear
// from, exactly like a user blocking a user.
function SupernodeBlocks({
  nodeId,
  account,
}: {
  nodeId: string;
  account: NodeAccountView;
}) {
  const tr = useT();
  const [hosts, setHosts] = useState<string[]>(account.blocked_hosts ?? []);
  const [users, setUsers] = useState<string[]>(account.blocked_users ?? []);
  const [hostDraft, setHostDraft] = useState("");
  const [userDraft, setUserDraft] = useState("");
  const [error, setError] = useState("");
  // Whether this org's web stands open (a VERIFIED entity under the
  // global account) — decides the explanatory line, not the editors.
  const [openWeb, setOpenWeb] = useState(false);
  useEffect(() => {
    api
      .kycStatus(nodeId)
      .then((k) => setOpenWeb(k.application?.status === "verified"))
      .catch(() => setOpenWeb(false));
  }, [nodeId]);

  async function save(patch: {
    blocked_hosts?: string[];
    blocked_users?: string[];
  }) {
    setError("");
    try {
      const saved = await api.workAccount(nodeId, patch);
      setHosts(saved.blocked_hosts ?? []);
      setUsers(saved.blocked_users ?? []);
      setHostDraft("");
      setUserDraft("");
    } catch (e) {
      setError((e as Error).message);
    }
  }

  return (
    <div className="commits network-grant supernode-blocks">
      {/* Until the org is verified under the global account its web is
          still the grant regime — the allow list stays editable. Once
          verified, the open-web line replaces it. */}
      {openWeb ? (
        <p className="muted">{tr("net.openWeb")}</p>
      ) : (
        <NetworkGrant nodeId={nodeId} account={account} />
      )}
      <div className="convo-group">{tr("net.blockedHosts")}</div>
      {hosts.length === 0 && (
        <div className="muted">{tr("net.noBlockedHosts")}</div>
      )}
      {hosts.map((h) => (
        <div key={h} className="commit-row">
          <span>{h}</span>
          <button
            onClick={() =>
              void save({ blocked_hosts: hosts.filter((x) => x !== h) })
            }
          >
            {tr("net.unblock")}
          </button>
        </div>
      ))}
      <form
        className="grant-row"
        onSubmit={(e) => {
          e.preventDefault();
          const host = hostDraft.trim().toLowerCase();
          if (host && !hosts.includes(host))
            void save({ blocked_hosts: [...hosts, host] });
        }}
      >
        <input
          aria-label={tr("net.blockHostLabel")}
          placeholder="tracker.example.com"
          value={hostDraft}
          onChange={(e) => setHostDraft(e.target.value)}
        />
        <button type="submit" disabled={!hostDraft.trim()}>
          {tr("net.blockHost")}
        </button>
      </form>
      <div className="convo-group">{tr("net.blockedUsers")}</div>
      {users.length === 0 && (
        <div className="muted">{tr("net.noBlockedUsers")}</div>
      )}
      {users.map((u) => (
        <div key={u} className="commit-row">
          <span>{u}</span>
          <button
            onClick={() =>
              void save({ blocked_users: users.filter((x) => x !== u) })
            }
          >
            {tr("net.unblock")}
          </button>
        </div>
      ))}
      <form
        className="grant-row"
        onSubmit={(e) => {
          e.preventDefault();
          const user = userDraft.trim();
          if (user && !users.includes(user))
            void save({ blocked_users: [...users, user] });
        }}
      >
        <input
          aria-label={tr("net.blockUserLabel")}
          placeholder="username"
          value={userDraft}
          onChange={(e) => setUserDraft(e.target.value)}
        />
        <button type="submit" disabled={!userDraft.trim()}>
          {tr("net.blockUser")}
        </button>
      </form>
      {error ? <div className="error">{error}</div> : null}
    </div>
  );
}

// ---- the template button: a lean working structure, imported --------------
// Deterministic-first, like node execution: a recorded choice returns
// instantly, a keyword match is arithmetic, and the model is consulted
// only when evidence is thin — and then only to PICK from the catalog.
function OrgTemplateSection({
  nodeId,
  onImported,
}: {
  nodeId: string;
  onImported: () => void;
}) {
  const tr = useT();
  const [view, setView] = useState<OrgTemplateView | null>(null);
  const [busy, setBusy] = useState(false);
  const [imported, setImported] = useState<number | null>(null);
  const [error, setError] = useState("");

  const missing = view ? view.roles.filter((r) => !r.exists).length : 0;

  return (
    <div className="commits org-template">
      <div className="convo-group">{tr("tpl.header")}</div>
      {view === null && (
        <div className="commit-row">
          <span className="muted">{tr("tpl.hint")}</span>
          <button
            disabled={busy}
            onClick={() => {
              setBusy(true);
              setError("");
              api
                .orgTemplate(nodeId)
                .then(setView)
                .catch((e) => setError((e as Error).message))
                .finally(() => setBusy(false));
            }}
          >
            {tr("tpl.button")}
          </button>
        </div>
      )}
      {view !== null && (
        <>
          <div className="commit-row">
            <span>
              {view.name}
              {view.evidence.length > 0 && (
                <span className="muted"> · {view.evidence.join(", ")}</span>
              )}
            </span>
            <span className="muted">{tr(`tpl.source.${view.source}`)}</span>
          </div>
          <p className="muted">{view.purpose}</p>
          {/* Growth pressure: a seat whose function outgrew the branch
              threshold marks the structure due for a RE-REASON — the
              operator's button, never a silent re-plan. */}
          {view.needs_branch && (
            <div className="commit-row rebranch">
              <span className="muted">
                {tr("tpl.rebranchNote")}{" "}
                {(view.members ?? [])
                  .filter((m) => m.over)
                  .map((m) => displayNodeName(m.title))
                  .join(", ")}
              </span>
              <button
                disabled={busy}
                onClick={() => {
                  setBusy(true);
                  setError("");
                  api
                    .orgTemplateApply(nodeId, true)
                    .then((r) => {
                      setImported(r.created.length);
                      setView(null);
                      onImported();
                    })
                    .catch((e) => setError((e as Error).message))
                    .finally(() => setBusy(false));
                }}
              >
                {tr("tpl.rebranch")}
              </button>
            </div>
          )}
          {view.roles.map((r) => (
            <div key={r.name} className="commit-row tpl-role">
              <span>
                <strong>{r.name}</strong>
                {r.exists && (
                  <span className="muted"> · {tr("tpl.seated")}</span>
                )}
                <br />
                <span className="muted">{r.responsibility}</span>
              </span>
            </div>
          ))}
          {imported !== null ? (
            <p className="muted">{tf("tpl.imported", { n: imported })}</p>
          ) : missing === 0 ? (
            <p className="muted">{tr("tpl.allSeated")}</p>
          ) : (
            <button
              disabled={busy}
              onClick={() => {
                setBusy(true);
                setError("");
                api
                  .orgTemplateApply(nodeId)
                  .then((applied) => {
                    setImported(applied.created.length);
                    onImported();
                  })
                  .catch((e) => setError((e as Error).message))
                  .finally(() => setBusy(false));
              }}
            >
              {tf("tpl.import", { n: missing })}
            </button>
          )}
        </>
      )}
      {error ? <div className="error">{error}</div> : null}
    </div>
  );
}

// ---- one held request: allow, sign & allow, reply, or reject --------------
function HoldDesk({
  hold,
  onDecided,
}: {
  hold: HoldItem;
  onDecided: () => void;
}) {
  const tr = useT();
  const [signature, setSignature] = useState("");
  const [reply, setReply] = useState("");
  const [error, setError] = useState("");

  async function act(fn: () => Promise<unknown>) {
    setError("");
    try {
      await fn();
      onDecided();
    } catch (e) {
      setError((e as Error).message);
    }
  }

  return (
    <div className="hold-desk">
      <div className="commit-row">
        <span>
          {hold.name} · {tr("hold.from")}{" "}
          {hold.submitted_by ?? tr("hold.unknown")}
        </span>
        <span className="row">
          <button
            onClick={() => void act(() => api.decideHold(hold.pending_id, true))}
          >
            {tr("hold.allow")}
          </button>
          <button
            className="ghost"
            onClick={() =>
              void act(() => api.decideHold(hold.pending_id, false))
            }
          >
            {tr("hold.reject")}
          </button>
        </span>
      </div>
      <div className="row hold-tools">
        <input
          aria-label={`Sign for ${hold.name}`}
          placeholder={tr("hold.signPh")}
          value={signature}
          onChange={(e) => setSignature(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && signature.trim()) {
              e.preventDefault();
              void act(() =>
                api.decideHold(hold.pending_id, true, signature.trim()),
              );
            }
          }}
        />
        <button
          disabled={!signature.trim()}
          onClick={() =>
            void act(() =>
              api.decideHold(hold.pending_id, true, signature.trim()),
            )
          }
        >
          {tr("hold.sign")}
        </button>
      </div>
      <div className="row hold-tools">
        <input
          aria-label={`Reply to ${hold.name}`}
          placeholder={tr("hold.replyPh")}
          value={reply}
          onChange={(e) => setReply(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && reply.trim()) {
              e.preventDefault();
              void act(async () => {
                await api.holdReply(hold.pending_id, reply.trim());
                setReply("");
              });
            }
          }}
        />
        <button
          disabled={!reply.trim()}
          onClick={() =>
            void act(async () => {
              await api.holdReply(hold.pending_id, reply.trim());
              setReply("");
            })
          }
        >
          {tr("hold.sendReply")}
        </button>
      </div>
      {hold.replies?.length > 0 && (
        <div className="hold-replies">
          {hold.replies.map((r, i) => (
            <div key={i} className="muted">
              ↳ {r.author}: {r.message}
            </div>
          ))}
        </div>
      )}
      {error ? <div className="error">{error}</div> : null}
    </div>
  );
}

// ---- The platform reviewer's KYC inbox -------------------------------------
// Visible only to accounts the host granted kyc:review (everyone else gets a
// 403 and sees nothing): pending applications, fast-tracked first, with the
// approve/reject verdict right on the row. A verdict clears the row.
export function KycReviewInbox() {
  const tr = useT();
  const [items, setItems] = useState<KycApplication[] | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState("");

  const refresh = useCallback(async () => {
    try {
      setItems((await api.kycReviews()).items ?? []);
    } catch {
      setItems(null); // not a reviewer, or not a reviewing host: no inbox
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  if (items === null || items.length === 0) return null;

  async function decide(nodeId: string, approved: boolean) {
    setError("");
    setBusy(nodeId);
    try {
      await api.kycDecide(nodeId, approved);
      await refresh();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy("");
    }
  }

  return (
    <div className="commits kyc-inbox">
      <div className="convo-group">
        {tr("kyc.inbox")} ({items.length})
      </div>
      {items.map((app) => (
        <div key={app.node_id} className="commit-row">
          <span>
            {app.legal_name} · {app.company_email}
            {app.registration_no ? ` · reg ${app.registration_no}` : ""}
          </span>
          <span className="muted">
            {app.screen === "fast_track" ? tr("kyc.fastRow") : tr("kyc.queue")}
          </span>
          <button
            disabled={busy === app.node_id}
            onClick={() => void decide(app.node_id, true)}
          >
            {tr("kyc.approve")}
          </button>
          <button
            disabled={busy === app.node_id}
            onClick={() => void decide(app.node_id, false)}
          >
            {tr("hold.reject")}
          </button>
        </div>
      ))}
      {error && <div className="error">{error}</div>}
    </div>
  );
}

// ---- Supernode KYC: a verified legal entity earns global trust ------------
// Applying is the Supernode owner's move; the platform's reviewers decide.
// Personal mailboxes are refused before anything is stored, and the fee
// rides on the paying plan — both walls answer here in words.
export function KycSection({ nodeId }: { nodeId: string }) {
  const tr = useT();
  const [view, setView] = useState<KycView | null>(null);
  const [legalName, setLegalName] = useState("");
  const [email, setEmail] = useState("");
  const [regNo, setRegNo] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  const refresh = useCallback(async () => {
    try {
      setView(await api.kycStatus(nodeId));
    } catch {
      setView(null); // KYC not enabled on this host: say nothing at all
    }
  }, [nodeId]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  if (view === null) return null;
  const app = view.application;

  // The review is DONE: the block disappears — one quiet badge remains.
  if (app?.status === "verified") {
    return (
      <div className="account-row">
        <span className="badge">
          ✓ {tr("kyc.verifiedBadge")} ×{view.trust_multiplier}
        </span>
        <span className="muted">{app.legal_name}</span>
      </div>
    );
  }

  // KYC binds only on the Global service, where a Supernode serves the
  // whole ecosystem with a higher trust score. On an Edge install (this
  // device or a private network) there is nothing to comply with and
  // nothing to subscribe to — so nothing to show.
  if (view.required === false) return null;

  async function submit() {
    setError("");
    setBusy(true);
    try {
      await api.kycApply(nodeId, {
        legal_name: legalName.trim(),
        company_email: email.trim(),
        ...(regNo.trim() ? { registration_no: regNo.trim() } : {}),
      });
      await refresh();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="commits kyc">
      <div className="convo-group">{tr("kyc.header")}</div>

      {app?.status === "pending_review" && (
        <div className="commit-row">
          <span className="badge">{tr("kyc.underReview")}</span>
          <span className="muted">
            {app.screen === "fast_track"
              ? tr("kyc.fastLane")
              : tr("kyc.queue")}{" "}
            · {app.legal_name}
          </span>
        </div>
      )}

      {app?.status === "rejected" && (
        <p className="muted">
          {tr("kyc.rejectedLead")}
          {app.decision_note ? ` — ${app.decision_note}` : ""}
          {tr("kyc.rejectedTail")}
        </p>
      )}

      {(!app || app.status === "rejected") && (
        <>
          <p className="muted">{tr("kyc.pitch")}</p>
          <div className="setting-control row">
            <input
              aria-label="Legal entity name"
              placeholder={tr("kyc.legalNamePh")}
              value={legalName}
              autoComplete="off"
              onChange={(e) => setLegalName(e.target.value)}
            />
            <input
              aria-label="Company email"
              placeholder="you@company.example"
              value={email}
              autoComplete="off"
              onChange={(e) => setEmail(e.target.value)}
            />
            <input
              aria-label="Registration number"
              placeholder={tr("kyc.regNoPh")}
              value={regNo}
              autoComplete="off"
              onChange={(e) => setRegNo(e.target.value)}
            />
            <button
              disabled={busy || !legalName.trim() || !email.trim()}
              onClick={() => void submit()}
            >
              {tr("kyc.apply")}
            </button>
          </div>
          {error && <div className="error">{error}</div>}
        </>
      )}
    </div>
  );
}
