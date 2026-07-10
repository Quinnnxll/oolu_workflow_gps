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
