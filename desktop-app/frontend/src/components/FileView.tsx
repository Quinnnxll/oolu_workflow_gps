import { useEffect, useMemo, useState } from "react";
import { api } from "../api";
import type { FileDoc } from "../api";
import { parseCsv, serializeCsv } from "../csv";

// Files live in the same conversation surface as everything else: a
// document reads like a message thread page, a sheet is a themed grid —
// no embedded office plugin, the app's own type and colors throughout.

function isSheet(file: FileDoc): boolean {
  return (
    file.media_type === "text/csv" || /\.(csv|tsv)$/i.test(file.name)
  );
}

export function FileView({
  fileId,
  onChanged,
  onDeleted,
}: {
  fileId: string;
  onChanged: () => void;
  onDeleted: () => void;
}) {
  const [file, setFile] = useState<FileDoc | null>(null);
  const [name, setName] = useState("");
  const [error, setError] = useState("");

  useEffect(() => {
    let cancelled = false;
    api
      .file(fileId)
      .then((f) => {
        if (!cancelled) {
          setFile(f);
          setName(f.name);
        }
      })
      .catch((e) => {
        if (!cancelled) setError((e as Error).message);
      });
    return () => {
      cancelled = true;
    };
  }, [fileId]);

  if (error) return <div className="pane-empty">{error}</div>;
  if (!file) return <div className="pane-empty muted">Opening…</div>;

  async function saveName() {
    if (!file || name.trim() === file.name) return;
    const saved = await api.saveFile(file.file_id, { name: name.trim() });
    setFile(saved);
    onChanged();
  }

  async function saveContent(content: string) {
    if (!file) return;
    const saved = await api.saveFile(file.file_id, { content });
    setFile(saved);
    onChanged();
  }

  return (
    <div className="file-view">
      <div className="file-head">
        <input
          className="file-name"
          aria-label="File name"
          value={name}
          onChange={(e) => setName(e.target.value)}
          onBlur={() => void saveName()}
        />
        <button
          className="linklike"
          onClick={async () => {
            await api.deleteFile(file.file_id);
            onDeleted();
          }}
        >
          delete
        </button>
      </div>
      {isSheet(file) ? (
        <Sheet key={file.updated_at} file={file} onSave={saveContent} />
      ) : (
        <Document key={file.updated_at} file={file} onSave={saveContent} />
      )}
    </div>
  );
}

// ---- documents: a reading page first, an editor on request ---------------
function Document({
  file,
  onSave,
}: {
  file: FileDoc;
  onSave: (content: string) => Promise<void>;
}) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(file.content);

  if (!editing) {
    return (
      <>
        <div className="doc-page">
          {file.content ? (
            file.content.split(/\n{2,}/).map((block, i) => (
              <p key={i}>{block}</p>
            ))
          ) : (
            <p className="muted">This document is empty.</p>
          )}
        </div>
        <div className="file-actions">
          <button onClick={() => setEditing(true)}>Edit</button>
        </div>
      </>
    );
  }

  return (
    <>
      <textarea
        className="doc-editor"
        aria-label="Document content"
        value={draft}
        onChange={(e) => setDraft(e.target.value)}
      />
      <div className="file-actions">
        <button
          onClick={async () => {
            await onSave(draft);
            setEditing(false);
          }}
        >
          Save
        </button>
        <button
          className="ghost"
          onClick={() => {
            setDraft(file.content);
            setEditing(false);
          }}
        >
          Cancel
        </button>
      </div>
    </>
  );
}

// ---- sheets: the app's own grid, first row as the header ------------------
function Sheet({
  file,
  onSave,
}: {
  file: FileDoc;
  onSave: (content: string) => Promise<void>;
}) {
  const initial = useMemo(() => {
    const rows = parseCsv(file.content);
    return rows.length ? rows : [[""]];
  }, [file.content]);
  const [rows, setRows] = useState<string[][]>(initial);
  const [dirty, setDirty] = useState(false);

  const width = Math.max(...rows.map((r) => r.length));

  function setCell(r: number, c: number, value: string) {
    setRows((prev) =>
      prev.map((row, ri) =>
        ri === r ? row.map((cell, ci) => (ci === c ? value : cell)) : row,
      ),
    );
    setDirty(true);
  }

  return (
    <>
      <div className="sheet-scroll">
        <table className="sheet">
          <thead>
            <tr>
              {Array.from({ length: width }, (_, c) => (
                <th key={c}>
                  <input
                    aria-label={`header ${c + 1}`}
                    value={rows[0]?.[c] ?? ""}
                    onChange={(e) => setCell(0, c, e.target.value)}
                  />
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.slice(1).map((row, r) => (
              <tr key={r}>
                {Array.from({ length: width }, (_, c) => (
                  <td key={c}>
                    <input
                      aria-label={`cell ${r + 2}:${c + 1}`}
                      value={row[c] ?? ""}
                      onChange={(e) => setCell(r + 1, c, e.target.value)}
                    />
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <div className="file-actions">
        <button
          className="ghost"
          onClick={() => {
            setRows((prev) => [...prev, Array.from({ length: width }, () => "")]);
            setDirty(true);
          }}
        >
          + row
        </button>
        <button
          className="ghost"
          onClick={() => {
            setRows((prev) => prev.map((row) => [...row, ""]));
            setDirty(true);
          }}
        >
          + column
        </button>
        <button
          disabled={!dirty}
          onClick={async () => {
            await onSave(serializeCsv(rows));
            setDirty(false);
          }}
        >
          Save
        </button>
      </div>
    </>
  );
}
