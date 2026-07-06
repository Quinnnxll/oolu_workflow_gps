import { useCallback, useEffect, useState } from "react";
import { api } from "../api";
import type { FileMeta } from "../api";
import { FileView } from "./FileView";

// One drawer of files — the Life account's shared drawer (no nodeId) or a
// single node's own files in Work. The list opens in place; a selected
// file becomes the pane with a way back.

function isSheetName(name: string): boolean {
  return /\.(csv|tsv)$/i.test(name);
}

export function FilesPane({ nodeId }: { nodeId?: string }) {
  const [files, setFiles] = useState<FileMeta[]>([]);
  const [open, setOpen] = useState<string | null>(null);

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

  return (
    <div className="files-pane">
      <div className="files-head">
        <span className="convo-group">
          {nodeId ? "This node's files" : "Your files"}
        </span>
        <button
          onClick={async () => {
            const doc = await api.createFile("untitled.md", "", nodeId);
            await refresh();
            setOpen(doc.file_id);
          }}
        >
          New document
        </button>
      </div>
      {files.length === 0 && (
        <div className="pane-empty muted">
          {nodeId
            ? "Nothing here yet — this node keeps its files to itself."
            : "No files yet. Create one, or ask OoLu to write something down."}
        </div>
      )}
      <div className="files-grid">
        {files.map((f) => (
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
    </div>
  );
}
