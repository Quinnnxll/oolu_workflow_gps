import { afterEach, describe, expect, it, vi } from "vitest";
import { currentPosition, photoName } from "./device";

afterEach(() => {
  vi.unstubAllGlobals();
});

type Fix = { coords: { latitude: number; longitude: number; accuracy: number } };

// Stub navigator.geolocation with a watchPosition that emits a scripted
// series of fixes (each a coarsening/sharpening reading), records the
// options, and reports whether the watch was cleared.
function stubWatch(fixes: Fix[]) {
  const state = { opts: undefined as PositionOptions | undefined, cleared: false };
  let stopped = false;
  vi.stubGlobal("navigator", {
    geolocation: {
      watchPosition: (
        ok: (p: Fix) => void,
        _fail: unknown,
        options: PositionOptions,
      ) => {
        state.opts = options;
        // Emit each reading on its own microtask — as the real API does,
        // AFTER watchPosition returns, so the watch id is assigned first.
        let chain = Promise.resolve();
        for (const fix of fixes) {
          chain = chain.then(() => {
            if (!stopped) ok(fix);
          });
        }
        return 7;
      },
      clearWatch: (id: number) => {
        if (id === 7) {
          state.cleared = true;
          stopped = true;
        }
      },
    },
  });
  return state;
}

describe("location", () => {
  it("waits for a fix good enough — not the first coarse one", async () => {
    // The receiver reports a coarse network fix first, then sharpens to GPS.
    const state = stubWatch([
      { coords: { latitude: 52.5, longitude: 13.4, accuracy: 2400 } },
      { coords: { latitude: 52.52, longitude: 13.405, accuracy: 8.3 } },
    ]);
    const here = await currentPosition();
    // The tight fix wins, not the coarse opener.
    expect(here).toEqual({ lat: 52.52, lon: 13.405, accuracy_m: 8 });
    // The metre-precision knobs: GPS on, no stale cache; watch stopped.
    expect(state.opts?.enableHighAccuracy).toBe(true);
    expect(state.opts?.maximumAge).toBe(0);
    expect(state.cleared).toBe(true);
  });

  it("hands back the best coarse fix when the window closes without GPS", async () => {
    vi.useFakeTimers();
    try {
      // A computer with no GNSS: only coarse fixes ever arrive.
      const state = stubWatch([
        { coords: { latitude: 40, longitude: -74, accuracy: 5200 } },
        { coords: { latitude: 40.1, longitude: -74.1, accuracy: 3100 } },
      ]);
      const pending = currentPosition();
      await vi.runAllTimersAsync();
      // The tightest coarse fix, with its honest radius — never a stale cache.
      await expect(pending).resolves.toEqual({
        lat: 40.1,
        lon: -74.1,
        accuracy_m: 3100,
      });
      expect(state.cleared).toBe(true);
    } finally {
      vi.useRealTimers();
    }
  });

  it("says plainly when permission was refused", async () => {
    vi.stubGlobal("navigator", {
      geolocation: {
        watchPosition: (
          _ok: unknown,
          fail: (e: { code: number; PERMISSION_DENIED: number }) => void,
        ) => {
          fail({ code: 1, PERMISSION_DENIED: 1 });
          return 7;
        },
        clearWatch: () => undefined,
      },
    });
    await expect(currentPosition()).rejects.toThrow(/permission was refused/);
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

// The download door: the drawer's stored shapes turn back into REAL
// files — true bytes, true type — on their way to the device.
describe("the download door", () => {
  it("a base64 data URL becomes its true bytes and type", async () => {
    const { contentToBlob } = await import("./device");
    const bytes = new Uint8Array([37, 80, 68, 70]); // "%PDF"
    const payload = btoa(String.fromCharCode(...bytes));
    const blob = contentToBlob(
      `data:application/pdf;base64,${payload}`,
      "application/pdf",
    );
    expect(blob.type).toBe("application/pdf");
    expect(new Uint8Array(await blob.arrayBuffer())).toEqual(bytes);
  });

  it("plain text downloads as the text it is", async () => {
    const { contentToBlob } = await import("./device");
    const blob = contentToBlob("hello", "text/markdown");
    expect(blob.type).toBe("text/markdown");
    expect(await blob.text()).toBe("hello");
  });

  it("saveToDevice hands the file to the device's own save flow", async () => {
    const { contentToBlob, saveToDevice } = await import("./device");
    const clicked: { download: string; href: string }[] = [];
    const realCreate = document.createElement.bind(document);
    vi.spyOn(document, "createElement").mockImplementation(((tag: string) => {
      const el = realCreate(tag);
      if (tag === "a") {
        (el as HTMLAnchorElement).click = () => {
          const a = el as HTMLAnchorElement;
          clicked.push({ download: a.download, href: a.href });
        };
      }
      return el;
    }) as typeof document.createElement);
    vi.stubGlobal("URL", {
      ...URL,
      createObjectURL: () => "blob:fake",
      revokeObjectURL: () => undefined,
    });
    try {
      saveToDevice("report.pdf", contentToBlob("x", "application/pdf"));
      expect(clicked).toEqual([{ download: "report.pdf", href: "blob:fake" }]);
    } finally {
      vi.restoreAllMocks();
    }
  });
});
