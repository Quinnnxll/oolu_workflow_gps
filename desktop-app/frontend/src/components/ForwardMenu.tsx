import { useState } from "react";
import {
  forwardMessage,
  forwardMessageToFile,
  forwardTargets,
} from "../forward";
import type { ForwardTarget } from "../forward";

// The ↪ on a chat bubble: pick a destination and the message travels —
// to the OoLu conversation, to a node's interact window, or into a new
// document in Files. Seamless on purpose: one tap, one pick, done.

export function ForwardMenu({ text, from }: { text: string; from: string }) {
  const [open, setOpen] = useState(false);
  const [targets, setTargets] = useState<ForwardTarget[] | null>(null);
  const [done, setDone] = useState("");

  async function openMenu() {
    setOpen(true);
    if (targets === null) setTargets(await forwardTargets());
  }

  async function pick(target: ForwardTarget | "file") {
    try {
      if (target === "file") {
        const name = await forwardMessageToFile(text, from);
        setDone(`saved to ${name}`);
      } else {
        forwardMessage(text, from, target);
        setDone(`forwarded to ${target.title}`);
      }
    } catch (e) {
      setDone(`couldn't forward (${(e as Error).message})`);
    }
    setOpen(false);
  }

  if (done) return <span className="forward-done">{done}</span>;

  return (
    <span className="forward">
      <button
        type="button"
        className="forward-btn"
        title="Forward this message"
        aria-label="Forward this message"
        onClick={() => void openMenu()}
      >
        ↪
      </button>
      {open && (
        <span className="forward-menu">
          {(targets ?? []).map((t) => (
            <button
              key={`${t.kind}:${t.id ?? ""}`}
              type="button"
              onClick={() => void pick(t)}
            >
              {t.title}
            </button>
          ))}
          <button type="button" onClick={() => void pick("file")}>
            New file in Files
          </button>
          <button
            type="button"
            className="ghost"
            onClick={() => setOpen(false)}
          >
            cancel
          </button>
        </span>
      )}
    </span>
  );
}
