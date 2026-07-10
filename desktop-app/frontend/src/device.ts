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

export function currentPosition(
  timeoutMs = 15_000,
): Promise<DevicePosition> {
  return new Promise((resolve, reject) => {
    if (!("geolocation" in navigator)) {
      reject(new Error("this device offers no location service"));
      return;
    }
    navigator.geolocation.getCurrentPosition(
      (position) =>
        resolve({
          lat: position.coords.latitude,
          lon: position.coords.longitude,
          accuracy_m: Math.round(position.coords.accuracy),
        }),
      (error) =>
        reject(
          new Error(
            error.code === error.PERMISSION_DENIED
              ? "location permission was refused — allow it in the browser/app settings to share where you are"
              : "the device could not determine its location right now",
          ),
        ),
      { enableHighAccuracy: false, timeout: timeoutMs, maximumAge: 60_000 },
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

// Shrink a shot until it fits the drawer: longest side capped, JPEG
// quality stepped down until the data URL is under the byte budget.
export async function photoToDataUrl(
  file: File,
  { maxDim = 1280, maxBytes = 900_000 }: { maxDim?: number; maxBytes?: number } = {},
): Promise<string> {
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

export function photoName(now = new Date()): string {
  const pad = (n: number) => String(n).padStart(2, "0");
  return (
    `photo-${now.getFullYear()}${pad(now.getMonth() + 1)}${pad(now.getDate())}` +
    `-${pad(now.getHours())}${pad(now.getMinutes())}${pad(now.getSeconds())}.jpg`
  );
}
