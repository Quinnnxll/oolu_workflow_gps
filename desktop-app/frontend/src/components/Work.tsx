import { useCallback, useEffect, useState, type FormEvent } from "react";
import { api } from "../api";
import type {
  HoldItem,
  KycApplication,
  KycView,
  NodeAccountView,
  NodeRunSteps,
  WorkNode,
} from "../api";
import { identityHue } from "../avatar";
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
        {nodes.map((n) => (
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
              <span className="convo-name">{displayNodeName(n.title)}</span>
              <span className="convo-sub">
                {money(n.earnings_micros)} · {healthLabel(n)}
              </span>
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
        </div>
        {selected === "add" && (
          <AddNode
            supernodes={nodes.filter((n) => n.account.is_supernode)}
            onDone={(nodeId) => {
              void refresh();
              setSelected(nodeId);
            }}
          />
        )}
        {active && (
          <NodeThread key={active.node_id} node={active} allNodes={nodes} />
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

function healthLabel(n: WorkNode): string {
  if (n.health.score === null) return t("work.noRunsYet");
  return tf("work.healthy", { pct: Math.round(n.health.score * 100) });
}

// ---- create (regime fixed forever) / onboard (no choices) -----------------
export function AddNode({
  supernodes,
  onDone,
}: {
  supernodes: WorkNode[];
  onDone: (nodeId: string) => void;
}) {
  const tr = useT();
  const [mode, setMode] = useState<"create" | "onboard">("create");
  const [title, setTitle] = useState("");
  const [summary, setSummary] = useState("");
  const [nodeId, setNodeId] = useState("");
  const [isSupernode, setIsSupernode] = useState(false);
  const [under, setUnder] = useState("");
  const [authority, setAuthority] = useState(1);
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
        supernode_id: under || null,
        // A Supernode always audits — humans in full control.
        audit_mode: isSupernode ? true : audit,
        allow_autodev_data: autoGrow,
        authority_level: under ? authority : null,
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

          {supernodes.length > 0 && (
            <>
              <label htmlFor="node-under">{tr("work.underSupernode")}</label>
              <select
                id="node-under"
                value={under}
                onChange={(e) => setUnder(e.target.value)}
              >
                <option value="">{tr("work.noneStandalone")}</option>
                {supernodes.map((s) => (
                  <option key={s.node_id} value={s.node_id}>
                    {displayNodeName(s.title)}
                  </option>
                ))}
              </select>
            </>
          )}

          {under && (
            <label htmlFor="node-authority">
              {tr("work.authority")}
              <select
                id="node-authority"
                value={authority}
                onChange={(e) => setAuthority(Number(e.target.value))}
              >
                {[1, 2, 3, 4, 5].map((level) => (
                  <option key={level} value={level}>
                    L{level}
                  </option>
                ))}
              </select>
            </label>
          )}

          {under && !isSupernode && (
            <p className="muted fixed-note">{tr("work.claimNote")}</p>
          )}

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

// ---- one node's thread: fixed regime, feed, and the human desk ------------
export function NodeThread({
  node,
  allNodes,
}: {
  node: WorkNode;
  allNodes: WorkNode[];
}) {
  const tr = useT();
  const [activity, setActivity] = useState<NodeRunSteps[] | null>(null);
  const [holds, setHolds] = useState<HoldItem[]>([]);
  const [tab, setTab] = useState<"activity" | "interact" | "files">(
    "activity",
  );
  // A large fleet folds away for a clear view of the thread itself.
  const [showMembers, setShowMembers] = useState(true);
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

      {/* The regime, fixed at creation: one concise tag, never knobs. */}
      <div className="account-row regime">
        <span className="badge">{regimeTag(account)}</span>
        {account.supernode_id && (
          <span className="muted">
            {tr("work.under")}{" "}
            {allNodes.find((n) => n.node_id === account.supernode_id)?.title ??
              account.supernode_id.slice(0, 8)}
          </span>
        )}
      </div>

      {tab !== "interact" && account.is_supernode && (
        <KycSection nodeId={node.node_id} />
      )}

      {/* The fleet roster lives on the Activity tab only — the Files tab
          already answered "which nodes" there; repeating the list is
          noise, not orientation. */}
      {tab === "activity" && account.is_supernode && members.length > 0 && (
        <div className="commits">
          <button
            className="convo-group toggle"
            aria-expanded={showMembers}
            onClick={() => setShowMembers((s) => !s)}
          >
            {showMembers ? "▾" : "▸"} {tr("work.memberNodes")} (
            {members.length})
          </button>
          {showMembers &&
            members.map((m) => (
              <div key={m.node_id} className="commit-row">
                <span>
                  {displayNodeName(m.title)} ·{" "}
                  {m.account.responsible || tr("work.keepIdPrivate")}
                </span>
                {/* Fixed at creation, authority included: display only. */}
                <span className="muted">{regimeTag(m.account)}</span>
              </div>
            ))}
        </div>
      )}

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
      </div>

      {/* Egress consent lives with the rest of the human desk: what this
          node may reach on the web, granted and withdrawable here. */}
      {tab === "activity" && account.responsible && (
        <NetworkGrant nodeId={node.node_id} account={account} />
      )}

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
      {hosts.length === 0 && <div className="muted">{tr("net.none")}</div>}
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
