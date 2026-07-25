import { accountScope, api } from "./api";
import { conciseName } from "./naming";

// Forwarding: messages and files move between conversations — the OoLu
// chat, a node's interact window, and the file drawers — without retyping.
// Forwarding to a conversation is a DELIVERY, not a display trick: the
// words land in the destination thread's stored history exactly as if the
// user had typed them there, and the destination's model answers in the
// same turn — the full path the composer takes, nothing shorter. No
// "forwarded from" note rides along to the user's own destinations: it is
// their message, moved by their own hand. Only a FRIEND delivery keeps
// the mark, because there a real recipient deserves to know these words
// were carried, not typed live. A forwarded file is a copy into the
// destination drawer (each drawer keeps its own independent record).

export interface ForwardTarget {
  kind: "oolu" | "node" | "file" | "friend";
  id?: string; // node id for "node"; the peer's username for "friend"
  title: string; // what the picker shows
}

export const FORWARDED_MARK = "↪ forwarded";

interface ThreadMsg {
  kind: string;
  text?: string;
  [extra: string]: unknown;
}

// The same storage keys the conversation windows themselves read: the
// OoLu thread cache is PER ACCOUNT (see Chat.tsx — two people on one
// device never read each other's OoLu), a node's thread is per node.
function threadKey(target: ForwardTarget): string {
  return target.kind === "oolu"
    ? `oolu_chat::${accountScope()}`
    : `oolu_node_chat_${target.id ?? ""}`;
}

function loadThread(key: string): ThreadMsg[] {
  try {
    const raw = localStorage.getItem(key);
    const parsed = raw ? JSON.parse(raw) : [];
    return Array.isArray(parsed) ? parsed : [];
  } catch {
    return [];
  }
}

function saveThread(key: string, thread: ThreadMsg[]): void {
  localStorage.setItem(key, JSON.stringify(thread));
}

// Deliver a message into the OoLu conversation or a node's interact
// window: the text joins the destination thread as the user's own turn,
// the destination's model answers it (with the thread's own recent
// history, exactly like its composer), and the reply lands beside it —
// both visible the moment that conversation opens.
export async function forwardMessage(
  text: string,
  target: ForwardTarget,
): Promise<void> {
  if (target.kind === "file") {
    throw new Error("use forwardMessageToFile for file targets");
  }
  if (target.kind === "friend") {
    throw new Error("use forwardMessageToFriend for friend targets");
  }
  const key = threadKey(target);
  const thread = loadThread(key);
  const history = thread
    .filter((m) => m.kind === "user" || m.kind === "assistant")
    .slice(-12)
    .map((m) => ({
      role: m.kind === "user" ? ("user" as const) : ("assistant" as const),
      content: String(m.text ?? ""),
    }));
  thread.push({ kind: "user", text });
  saveThread(key, thread);
  const turn = await api.chat(
    text,
    history,
    target.kind === "node" ? target.id : undefined,
  );
  thread.push({
    kind: "assistant",
    text: turn.reply,
    ...(turn.actions && turn.actions.length ? { actions: turn.actions } : {}),
    ...(turn.reasoning ? { reasoning: turn.reasoning } : {}),
  });
  saveThread(key, thread);
}

// Forwarding to a person is a real delivery through the server — and the
// one destination that keeps the mark: the recipient is told these words
// were carried from elsewhere, never presented as typed live.
export async function forwardMessageToFriend(
  text: string,
  from: string,
  peer: string,
): Promise<void> {
  await api.sendFriendMessage(peer, `${FORWARDED_MARK} from ${from}:\n${text}`);
}

// A forwarded message can also become a document in the Life drawer —
// the words alone; the "forwarded" folder already says how it arrived.
export async function forwardMessageToFile(text: string): Promise<string> {
  const name = `${conciseName(text) || "forwarded"}.md`.toLowerCase();
  const doc = await api.createFile(name, text, undefined, "forwarded");
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
    const { items } = await api.friends();
    for (const f of items ?? []) {
      targets.push({ kind: "friend", id: f.peer, title: f.peer });
    }
  } catch {
    /* no server here: friends are not destinations */
  }
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
