import { api } from "./api";
import { conciseName } from "./naming";

// Forwarding: messages and files move between conversations — the OoLu
// chat, a node's interact window, and the file drawers — without retyping.
// A forwarded message lands in the destination thread's stored history,
// marked with where it came from, and appears when that conversation is
// opened; a forwarded file is a copy into the destination drawer (each
// drawer keeps its own independent record).

export interface ForwardTarget {
  kind: "oolu" | "node" | "file";
  id?: string; // node id for "node"
  title: string; // what the picker shows
}

export const FORWARDED_MARK = "↪ forwarded";

function threadKey(target: ForwardTarget): string {
  return target.kind === "oolu"
    ? "oolu_chat"
    : `oolu_node_chat_${target.id ?? ""}`;
}

// Append one marked user-side message to a stored conversation thread.
// Both thread models (the OoLu chat and a node's interact window) render
// a {kind: "user", text} entry, so the forwarded line is readable there
// the moment the conversation opens.
export function forwardMessage(
  text: string,
  from: string,
  target: ForwardTarget,
): void {
  if (target.kind === "file") {
    throw new Error("use forwardMessageToFile for file targets");
  }
  const key = threadKey(target);
  let thread: unknown[] = [];
  try {
    const raw = localStorage.getItem(key);
    const parsed = raw ? JSON.parse(raw) : [];
    thread = Array.isArray(parsed) ? parsed : [];
  } catch {
    thread = [];
  }
  thread.push({
    kind: "user",
    text: `${FORWARDED_MARK} from ${from}:\n${text}`,
  });
  localStorage.setItem(key, JSON.stringify(thread));
}

// A forwarded message can also become a document in the Life drawer.
export async function forwardMessageToFile(
  text: string,
  from: string,
): Promise<string> {
  const name = `${conciseName(text) || "forwarded"}.md`.toLowerCase();
  const doc = await api.createFile(
    name,
    `> ${FORWARDED_MARK} from ${from}\n\n${text}`,
    undefined,
    "forwarded",
  );
  return doc.name;
}

// Copy a file into another drawer (a node's, or the Life drawer when
// nodeId is undefined). A copy, deliberately: each drawer keeps its own
// record — forwarding never moves someone's original.
export async function forwardFile(
  fileId: string,
  targetNodeId: string | undefined,
): Promise<string> {
  const doc = await api.file(fileId);
  const copied = await api.createFile(
    doc.name,
    doc.content,
    targetNodeId,
    "forwarded",
  );
  return copied.name;
}

// The destinations a picker offers: the OoLu conversation plus every node
// on the caller's desk (best-effort — no desk means just OoLu).
export async function forwardTargets(): Promise<ForwardTarget[]> {
  const targets: ForwardTarget[] = [{ kind: "oolu", title: "OoLu" }];
  try {
    const { items } = await api.workNodes();
    for (const n of items) {
      targets.push({ kind: "node", id: n.node_id, title: n.title });
    }
  } catch {
    /* no desk here: OoLu alone is still a destination */
  }
  return targets;
}
