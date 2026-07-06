import { useCallback, useEffect, useState, type FormEvent } from "react";
import { api } from "../api";
import type { HoldItem, NodeRunSteps, WorkNode } from "../api";
import { humanizeEvent } from "../humanize";
import { FilesPane } from "./FilesPane";

// The Work environment: where a noder manages and observes their nodes.
// Same messenger architecture as Life — the list is node accounts (name,
// cumulative earnings, health), the thread is the node's execution feed —
// plus the top-right "+" to create a node as admin or onboard an existing
// one as its responsible.

const AUTOBUILD_KEY = "oolu_autobuild";

export function Work({ onLife }: { onLife: () => void }) {
  const [nodes, setNodes] = useState<WorkNode[]>([]);
  const [selected, setSelected] = useState<string | "add" | null>(null);
  const [autobuild, setAutobuild] = useState(
    localStorage.getItem(AUTOBUILD_KEY) !== "off",
  );

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
            <span className="convo-avatar node">
              {n.title.slice(0, 1).toUpperCase()}
            </span>
            <span className="convo-body">
              <span className="convo-name">{n.title}</span>
              <span className="convo-sub">
                {money(n.earnings_micros)} · {healthLabel(n)}
              </span>
            </span>
          </button>
        ))}

        <label className="autobuild">
          <input
            type="checkbox"
            checked={autobuild}
            onChange={(e) => {
              setAutobuild(e.target.checked);
              localStorage.setItem(AUTOBUILD_KEY, e.target.checked ? "on" : "off");
            }}
          />
          Let OoLu auto-build missing nodes on my paths and publish them under
          my account
        </label>
      </aside>

      <section className="convo-pane">
        {selected === "add" && (
          <AddNode
            onDone={(nodeId) => {
              void refresh();
              setSelected(nodeId);
            }}
          />
        )}
        {active && (
          <NodeThread key={active.node_id} node={active} onChanged={refresh} />
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

// ---- create as admin / onboard as responsible -----------------------------
export function AddNode({ onDone }: { onDone: (nodeId: string) => void }) {
  const [mode, setMode] = useState<"create" | "onboard">("create");
  const [title, setTitle] = useState("");
  const [summary, setSummary] = useState("");
  const [nodeId, setNodeId] = useState("");
  const [audit, setAudit] = useState(false);
  const [shareData, setShareData] = useState(true);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  async function submit(event: FormEvent) {
    event.preventDefault();
    setError("");
    setBusy(true);
    try {
      let id = nodeId.trim();
      if (mode === "create") {
        id = (await api.createNode(title.trim(), summary.trim())).node_id;
      }
      await api.workAccount(id, {
        audit_mode: audit,
        allow_autodev_data: shareData,
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
          <p className="muted">
            You become the node's admin; it stays "needs verification" until
            its first verified runs.
          </p>
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
        </>
      ) : (
        <>
          <p className="muted">
            Take responsibility for a node that already exists on this host.
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

      <label className="checkline">
        <input
          type="checkbox"
          checked={audit}
          onChange={(e) => setAudit(e.target.checked)}
        />
        Audit node — every request must be committed manually
      </label>
      <label className="checkline">
        <input
          type="checkbox"
          checked={shareData}
          onChange={(e) => setShareData(e.target.checked)}
        />
        Allow data passing this node to be used for auto-development
      </label>

      <button type="submit" disabled={busy}>
        {busy ? "Working…" : mode === "create" ? "Create node" : "Onboard"}
      </button>
      {error ? <div className="error">{error}</div> : null}
    </form>
  );
}

// ---- one node's thread: account, steps, manual commits --------------------
export function NodeThread({
  node,
  onChanged,
}: {
  node: WorkNode;
  onChanged: () => void;
}) {
  const [activity, setActivity] = useState<NodeRunSteps[] | null>(null);
  const [holds, setHolds] = useState<HoldItem[]>([]);
  const [tab, setTab] = useState<"activity" | "files">("activity");
  const account = node.account;

  const refresh = useCallback(async () => {
    try {
      setActivity((await api.workActivity(node.node_id)).items);
    } catch {
      setActivity([]);
    }
    if (account.audit_mode) {
      try {
        const all = (await api.holds()).items;
        setHolds(
          all.filter((h) =>
            h.reserved.some((r) => r === `audit-node:${node.node_id}`),
          ),
        );
      } catch {
        setHolds([]);
      }
    }
  }, [node.node_id, account.audit_mode]);

  useEffect(() => {
    void refresh();
    const t = setInterval(refresh, 6000);
    return () => clearInterval(t);
  }, [refresh]);

  async function patch(change: Parameters<typeof api.workAccount>[1]) {
    await api.workAccount(node.node_id, change);
    onChanged();
  }

  return (
    <div className="noder-thread">
      <div className="noder-head">
        <div>
          <div className="run-card-intent">{node.title}</div>
          <div className="muted">
            {node.node_id} · responsible {account.responsible}
            {account.admin ? ` · admin ${account.admin}` : ""}
          </div>
        </div>
        <span className={`phase phase-${account.status}`}>
          {account.status.replace("_", " ")}
        </span>
      </div>

      <div className="account-row">
        <label>
          Authority
          <select
            value={account.authority_level}
            onChange={(e) => void patch({ authority_level: Number(e.target.value) })}
          >
            {[1, 2, 3, 4, 5].map((level) => (
              <option key={level} value={level}>
                L{level}
              </option>
            ))}
          </select>
        </label>
        <label className="checkline">
          <input
            type="checkbox"
            checked={account.audit_mode}
            onChange={(e) => void patch({ audit_mode: e.target.checked })}
          />
          Audit node
        </label>
        <label className="checkline">
          <input
            type="checkbox"
            checked={account.allow_autodev_data}
            onChange={(e) => void patch({ allow_autodev_data: e.target.checked })}
          />
          Data may train auto-development
        </label>
      </div>

      {account.audit_mode && holds.length > 0 && (
        <div className="commits">
          <div className="convo-group">Pending commits</div>
          {holds.map((h) => (
            <div key={h.pending_id} className="commit-row">
              <span>
                {h.name} · from {h.submitted_by ?? "unknown"}
              </span>
              <span className="row">
                <button
                  onClick={async () => {
                    await api.decideHold(h.pending_id, true);
                    void refresh();
                  }}
                >
                  Commit
                </button>
                <button
                  className="ghost"
                  onClick={async () => {
                    await api.decideHold(h.pending_id, false);
                    void refresh();
                  }}
                >
                  Reject
                </button>
              </span>
            </div>
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
            No executions yet — runs appear here as the marketplace uses this
            node.
          </div>
        )}
        {activity?.map((run) => (
          <div key={run.run_id} className="feed-run">
            <div className="feed-run-head">
              <span className="log-label">run {run.run_id.slice(0, 8)}</span>
              <span className="muted">{money(Math.round(run.gross * 1e6))}</span>
            </div>
            {run.steps.map((s) => (
              <div key={s.seq} className="log-line" title={s.event_type}>
                <span className="log-at">{s.at}</span>
                <span className="log-label">{humanizeEvent(s.event_type)}</span>
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
