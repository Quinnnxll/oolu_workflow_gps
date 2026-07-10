import { useCallback, useEffect, useState } from "react";
import { api } from "../api";
import type { FileMeta } from "../api";
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

  function addFolder() {
    const name = folderDraft.trim().replace(/\/+/g, "/").replace(/^\/|\/$/g, "");
    setNaming(false);
    setFolderDraft("");
    if (!name) return;
    const path = cwd ? `${cwd}/${name}` : name;
    setDrafts((d) => (d.includes(path) ? d : [...d, path]));
    setCwd(path);
  }

  return (
    <div className="files-pane">
      <div className="files-head">
        <span className="convo-group">
          {nodeId ? "This node's files" : "Your files"}
          {cwd && <span className="files-path"> / {cwd}</span>}
        </span>
        <span className="row">
          <button className="ghost" onClick={() => setNaming(true)}>
            New folder
          </button>
          <button
            onClick={async () => {
              const doc = await api.createFile("untitled.md", "", nodeId, cwd);
              await refresh();
              setOpen(doc.file_id);
            }}
          >
            New document
          </button>
        </span>
      </div>

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
            : "No files yet. Create one, or ask OoLu to write something down."}
        </div>
      )}

      <div className="files-grid">
        {cwd && (
          <button
            className="file-tile"
            onClick={() =>
              setCwd(cwd.includes("/") ? cwd.slice(0, cwd.lastIndexOf("/")) : "")
            }
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
          >
            <span className="file-tile-icon">▣</span>
            <span className="file-tile-name">{name}</span>
            <span className="file-tile-sub">folder</span>
          </button>
        ))}
        {here.map((f) => (
          <button
            key={f.file_id}
            className="file-tile"
            onClick={() => setOpen(f.file_id)}
          >
            <span className="file-tile-icon">
              {isSheetName(f.name) ? "▤" : "≡"}
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
          Empty folder — create a document to keep it.
        </div>
      )}
    </div>
  );
}
