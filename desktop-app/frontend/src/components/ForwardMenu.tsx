import { useEffect, useRef, useState } from "react";
import {
  forwardMessage,
  forwardMessageToFile,
  forwardMessageToFriend,
  forwardTargets,
} from "../forward";
import type { ForwardTarget } from "../forward";
import { t } from "../ui";

// The ↪ on a chat bubble: pick a destination and the message travels —
// to the OoLu conversation, to a friend, to a node's interact window, or
// into a new document in Files. Seamless on purpose: one tap, one pick,
// done. The menu behaves like a menu: a click anywhere else (or Escape)
// closes it, and a search box narrows long friend/node lists to a match.

export function ForwardMenu({ text, from }: { text: string; from: string }) {
  const [open, setOpen] = useState(false);
  const [targets, setTargets] = useState<ForwardTarget[] | null>(null);
  const [query, setQuery] = useState("");
  const [done, setDone] = useState("");
  const rootRef = useRef<HTMLSpanElement | null>(null);

  async function openMenu() {
    setQuery("");
    setOpen(true);
    if (targets === null) setTargets(await forwardTargets());
  }

  // A menu closes when the user looks away: any click outside, or Escape.
  useEffect(() => {
    if (!open) return;
    function onPointerDown(event: MouseEvent | TouchEvent) {
      const target = event.target as Node | null;
      if (target && rootRef.current && !rootRef.current.contains(target)) {
        setOpen(false);
      }
    }
    function onKeyDown(event: KeyboardEvent) {
      if (event.key === "Escape") setOpen(false);
    }
    document.addEventListener("mousedown", onPointerDown);
    document.addEventListener("touchstart", onPointerDown);
    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("mousedown", onPointerDown);
      document.removeEventListener("touchstart", onPointerDown);
      document.removeEventListener("keydown", onKeyDown);
    };
  }, [open]);

  async function pick(target: ForwardTarget | "file") {
    try {
      if (target === "file") {
        const name = await forwardMessageToFile(text, from);
        setDone(`saved to ${name}`);
      } else if (target.kind === "friend") {
        // A person is a real delivery through the server, not a local
        // thread append — it lands in their conversation, marked.
        await forwardMessageToFriend(text, from, target.id ?? target.title);
        setDone(`sent to ${target.title}`);
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

  const needle = query.trim().toLowerCase();
  const shown = (targets ?? []).filter(
    (candidate) =>
      !needle || candidate.title.toLowerCase().includes(needle),
  );

  return (
    <span className="forward" ref={rootRef}>
      <button
        type="button"
        className="forward-btn"
        title={t("forwardThis")}
        aria-label={t("forwardThis")}
        onClick={() => void openMenu()}
      >
        ↪
      </button>
      {open && (
        <span className="forward-menu">
          <input
            className="forward-search"
            aria-label={t("forwardSearch")}
            placeholder={t("forwardSearch")}
            value={query}
            autoFocus
            onChange={(e) => setQuery(e.target.value)}
          />
          {shown.map((candidate) => (
            <button
              key={`${candidate.kind}:${candidate.id ?? ""}`}
              type="button"
              onClick={() => void pick(candidate)}
            >
              {candidate.title}
            </button>
          ))}
          {targets !== null && shown.length === 0 && (
            <span className="muted">{t("noMatches")}</span>
          )}
          <button type="button" onClick={() => void pick("file")}>
            {t("newFileInFiles")}
          </button>
          <button
            type="button"
            className="ghost"
            onClick={() => setOpen(false)}
          >
            {t("cancel")}
          </button>
        </span>
      )}
    </span>
  );
}
