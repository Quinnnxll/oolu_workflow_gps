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
import { useT } from "../ui";
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
    <div className="life">
      <aside className="convo-list">
        <div className="mode-tabs">
          <button onClick={onLife}>{tr("life")}</button>
          <button className="on">{tr("work")}</button>
        </div>

        <div className="work-head">
          <span className="convo-group">My nodes</span>
          <button
            className="add-node"
            title="Create a node or onboard an existing one"
            onClick={() => setSelected("add")}
          >
            +
          </button>
        </div>

        {nodes.length === 0 && (
          <div className="convo-empty">
            No nodes yet — press + to create or onboard one.
          </div>
        )}
        {nodes.map((n) => (
          <button
            key={n.node_id}
            className={`convo ${selected === n.node_id ? "on" : ""}`}
            onClick={() => setSelected(n.node_id)}
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
              <span className="convo-name">{n.title}</span>
              <span className="convo-sub">
                {money(n.earnings_micros)} · {healthLabel(n)}
              </span>
            </span>
          </button>
        ))}
      </aside>

      <section className="convo-pane">
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
            <p>Pick a node to see what it has been doing.</p>
            <p className="muted">Earnings and health update as runs verify.</p>
          </div>
        )}
      </section>
    </div>
  );
}

function money(micros: number): string {
  return `$${(micros / 1_000_000).toFixed(2)}`;
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
  if (account.is_supernode) parts.push("Supernode");
  if (account.authority_level != null) parts.push(`L${account.authority_level}`);
  if (account.audit_mode) parts.push("Audit");
  if (account.allow_autodev_data) parts.push("Auto-growing");
  return parts.length ? `(${parts.join(", ")})` : "(standalone)";
}

function healthLabel(n: WorkNode): string {
  if (n.health.score === null) return "no runs yet";
  return `${Math.round(n.health.score * 100)}% healthy`;
}

