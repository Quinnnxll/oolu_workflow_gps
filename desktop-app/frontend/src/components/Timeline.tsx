import { useEffect, useState } from "react";
import { api, timelineSocket } from "../api";
import type { TimelineEvent } from "../types";

export function Timeline({ runId, phase }: { runId: string; phase: string }) {
  const [events, setEvents] = useState<TimelineEvent[]>([]);

  useEffect(() => {
    let alive = true;
    let opened = false;
    let ws: WebSocket | null = null;

    // The loopback stream replays the whole append-only timeline from seq 0,
    // so it is the source of truth. Poll only if the socket never opens.
    try {
      ws = timelineSocket(runId, (e) => {
        if (alive) setEvents((prev) => [...prev, e]);
      });
      ws.onopen = () => {
        opened = true;
        if (alive) setEvents([]);
      };
    } catch {
      ws = null;
    }

    const fallback = setTimeout(() => {
      if (!opened) {
        void api
          .timeline(runId)
          .then((r) => alive && setEvents(r.items))
          .catch(() => {});
      }
    }, 600);

    return () => {
      alive = false;
      clearTimeout(fallback);
      ws?.close();
    };
  }, [runId, phase]);

  if (!events.length) return null;

  return (
    <ol className="timeline">
      {events.map((e, i) => (
        <li key={i}>
          <time>{new Date(e.at).toLocaleTimeString()}</time>
          <span className="label">{e.label}</span>
          {e.detail && <span className="detail">{e.detail}</span>}
        </li>
      ))}
    </ol>
  );
}
