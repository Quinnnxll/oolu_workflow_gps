import { useEffect, useState } from "react";
import { api } from "../api";
import type { TurnFileRef } from "../api";
import { saveToDevice } from "../device";
import { useT } from "../ui";

// Attachments in a conversation bubble, first-class: the TRUE bytes are
// fetched once from the drawer (the bearer token rides the fetch — a
// plain src cannot carry it) and serve both faces at once — the preview
// (a photo inline, a clip or a sound with controls, an honest card for
// anything the app can't render in place) and the lossless download.
// A ref whose file is gone renders a quiet name, never a broken box.

const KIND_WORDS: [string, string][] = [
  ["application/pdf", "PDF document"],
  ["wordprocessingml", "Word document"],
  ["spreadsheetml", "Excel workbook"],
  ["presentationml", "PowerPoint deck"],
  ["video/", "video"],
  ["audio/", "audio"],
  ["image/", "picture"],
];

function kindWords(mediaType: string): string {
  for (const [marker, words] of KIND_WORDS) {
    if (mediaType.includes(marker)) return words;
  }
  return "file";
}

function Attachment({ file }: { file: TurnFileRef }) {
  const tr = useT();
  const [blob, setBlob] = useState<Blob | null>(null);
  const [url, setUrl] = useState<string | null>(null);
  const [gone, setGone] = useState(false);

  useEffect(() => {
    let cancelled = false;
    let held: string | null = null;
    api
      .fileBytes(file.file_id)
      .then((bytes) => {
        if (cancelled) return;
        held = URL.createObjectURL(bytes);
        setBlob(bytes);
        setUrl(held);
      })
      .catch(() => {
        if (!cancelled) setGone(true);
      });
    return () => {
      cancelled = true;
      if (held) URL.revokeObjectURL(held);
    };
  }, [file.file_id]);

  if (gone) {
    // The file left the drawer after the turn — the name stays as the
    // honest record of what was sent.
    return <span className="attachment-gone muted">📎 {file.name}</span>;
  }

  const type = file.media_type || "";
  const download = blob && (
    <button
      type="button"
      className="linklike attachment-download"
      onClick={() => saveToDevice(file.name, blob)}
    >
      {tr("file.download")}
    </button>
  );
  if (url && type.startsWith("image/")) {
    return (
      <figure className="attachment">
        <img className="attachment-media" src={url} alt={file.name} />
        <figcaption>
          {file.name} {download}
        </figcaption>
      </figure>
    );
  }
  if (url && type.startsWith("video/")) {
    return (
      <figure className="attachment">
        <video className="attachment-media" src={url} controls title={file.name} />
        <figcaption>
          {file.name} {download}
        </figcaption>
      </figure>
    );
  }
  if (url && type.startsWith("audio/")) {
    return (
      <figure className="attachment">
        <audio className="attachment-media" src={url} controls title={file.name} />
        <figcaption>
          {file.name} {download}
        </figcaption>
      </figure>
    );
  }
  return (
    <span className="attachment attachment-card">
      📎 {file.name}
      <span className="muted"> — {kindWords(type)}</span> {download}
    </span>
  );
}

export function AttachmentStrip({ files }: { files?: TurnFileRef[] }) {
  if (!files || files.length === 0) return null;
  return (
    <div className="attachment-strip">
      {files.map((f) => (
        <Attachment key={f.file_id} file={f} />
      ))}
    </div>
  );
}
