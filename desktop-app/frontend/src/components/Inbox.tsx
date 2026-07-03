import type { InboxItem } from "../types";

interface Props {
  items: InboxItem[];
  onOpen: (runId: string) => void;
  onRefresh: () => void;
}

export function Inbox({ items, onOpen, onRefresh }: Props) {
  return (
    <section className="inbox">
      <div className="pane-head">
        <h2>Inbox</h2>
        <button className="ghost" onClick={onRefresh}>
          Refresh
        </button>
      </div>
      {!items.length && <p className="empty">Nothing waiting on you.</p>}
      <ul>
        {items.map((it) => (
          <li key={`${it.run_id}-${it.kind}`} onClick={() => onOpen(it.run_id)}>
            <span className={`kind kind-${it.kind}`}>{it.kind}</span>
            <div className="body">
              <div className="intent">{it.intent}</div>
              <div className="prompt">{it.prompt}</div>
            </div>
            <time>{new Date(it.created_at).toLocaleString()}</time>
          </li>
        ))}
      </ul>
    </section>
  );
}
