import { afterEach, describe, expect, it } from "vitest";
import { act, cleanup, render } from "@testing-library/react";
import { resetAvatarSignals, updateAvatarSignals } from "../avatar";
import { OoLuAvatar } from "./OoLuAvatar";

afterEach(() => {
  cleanup();
  resetAvatarSignals();
});

describe("OoLuAvatar", () => {
  it("renders the creature: a body, a camouflage layer, and two eyes", () => {
    const { container } = render(<OoLuAvatar />);
    const svg = container.querySelector("svg.oolu-avatar")!;
    expect(svg).toBeTruthy();
    expect(svg.getAttribute("data-mood")).toBe("calm");
    expect(svg.getAttribute("aria-label")).toBe("OoLu (calm)");
    expect(svg.querySelectorAll("path").length).toBe(2); // body + camo
    expect(svg.querySelectorAll("ellipse").length).toBe(2); // eyes
    // The first frame is drawn even without an animation loop.
    const body = svg.querySelector("path")!;
    expect(body.getAttribute("d")).toMatch(/^M /);
  });

  it("changes shape-state with the conversation", () => {
    const { container } = render(<OoLuAvatar />);
    const svg = () => container.querySelector("svg.oolu-avatar")!;

    act(() => updateAvatarSignals({ tone: "bad" }));
    expect(svg().getAttribute("data-mood")).toBe("worried");

    act(() => updateAvatarSignals({ tone: "neutral", listening: true }));
    expect(svg().getAttribute("data-mood")).toBe("excited");

    act(() => updateAvatarSignals({ listening: false, workload: 3 }));
    expect(svg().getAttribute("data-mood")).toBe("thinking");
  });

  it("takes more room than an ordinary account picture by default", () => {
    const { container } = render(<OoLuAvatar />);
    // Ordinary sidebar avatars are 34px; the companion gets 56.
    expect(container.querySelector("svg")!.getAttribute("width")).toBe("56");
  });
});
