import { describe, expect, it } from "vitest";
import { orderThreads } from "./conversations";

describe("orderThreads: the reading order of a messenger", () => {
  type Thread = {
    id: string;
    at: string;
    pinned?: boolean;
    hidden?: boolean;
  };
  const threads: Thread[] = [
    { id: "old", at: "2026-07-01T10:00:00+00:00" },
    { id: "new", at: "2026-07-20T10:00:00+00:00" },
    { id: "mid", at: "2026-07-10T10:00:00+00:00" },
  ];

  it("puts the most recently spoken uppermost", () => {
    const ordered = orderThreads(threads, (t) => t.at);
    expect(ordered.map((t) => t.id)).toEqual(["new", "mid", "old"]);
  });

  it("pinned threads rise above newer unpinned ones", () => {
    const ordered = orderThreads(
      threads.map((t) => (t.id === "old" ? { ...t, pinned: true } : t)),
      (t) => t.at,
    );
    expect(ordered.map((t) => t.id)).toEqual(["old", "new", "mid"]);
  });

  it("hidden threads leave the list entirely", () => {
    const ordered = orderThreads(
      threads.map((t) => (t.id === "new" ? { ...t, hidden: true } : t)),
      (t) => t.at,
    );
    expect(ordered.map((t) => t.id)).toEqual(["mid", "old"]);
  });

  it("silent fresh threads sink below anything spoken", () => {
    const ordered = orderThreads(
      [...threads, { id: "fresh", at: "", pinned: false }],
      (t) => t.at,
    );
    expect(ordered.map((t) => t.id).at(-1)).toBe("fresh");
  });
});
