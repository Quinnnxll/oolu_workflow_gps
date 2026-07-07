import { useCallback, useEffect, useState, type FormEvent } from "react";
import { api } from "../api";
import type { HoldItem, NodeRunSteps, WorkNode } from "../api";
import { identityHue } from "../avatar";
import { humanizeEvent } from "../humanize";
import { FilesPane } from "./FilesPane";

// The Work environment: where a noder manages and observes their nodes.
// A node's REGIME — Supernode or not, under which Supernode, audit or
// not, auto-growing or not — is fixed when it is created and can never be
// changed afterwards; authority levels exist only under a Supernode. The
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
                {n.account.is_supernode ? "Supernode · " : ""}
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
          <NodeThread
            key={active.node_id}
            node={active}
            allNodes={nodes}
            onChanged={refresh}
          />
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
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  async function submit(event: FormEvent) {
    event.preventDefault();
    setError("");
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
        supernode_id: !isSupernode && under ? under : null,
        // A Supernode always audits — humans in full control.
        audit_mode: isSupernode ? true : audit,
        allow_autodev_data: autoGrow,
        authority_level: !isSupernode && under ? authority : null,
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

          {!isSupernode && supernodes.length > 0 && (
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

          {!isSupernode && under && (
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
            Auto-growing — data passing this node may feed auto-development
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
  onChanged,
}: {
  node: WorkNode;
  allNodes: WorkNode[];
  onChanged: () => void;
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

      {/* The regime, fixed at creation: badges, never knobs. */}
      <div className="account-row regime">
        {account.is_supernode && (
          <span className="badge">Supernode — humans in full control</span>
        )}
        {account.supernode_id && (
          <span className="badge">
            L{account.authority_level ?? "?"} under{" "}
            {allNodes.find((n) => n.node_id === account.supernode_id)?.title ??
              account.supernode_id.slice(0, 8)}
          </span>
        )}
        {!account.supernode_id && !account.is_supernode && (
          <span className="badge muted-badge">no authority (standalone)</span>
        )}
        <span className="badge">
          {account.audit_mode
            ? "Audit — every request commits manually"
            : "Unattended runs allowed"}
        </span>
        <span className="badge">
          {account.allow_autodev_data
            ? "Auto-growing: data may feed development"
            : "Data never feeds auto-development"}
        </span>
      </div>

      {account.is_supernode && members.length > 0 && (
        <div className="commits">
          <div className="convo-group">Member nodes</div>
          {members.map((m) => (
            <div key={m.node_id} className="commit-row">
              <span>
                {m.title} · {m.account.responsible}
              </span>
              <label>
                Authority
                <select
                  aria-label={`Authority for ${m.title}`}
                  value={m.account.authority_level ?? 1}
                  onChange={async (e) => {
                    await api.workAccount(m.node_id, {
                      authority_level: Number(e.target.value),
                    });
                    onChanged();
                  }}
                >
                  {[1, 2, 3, 4, 5].map((level) => (
                    <option key={level} value={level}>
                      L{level}
                    </option>
                  ))}
                </select>
              </label>
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
