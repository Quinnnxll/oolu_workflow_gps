import { useEffect, useState } from "react";
import { api } from "../api";
import type { SkillCard } from "../types";

export function Skills() {
  const [q, setQ] = useState("");
  const [items, setItems] = useState<SkillCard[]>([]);
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
          placeholder="Search skills…"
          value={q}
          onChange={(e) => setQ(e.target.value)}
        />
      </div>
      {loading && !items.length && <p className="empty">Loading…</p>}
      {!loading && !items.length && <p className="empty">No skills registered yet.</p>}
      <ul>
        {items.map((s) => (
          <li key={`${s.skill_id}-${s.semver}`}>
            <div className="skill-head">
              <span className="name">{s.name}</span>
              <span className="semver">v{s.semver}</span>
              {s.score !== undefined && (
                <span className="score">{s.score.toFixed(2)}</span>
              )}
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
