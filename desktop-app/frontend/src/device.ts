// The device's senses, asked for exactly when needed — never at startup.
//
// The app runs on phones, tablets, and computers; all three expose the
// same web doors. The microphone is already in use (hold Send to talk —
// the browser prompts on first use). This module adds the other two:
//
// - Location: navigator.geolocation, wrapped in a promise with honest
//   error words. The permission prompt appears the moment the user taps
//   "Share my location" — not before.
// - Camera: an <input type=file capture> — on a phone or tablet this IS
//   the native camera; on a computer it falls back to the file picker.
//   The shot is downscaled to fit the file drawer's 1 MB cap.

export interface DevicePosition {
  lat: number;
  lon: number;
  accuracy_m: number;
}

// Getting a metre-level fix is not a single question — it is watching the
// receiver refine. A phone answers getCurrentPosition with the FIRST fix it
// has, which is usually the coarse wifi/cell/IP estimate (tens of km off)
// because the GNSS chip hasn't locked yet; enableHighAccuracy only *asks*
// for GPS, it doesn't wait for it. So watch instead: keep the tightest fix
// seen, resolve the instant one is good enough, and if the window closes
// with only coarse fixes, hand back the best one WITH its honest radius
// (never a stale cache) rather than a wrong pinpoint.
//
// ``targetAccuracyM`` is "good enough to stop early"; ``timeoutMs`` bounds
// the wait. On a computer with no GNSS the watch only ever yields the coarse
// estimate — returned with its true ±radius so the caller can say how rough
// it is, which is the honest answer that hardware allows.
export function currentPosition(
  timeoutMs = 20_000,
  targetAccuracyM = 35,
): Promise<DevicePosition> {
  return new Promise((resolve, reject) => {
    if (!("geolocation" in navigator)) {
      reject(new Error("this device offers no location service"));
      return;
    }
    const shape = (p: GeolocationPosition): DevicePosition => ({
      lat: p.coords.latitude,
      lon: p.coords.longitude,
      accuracy_m: Math.round(p.coords.accuracy),
    });
    let best: GeolocationPosition | null = null;
    let watchId: number | null = null;
    let settled = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const settle = (finish: () => void) => {
      if (settled) return;
      settled = true;
      if (timer !== undefined) clearTimeout(timer);
      if (watchId !== null) navigator.geolocation.clearWatch(watchId);
      finish();
    };
    // Time's up: answer with the best fix we saw, or say we got none.
    timer = setTimeout(() => {
      settle(() =>
        best
          ? resolve(shape(best))
          : reject(
              new Error(
                "couldn't get a fix in time — try again outdoors or near a window",
              ),
            ),
      );
    }, timeoutMs);
    watchId = navigator.geolocation.watchPosition(
      (position) => {
        // Keep the tightest fix; the receiver sharpens it reading by reading.
        if (!best || position.coords.accuracy < best.coords.accuracy) {
          best = position;
        }
        // Good enough — stop the sensor and answer now.
        if (best.coords.accuracy <= targetAccuracyM) {
          settle(() => resolve(shape(best as GeolocationPosition)));
        }
      },
      (error) => {
        // Permission is a hard no. A transient failure (position momentarily
        // unavailable) is not fatal: let the watch keep trying until it gets
        // a fix or the timeout has the final word.
        if (error.code === error.PERMISSION_DENIED) {
          settle(() =>
            reject(
              new Error(
                "location permission was refused — allow it in the browser/app settings to share where you are",
              ),
            ),
          );
        }
      },
      { enableHighAccuracy: true, timeout: timeoutMs, maximumAge: 0 },
    );
  });
}

// Open the camera (native on mobile; file picker on a computer). Resolves
// null when the user cancels — a cancel is not an error.
export function capturePhoto(): Promise<File | null> {
  return new Promise((resolve) => {
    const input = document.createElement("input");
    input.type = "file";
    input.accept = "image/*";
    input.setAttribute("capture", "environment");
    input.onchange = () => resolve(input.files?.[0] ?? null);
    // A closed picker fires no change event; focus coming back means
    // the user decided. cancel event exists in modern browsers:
    input.oncancel = () => resolve(null);
    input.click();
  });
}

// Read a file into a data URL, refusing honestly when it exceeds the
// budget or when a non-empty file reads back blank — every way a
// "correct name, hollow content" upload could happen must end in words,
// never in a silently empty document that "passed".
function readAsDataUrl(file: File, maxBytes: number): Promise<string> {
  if (file.size > Math.floor(maxBytes * 0.74)) {
    return Promise.reject(
      new Error(`${file.name} is too large for the drawer (1 MB cap)`),
    );
  }
  return new Promise<string>((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => {
      const url = String(reader.result || "");
      const payload = url.split(",", 2)[1] ?? "";
      if (file.size > 0 && payload.length === 0) {
        reject(
          new Error(`could not read ${file.name} — nothing arrived from disk`),
        );
        return;
      }
      resolve(url);
    };
    reader.onerror = () => reject(new Error(`could not read ${file.name}`));
    reader.readAsDataURL(file);
  });
}