// ---- create (regime fixed forever) / onboard (no choices) -----------------
export function AddNode({
  supernodes,
  onDone,
}: {
  supernodes: WorkNode[];
  onDone: (nodeId: string) => void;
}) {
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
      setError("Please agree to the Node Policy first — it is what lets "
        + "the platform restrict or remove clone, fraud, and zombie nodes.");
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
    <form className="add-pane" onSubmit={submit}>
      <div className="mode-tabs">
        <button
          type="button"
          className={mode === "create" ? "on" : ""}
          onClick={() => setMode("create")}
        >
          Create a node
        </button>
        <button
          type="button"
          className={mode === "onboard" ? "on" : ""}
          onClick={() => setMode("onboard")}
        >
          Onboard existing
        </button>
      </div>

      {mode === "create" ? (
        <>
          <label htmlFor="node-title">Name</label>
          <input
            id="node-title"
            value={title}
            required
            onChange={(e) => setTitle(e.target.value)}
          />
          <label htmlFor="node-summary">What it does</label>
          <input
            id="node-summary"
            value={summary}
            onChange={(e) => setSummary(e.target.value)}
          />

          <label htmlFor="node-function">
            Function (optional — bring your own code)
          </label>
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
              Upload a .py function
            </button>
            {fnName && <span className="muted">{fnName}</span>}
          </div>
          <textarea
            id="node-function"
            className="node-function"
            placeholder={
              "Paste or upload a self-contained Python function. It must " +
              "call emit_result once with its output. It runs sandboxed — " +
              "no network, no host credentials — and is screened and " +
              "verified before it is ever stored."
            }
            value={fnScript}
            rows={6}
            onChange={(e) => setFnScript(e.target.value)}
          />

          <p className="muted fixed-note">
            The choices below are fixed at creation — they can never be
            changed later.
          </p>

          <label className="checkline">
            <input
              type="checkbox"
              checked={isSupernode}
              onChange={(e) => setIsSupernode(e.target.checked)}
            />
            Supernode — manages many nodes for a group, a corporation, or a
            government division, with humans in full control (always audits)
          </label>

          {supernodes.length > 0 && (
            <>
              <label htmlFor="node-under">Under Supernode</label>
              <select
                id="node-under"
                value={under}
                onChange={(e) => setUnder(e.target.value)}
              >
                <option value="">(none — standalone, no authority)</option>
                {supernodes.map((s) => (
                  <option key={s.node_id} value={s.node_id}>
                    {s.title}
                  </option>
                ))}
              </select>
            </>
          )}

          {under && (
            <label htmlFor="node-authority">
              Authority
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
            <p className="muted fixed-note">
              A node created under a Supernode starts with NO responsible
              account. Its node id is the claim ticket: give it only to the
              person who should onboard, and never post it publicly — the
              user account that onboards becomes the responsible shown on
              the node.
            </p>
          )}

          <label className="checkline">
            <input
              type="checkbox"
              checked={isSupernode ? true : audit}
              disabled={isSupernode}
              onChange={(e) => setAudit(e.target.checked)}
            />
            Audit node — every request must be committed manually
          </label>
          <label className="checkline">
            <input
              type="checkbox"
              checked={autoGrow}
              onChange={(e) => setAutoGrow(e.target.checked)}
            />
            Auto-growing — data passing this node may feed new development
          </label>

          <label className="checkline policy">
            <input
              type="checkbox"
              checked={policyOk}
              onChange={(e) => setPolicyOk(e.target.checked)}
            />
            I agree to the Node Policy — clone, fraud, and zombie nodes are
            detected and can be restricted or removed by the platform
          </label>
        </>
      ) : (
        <>
          <p className="muted">
            Take responsibility for a node that already exists. Audit,
            auto-growing, and any Supernode membership or authority were
            fixed when it was created — onboarding offers no choices.
            Onboarding names YOU: your user ID appears on the node as its
            responsible.
          </p>
          <label htmlFor="node-id">Node id</label>
          <input
            id="node-id"
            value={nodeId}
            required
            onChange={(e) => setNodeId(e.target.value)}
          />
        </>
      )}

      <button type="submit" disabled={busy}>
        {busy ? "Working…" : mode === "create" ? "Create node" : "Onboard"}
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
            {node.title}
          </div>
          <div className="muted">
            {node.node_id} ·{" "}
            {account.responsible
              ? `responsible ${account.responsible}`
              : "not onboarded yet"}
            {account.admin ? ` · admin ${account.admin}` : ""}
          </div>
        </div>
        <span className={`phase phase-${account.status}`}>
          {account.status.replace("_", " ")}
        </span>
      </div>

      {!account.responsible && (
        <p className="muted fixed-note">
          This node has no responsible account yet. Do not show its node id
          publicly — whoever onboards with it becomes the responsible. Share
          it only with the person meant to take responsibility; once they
          onboard, their user ID appears here.
        </p>
      )}

      {/* The regime, fixed at creation: one concise tag, never knobs. */}
      <div className="account-row regime">
        <span className="badge">{regimeTag(account)}</span>
        {account.supernode_id && (
          <span className="muted">
            under{" "}
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
            {showMembers ? "▾" : "▸"} Member nodes ({members.length})
          </button>
          {showMembers &&
            members.map((m) => (
              <div key={m.node_id} className="commit-row">
                <span>
                  {m.title} ·{" "}
                  {m.account.responsible ||
                    "not onboarded — keep its id private"}
                </span>
                {/* Fixed at creation, authority included: display only. */}
                <span className="muted">{regimeTag(m.account)}</span>
              </div>
            ))}
        </div>
      )}

      {tab !== "interact" && watchesHolds && holds.length > 0 && (
        <div className="commits">
          <div className="convo-group">Pending</div>
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
          Activity
        </button>
        <button
          className={tab === "interact" ? "on" : ""}
          onClick={() => setTab("interact")}
        >
          Interact
        </button>
        <button
          className={tab === "files" ? "on" : ""}
          onClick={() => setTab("files")}
        >
          Files
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
          {activity === null && <div className="muted">Loading activity…</div>}
          {activity !== null && activity.length === 0 && (
            <div className="muted">
              No executions yet — runs appear here as the marketplace uses
              this node.
            </div>
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
          ? "You are responsible for this node — every step above is yours " +
            "to answer for."
          : "No one answers for this node yet — it gets its responsible " +
            "when the right person onboards with the node id."}
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
      <div className="convo-group">Network access</div>
      {hosts.length === 0 && (
        <div className="muted">
          No hosts granted — this node cannot reach the web at all until you
          name the exact hosts it may fetch from.
        </div>
      )}
      {hosts.map((h) => (
        <div key={h} className="commit-row">
          <span>{h}</span>
          <button onClick={() => void save(hosts.filter((x) => x !== h))}>
            Withdraw
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
          aria-label="Host to grant"
          placeholder="api.example.com"
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
        />
        <button type="submit" disabled={!draft.trim()}>
          Grant host
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
          {hold.name} · from {hold.submitted_by ?? "unknown"}
        </span>
        <span className="row">
          <button
            onClick={() => void act(() => api.decideHold(hold.pending_id, true))}
          >
            Allow
          </button>
          <button
            className="ghost"
            onClick={() =>
              void act(() => api.decideHold(hold.pending_id, false))
            }
          >
            Reject
          </button>
        </span>
      </div>
      <div className="row hold-tools">
        <input
          aria-label={`Sign for ${hold.name}`}
          placeholder="type your name to sign"
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
          Sign & allow
        </button>
      </div>
      <div className="row hold-tools">
        <input
          aria-label={`Reply to ${hold.name}`}
          placeholder="type a reply to the requester"
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
          Send reply
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
        KYC reviews awaiting your verdict ({items.length})
      </div>
      {items.map((app) => (
        <div key={app.node_id} className="commit-row">
          <span>
            {app.legal_name} · {app.company_email}
            {app.registration_no ? ` · reg ${app.registration_no}` : ""}
          </span>
          <span className="muted">
            {app.screen === "fast_track"
              ? "fast lane — trusted domain"
              : "standard queue"}
          </span>
          <button
            disabled={busy === app.node_id}
            onClick={() => void decide(app.node_id, true)}
          >
            Approve
          </button>
          <button
            disabled={busy === app.node_id}
            onClick={() => void decide(app.node_id, false)}
          >
            Reject
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
          ✓ KYC verified · global trust ×{view.trust_multiplier}
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
      <div className="convo-group">KYC — legal entity</div>

      {app?.status === "pending_review" && (
        <div className="commit-row">
          <span className="badge">Under review</span>
          <span className="muted">
            {app.screen === "fast_track"
              ? "fast lane — trusted company domain"
              : "standard queue"}{" "}
            · {app.legal_name}
          </span>
        </div>
      )}

      {app?.status === "rejected" && (
        <p className="muted">
          The last application was rejected
          {app.decision_note ? ` — ${app.decision_note}` : ""}. You can apply
          again below.
        </p>
      )}

      {(!app || app.status === "rejected") && (
        <>
          <p className="muted">
            Obey the KYC policy to rank with global trust: verification
            rides on your paying plan, and a verified Supernode carries a
            trust multiplier for every node under it. Use a company
            mailbox — personal mailboxes are refused.
          </p>
          <div className="setting-control row">
            <input
              aria-label="Legal entity name"
              placeholder="legal entity name"
              value={legalName}
              onChange={(e) => setLegalName(e.target.value)}
            />
            <input
              aria-label="Company email"
              placeholder="you@company.example"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
            />
            <input
              aria-label="Registration number"
              placeholder="registration no. (optional)"
              value={regNo}
              onChange={(e) => setRegNo(e.target.value)}
            />
            <button
              disabled={busy || !legalName.trim() || !email.trim()}
              onClick={() => void submit()}
            >
              Apply
            </button>
          </div>
          {error && <div className="error">{error}</div>}
        </>
      )}
    </div>
  );
}
