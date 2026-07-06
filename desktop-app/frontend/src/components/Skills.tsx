import { useEffect, useState } from "react";
import { api } from "../api";
import type { Listing } from "../types";

export function Skills() {
  const [q, setQ] = useState("");
  const [items, setItems] = useState<Listing[]>([]);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    let alive = true;
    setLoading(true);
    const h = setTimeout(() => {
      void api
        .skills(q.trim() || undefined)
        .then((r) => alive && setItems(r.items))
        .catch(() => alive && setItems([]))
        .finally(() => alive && setLoading(false));
    }, 200);
    return () => {
      alive = false;
      clearTimeout(h);
    };
  }, [q]);

  return (
    <section className="skills">
      <div className="pane-head">
        <h2>Skills</h2>
        <input
          placeholder="Search published nodes…"
          value={q}
          onChange={(e) => setQ(e.target.value)}
        />
      </div>
      {loading && !items.length && <p className="empty">Loading…</p>}
      {!loading && !items.length && (
        <p className="empty">No published nodes match.</p>
      )}
      <ul>
        {items.map((s) => (
          <li key={s.listing_id}>
            <div className="skill-head">
              <span className="name">{s.title}</span>
              <span className={`kind kind-${s.status}`}>{s.status}</span>
            </div>
            <div className="summary">{s.summary}</div>
            <div className="tags">
              {s.tags.map((t) => (
                <span key={t} className="tag">
                  {t}
                </span>
              ))}
            </div>
          </li>
        ))}
      </ul>
    </section>
  );
}