// Shrink a shot until it fits the drawer: longest side capped, JPEG
// quality stepped down until the data URL is under the byte budget.
export async function photoToDataUrl(
  file: File,
  { maxDim = 1280, maxBytes = 900_000 }: { maxDim?: number; maxBytes?: number } = {},
): Promise<string> {
  if (typeof createImageBitmap !== "function") {
    // An older webview can't downscale: ship the picture as-is when it
    // fits the budget, refuse honestly when it doesn't — never a blank.
    return readAsDataUrl(file, maxBytes);
  }
  const bitmap = await createImageBitmap(file);
  const scale = Math.min(1, maxDim / Math.max(bitmap.width, bitmap.height));
  const canvas = document.createElement("canvas");
  canvas.width = Math.max(1, Math.round(bitmap.width * scale));
  canvas.height = Math.max(1, Math.round(bitmap.height * scale));
  const context = canvas.getContext("2d");
  if (!context) throw new Error("this device could not process the photo");
  context.drawImage(bitmap, 0, 0, canvas.width, canvas.height);
  for (const quality of [0.85, 0.7, 0.55, 0.4]) {
    const url = canvas.toDataURL("image/jpeg", quality);
    if (url.length <= maxBytes) return url;
  }
  throw new Error("that photo is too large even after downscaling");
}

// Pick any files from the local device (documents, images, anything) —
// the same native picker on phone, tablet, and computer. Resolves []
// when the user cancels: a cancel is not an error.
export function pickLocalFiles(): Promise<File[]> {
  return new Promise((resolve) => {
    const input = document.createElement("input");
    input.type = "file";
    input.multiple = true;
    input.onchange = () => resolve(Array.from(input.files ?? []));
    input.oncancel = () => resolve([]);
    input.click();
  });
}

// Turn a picked file into drawer content within the 1 MB budget: images
// are downscaled to JPEG, text stays text, anything else rides as a
// data URL (base64 inflates ~4/3, so the raw cap is lower). Refusals are
// honest errors naming the file.
export async function fileToDrawerContent(
  file: File,
  maxBytes = 900_000,
): Promise<{ content: string; mediaType: string }> {
  const type = file.type || "";
  if (type.startsWith("image/")) {
    return { content: await photoToDataUrl(file), mediaType: "image/jpeg" };
  }
  const textLike =
    type.startsWith("text/") ||
    type === "application/json" ||
    /\.(md|txt|csv|tsv|json|log)$/i.test(file.name);
  if (textLike) {
    const text = await file.text();
    if (text.length > maxBytes) {
      throw new Error(`${file.name} is too large for the drawer (1 MB cap)`);
    }
    if (file.size > 0 && text.length === 0) {
      // A non-empty file that reads back blank is a FAILED read, not a
      // document: refuse in words instead of saving a hollow file.
      throw new Error(
        `could not read ${file.name} — nothing arrived from disk`,
      );
    }
    return { content: text, mediaType: type || "text/markdown" };
  }
  const dataUrl = await readAsDataUrl(file, maxBytes);
  return { content: dataUrl, mediaType: type || "application/octet-stream" };
}

export function photoName(now = new Date()): string {
  const pad = (n: number) => String(n).padStart(2, "0");
  return (
    `photo-${now.getFullYear()}${pad(now.getMonth() + 1)}${pad(now.getDate())}` +
    `-${pad(now.getHours())}${pad(now.getMinutes())}${pad(now.getSeconds())}.jpg`
  );
}

// ---- the download door: cloud drawer -> this device -----------------------
// The drawer stores text as text and everything else as a data URL; the
// download door turns either back into the REAL file — true bytes, true
// type — and hands it to the device's own save flow.
export function contentToBlob(content: string, mediaType: string): Blob {
  if (content.startsWith("data:")) {
    const [head, payload = ""] = content.split(",", 2);
    const type = head.slice(5).split(";")[0] || mediaType || "application/octet-stream";
    if (head.includes(";base64")) {
      const bytes = Uint8Array.from(atob(payload), (c) => c.charCodeAt(0));
      return new Blob([bytes], { type });
    }
    return new Blob([decodeURIComponent(payload)], { type });
  }
  return new Blob([content], { type: mediaType || "text/plain" });
}

export function saveToDevice(name: string, blob: Blob): void {
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = name;
  anchor.click();
  URL.revokeObjectURL(url);
}
