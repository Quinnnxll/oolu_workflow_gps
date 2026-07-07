import { useCallback, useEffect, useState, type FormEvent } from "react";
import { api } from "../api";
import type { HoldItem, KycView, NodeRunSteps, WorkNode } from "../api";
import { identityHue } from "../avatar";
import { humanizeEvent } from "../humanize";
import { FilesPane } from "./FilesPane";

// The Work environment: where a noder manages and observes their nodes.
// A node's REGIME — Supernode or not, under which Supernode, audit or
// not, auto-growing or not, and its authority level — is fixed when it is
// created and can never be changed afterwards, not even by the Supernode's
// humans; authority levels exist only under a Supernode. The
// list is node accounts (name, cumulative earnings, health); the thread is
// the node's execution feed plus, where humans are in control (audit nodes
// and Supernodes), the allow / sign / reply desk for held requests.

export function Work({ onLife }: { onLife: () => void }) {
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
          <button onClick={onLife}>Life</button>
          <button className="on">Work</button>
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
      const id = (await api.createNode(title.trim(), summary.trim())).node_id;
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
  const [tab, setTab] = useState<"activity" | "files">("activity");
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
            {node.node_id} · responsible {account.responsible}
            {account.admin ? ` · admin ${account.admin}` : ""}
          </div>
        </div>
        <span className={`phase phase-${account.status}`}>
          {account.status.replace("_", " ")}
        </span>
      </div>

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

      {account.is_supernode && <KycSection nodeId={node.node_id} />}

      {account.is_supernode && members.length > 0 && (
        <div className="commits">
          <div className="convo-group">Member nodes</div>
          {members.map((m) => (
            <div key={m.node_id} className="commit-row">
              <span>
                {m.title} · {m.account.responsible}
              </span>
              {/* Fixed at creation, authority included: display only. */}
              <span className="muted">{regimeTag(m.account)}</span>
            </div>
          ))}
        </div>
      )}

      {watchesHolds && holds.length > 0 && (
        <div className="commits">
          <div className="convo-group">Pending requests</div>
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
          className={tab === "files" ? "on" : ""}
          onClick={() => setTab("files")}
        >
          Files
        </button>
      </div>

      {tab === "files" && <FilesPane nodeId={node.node_id} />}

      {tab === "activity" && (
        <div className="noder-log">
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
                <span className="log-label">run {run.run_id.slice(0, 8)}</span>
                <span className="muted">
                  {money(Math.round(run.gross * 1e6))}
                </span>
              </div>
              {run.steps.map((s) => (
                <div key={s.seq} className="log-line" title={s.event_type}>
                  <span className="log-at">{s.at}</span>
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
        You are responsible for this node — every step above is yours to
        answer for.
      </p>
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

      {app?.status === "verified" && (
        <div className="commit-row">
          <span className="badge">
            ✓ KYC verified · global trust ×{view.trust_multiplier}
          </span>
          <span className="muted">
            {app.legal_name} · every node under this Supernode ranks with it
          </span>
        </div>
      )}

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
