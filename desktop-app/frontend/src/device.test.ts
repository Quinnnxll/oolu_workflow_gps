import { afterEach, describe, expect, it, vi } from "vitest";
import { currentPosition, photoName } from "./device";

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("location", () => {
  it("resolves coordinates when the device allows it", async () => {
    vi.stubGlobal("navigator", {
      geolocation: {
        getCurrentPosition: (
          ok: (p: {
            coords: { latitude: number; longitude: number; accuracy: number };
          }) => void,
        ) =>
          ok({
            coords: { latitude: 52.52, longitude: 13.405, accuracy: 12.4 },
          }),
      },
    });
    const here = await currentPosition();
    expect(here).toEqual({ lat: 52.52, lon: 13.405, accuracy_m: 12 });
  });

  it("says plainly when permission was refused", async () => {
    vi.stubGlobal("navigator", {
      geolocation: {
        getCurrentPosition: (
          _ok: unknown,
          fail: (e: { code: number; PERMISSION_DENIED: number }) => void,
        ) => fail({ code: 1, PERMISSION_DENIED: 1 }),
      },
    });
    await expect(currentPosition()).rejects.toThrow(
      /permission was refused/,
    );
  });

  it("says so when the device has no location service at all", async () => {
    vi.stubGlobal("navigator", {});
    await expect(currentPosition()).rejects.toThrow(/no location service/);
  });
});

describe("camera", () => {
  it("names shots by the moment they were taken", () => {
    expect(photoName(new Date(2026, 6, 10, 9, 5, 3))).toBe(
      "photo-20260710-090503.jpg",
    );
  });
});

// The REAL reading path — no mocks. This is the answer to "does upload
// actually carry the file's content, or just its name?": every branch
// must produce the true bytes, and a non-empty file that reads back
// blank must REFUSE instead of minting a hollow document that "passed".
describe("fileToDrawerContent — the real reading path", () => {
  it("a text file's actual words arrive, not just its name", async () => {
    const { fileToDrawerContent } = await import("./device");
    const file = new File(["hello drawer"], "notes.md", {
      type: "text/markdown",
    });
    const { content, mediaType } = await fileToDrawerContent(file);
    expect(content).toBe("hello drawer");
    expect(mediaType).toBe("text/markdown");
  });

  it("a typeless .csv still reads as text (some OSes send no MIME)", async () => {
    const { fileToDrawerContent } = await import("./device");
    const file = new File(["a,b\n1,2"], "table.csv", { type: "" });
    const { content } = await fileToDrawerContent(file);
    expect(content).toBe("a,b\n1,2");
  });

  it("a binary rides as a data URL carrying its real bytes", async () => {
    const { fileToDrawerContent } = await import("./device");
    const bytes = new Uint8Array([1, 2, 3, 250]);
    const file = new File([bytes], "blob.bin", {
      type: "application/octet-stream",
    });
    const { content, mediaType } = await fileToDrawerContent(file);
    expect(content.startsWith("data:application/octet-stream;base64,")).toBe(
      true,
    );
    expect(atob(content.split(",")[1])).toHaveLength(4);
    expect(mediaType).toBe("application/octet-stream");
  });

  it("an image still uploads where downscaling is unavailable", async () => {
    // jsdom has no createImageBitmap — exactly the older-webview shape:
    // the picture ships as-is instead of failing (or arriving blank).
    const { fileToDrawerContent } = await import("./device");
    const file = new File([new Uint8Array([137, 80, 78, 71])], "shot.png", {
      type: "image/png",
    });
    const { content } = await fileToDrawerContent(file);
    expect(content.startsWith("data:image/png;base64,")).toBe(true);
    expect(atob(content.split(",")[1]).length).toBeGreaterThan(0);
  });

  it("a non-empty file that reads back blank is refused, never saved hollow", async () => {
    const { fileToDrawerContent } = await import("./device");
    const liar = new File(["real words"], "ghost.md", {
      type: "text/markdown",
    });
    Object.defineProperty(liar, "text", {
      value: async () => "", // the failed-read shape, whatever caused it
    });
    await expect(fileToDrawerContent(liar)).rejects.toThrow(
      /could not read ghost.md/,
    );
  });
});
