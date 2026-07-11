import { useCallback, useEffect, useState } from "react";
import { api } from "../api";
import type { FileMeta } from "../api";
import { fileToDrawerContent, pickLocalFiles } from "../device";
import { forwardFile, forwardTargets } from "../forward";
import type { ForwardTarget } from "../forward";
import { FileView } from "./FileView";

// One drawer of files — the Life account's shared drawer (no nodeId) or a
// single node's own files in Work. Folders organize the drawer: a folder
// is derived from the files that name it (plus any freshly created empty
// ones held client-side until a file lands in them). The list opens in
// place; a selected file becomes the pane with a way back.

function isSheetName(name: string): boolean {
  return /\.(csv|tsv)$/i.test(name);
}

// The immediate child folders of `cwd`, derived from every file's folder
// path ("a/b/c" seen from "a" contributes "b").
export function childFolders(files: FileMeta[], cwd: string): string[] {
  const children = new Set<string>();
  const prefix = cwd ? cwd + "/" : "";
  for (const f of files) {
    const folder = f.folder ?? "";
    if (folder === cwd || !folder.startsWith(prefix)) continue;
    const child = folder.slice(prefix.length).split("/")[0];
    if (child) children.add(child);
  }
  return Array.from(children).sort();
}

export function FilesPane({ nodeId }: { nodeId?: string }) {
  const [files, setFiles] = useState<FileMeta[]>([]);
  const [open, setOpen] = useState<string | null>(null);
  const [cwd, setCwd] = useState("");
  // Folders created before any file lives in them — client-side until a
  // document lands, at which point the file itself carries the folder.
  const [drafts, setDrafts] = useState<string[]>([]);
  const [naming, setNaming] = useState(false);
  const [folderDraft, setFolderDraft] = useState("");
  // The one + menu: upload from the device, or make a folder.
  const [adding, setAdding] = useState(false);
  // Select mode: tiles toggle instead of opening, and the bar below acts
  // on everything selected at once — forward or delete, one move.
  const [selecting, setSelecting] = useState(false);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [targets, setTargets] = useState<ForwardTarget[] | null>(null);
  const [forwarding, setForwarding] = useState(false);
  const [notice, setNotice] = useState("");

  const refresh = useCallback(async () => {
    try {
      setFiles((await api.files(nodeId)).items ?? []);
    } catch {
      setFiles([]);
    }
  }, [nodeId]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  if (open) {
    return (
      <FileView
        key={open}
        fileId={open}
        onBack={() => {
          setOpen(null);
          void refresh();
        }}
        onChanged={refresh}
        onDeleted={() => {
          setOpen(null);
          void refresh();
        }}
      />
    );
  }

  const here = files.filter((f) => (f.folder ?? "") === cwd);
  const prefix = cwd ? cwd + "/" : "";
  const folders = Array.from(
    new Set([
      ...childFolders(files, cwd),
      ...drafts
        .filter((d) => d !== cwd && d.startsWith(prefix))
        .map((d) => d.slice(prefix.length).split("/")[0])
        .filter(Boolean),
    ]),
  ).sort();

  function leaveSelectMode() {
    setSelecting(false);
    setSelected(new Set());
    setConfirmDelete(false);
    setForwarding(false);
  }

  function toggle(fileId: string) {
    setSelected((current) => {
      const next = new Set(current);
      if (next.has(fileId)) next.delete(fileId);
      else next.add(fileId);
      return next;
    });
    setConfirmDelete(false);
  }

  async function upload() {
    setNotice("");
    const picked = await pickLocalFiles();
    if (picked.length === 0) return;
    let saved = 0;
    const refused: string[] = [];
    for (const file of picked) {
      try {
        const { content, mediaType } = await fileToDrawerContent(file);
        await api.createFile(file.name, content, nodeId, cwd, mediaType);
        saved++;
      } catch (e) {
        refused.push((e as Error).message);
      }
    }
    setNotice(
      [
        saved > 0 ? `uploaded ${saved} file${saved === 1 ? "" : "s"}` : "",
        ...refused,
      ]
        .filter(Boolean)
        .join(" · "),
    );
    await refresh();
  }

  async function deleteSelected() {
    const ids = Array.from(selected);
    let gone = 0;
    for (const id of ids) {
      try {
        await api.deleteFile(id);
        gone++;
      } catch {
        /* one refusal must not stop the rest */
      }
    }
    setNotice(`deleted ${gone} file${gone === 1 ? "" : "s"}`);
    leaveSelectMode();
    await refresh();
  }

  async function forwardSelectedTo(target: ForwardTarget) {
    const chosen = files.filter((f) => selected.has(f.file_id));
    let sent = 0;
    const failed: string[] = [];
    for (const f of chosen) {
      try {
        if (target.kind === "friend") {
          // A person gets a real delivery: the message carries the file.
          await api.sendFriendMessage(
            target.id ?? target.title,
            `📄 forwarded a file: ${f.name}`,
            f.file_id,
          );
        } else {
          await forwardFile(
            f.file_id,
            target.kind === "node" ? target.id : undefined,
          );
        }
        sent++;
      } catch {
        failed.push(f.name);
      }
    }
    setNotice(
      `forwarded ${sent} file${sent === 1 ? "" : "s"} to ${target.title}` +
        (failed.length ? ` · failed: ${failed.join(", ")}` : ""),
    );
    leaveSelectMode();
    await refresh();
  }

  function addFolder() {
    const name = folderDraft.trim().replace(/\/+/g, "/").replace(/^\/|\/$/g, "");
    setNaming(false);
    setFolderDraft("");
    if (!name) return;
    const path = cwd ? `${cwd}/${name}` : name;
    setDrafts((d) => (d.includes(path) ? d : [...d, path]));
    setCwd(path);
  }

  // Dragging a file tile onto a folder tile MOVES it there — the file
  // itself carries its folder, so a move is one honest PATCH.
  async function moveFile(fileId: string, folder: string) {
    setNotice("");
    try {
      const moved = await api.saveFile(fileId, { folder });
      setNotice(`moved “${moved.name}” to ${folder || "the top level"}`);
    } catch (e) {
      setNotice((e as Error).message);
    }
    await refresh();
  }

  function dropHandlers(folder: string) {
    return {
      onDragOver: (e: React.DragEvent) => e.preventDefault(),
      onDrop: (e: React.DragEvent) => {
        e.preventDefault();
        const fileId = e.dataTransfer?.getData("text/oolu-file-id");
        if (fileId) void moveFile(fileId, folder);
      },
    };
  }

  return (
    <div className="files-pane">
      <div className="files-head">
        <span className="convo-group">
          {nodeId ? "This node's files" : "Your files"}
          {cwd && <span className="files-path"> / {cwd}</span>}
        </span>
        <span className="row">
          <button
            className="ghost"
            onClick={() => (selecting ? leaveSelectMode() : setSelecting(true))}
          >
            {selecting ? "Done" : "Select"}
          </button>
          {/* Documents are OoLu's to write — ask in the chat. The + holds
              what only a human can do here: bring device files in, and
              shape folders. */}
          <span className="composer-plus">
            <button
              className="ghost plus-btn"
              aria-label="Add"
              title="Upload from this device, or make a folder"
              onClick={() => setAdding((open) => !open)}
            >
              ＋
            </button>
            {adding && (
              <span className="forward-menu plus-menu">
                <button
                  type="button"
                  onClick={() => {
                    setAdding(false);
                    void upload();
                  }}
                >
                  Upload from device
                </button>
                <button
                  type="button"
                  onClick={() => {
                    setAdding(false);
                    setNaming(true);
                  }}
                >
                  New folder
                </button>
              </span>
            )}
          </span>
        </span>
      </div>

      {notice && <div className="muted files-notice">{notice}</div>}

      {selecting && selected.size > 0 && (
        <div className="row files-actions">
          <span className="muted">{selected.size} selected</span>
          <span className="forward">
            <button
              onClick={async () => {
                setForwarding(true);
                if (targets === null) setTargets(await forwardTargets());
              }}
            >
              Forward…
            </button>
            {forwarding && (
              <span className="forward-menu">
                {(targets ?? []).map((t) => (
                  <button
                    key={`${t.kind}:${t.id ?? ""}`}
                    type="button"
                    onClick={() => void forwardSelectedTo(t)}
                  >
                    {t.title}
                  </button>
                ))}
                <button
                  type="button"
                  className="ghost"
                  onClick={() => setForwarding(false)}
                >
                  cancel
                </button>
              </span>
            )}
          </span>
          {!confirmDelete ? (
            <button className="ghost" onClick={() => setConfirmDelete(true)}>
              Delete…
            </button>
          ) : (
            <button className="danger" onClick={() => void deleteSelected()}>
              Really delete {selected.size}?
            </button>
          )}
        </div>
      )}

      {naming && (
        <div className="row files-newfolder">
          <input
            aria-label="Folder name"
            placeholder="folder name"
            value={folderDraft}
            autoFocus
            onChange={(e) => setFolderDraft(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") addFolder();
              if (e.key === "Escape") setNaming(false);
            }}
          />
          <button onClick={addFolder}>Create</button>
        </div>
      )}

      {files.length === 0 && folders.length === 0 && !cwd && (
        <div className="pane-empty muted">
          {nodeId
            ? "Nothing here yet — this node keeps its files to itself."
            : "No files yet — ask OoLu to write something down, or press + " +
              "to bring one in from this device."}
        </div>
      )}

      <div className="files-grid">
        {cwd && (
          <button
            className="file-tile"
            onClick={() =>
              setCwd(cwd.includes("/") ? cwd.slice(0, cwd.lastIndexOf("/")) : "")
            }
            {...dropHandlers(
              cwd.includes("/") ? cwd.slice(0, cwd.lastIndexOf("/")) : "",
            )}
          >
            <span className="file-tile-icon">←</span>
            <span className="file-tile-name">..</span>
            <span className="file-tile-sub">up one level</span>
          </button>
        )}
        {folders.map((name) => (
          <button
            key={`dir:${name}`}
            className="file-tile folder"
            onClick={() => setCwd(cwd ? `${cwd}/${name}` : name)}
            {...dropHandlers(cwd ? `${cwd}/${name}` : name)}
          >
            <span className="file-tile-icon">▣</span>
            <span className="file-tile-name">{name}</span>
            <span className="file-tile-sub">folder · drop files to move</span>
          </button>
        ))}
        {here.map((f) => (
          <button
            key={f.file_id}
            className={`file-tile ${
              selecting && selected.has(f.file_id) ? "on" : ""
            }`}
            aria-pressed={selecting ? selected.has(f.file_id) : undefined}
            draggable={!selecting}
            onDragStart={(e) =>
              e.dataTransfer?.setData("text/oolu-file-id", f.file_id)
            }
            onClick={() =>
              selecting ? toggle(f.file_id) : setOpen(f.file_id)
            }
          >
            <span className="file-tile-icon">
              {selecting
                ? selected.has(f.file_id)
                  ? "☑"
                  : "☐"
                : isSheetName(f.name)
                  ? "▤"
                  : "≡"}
            </span>
            <span className="file-tile-name">{f.name}</span>
            <span className="file-tile-sub">
              {isSheetName(f.name) ? "sheet" : "document"} ·{" "}
              {(f.size / 1024).toFixed(1)} kB
            </span>
          </button>
        ))}
      </div>

      {cwd && here.length === 0 && folders.length === 0 && (
        <div className="pane-empty muted">
          Empty folder — drag a file in, or ask OoLu to write one here.
        </div>
      )}
    </div>
  );
}
