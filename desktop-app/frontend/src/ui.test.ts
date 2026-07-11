import { afterEach, describe, expect, it } from "vitest";
import {
  applyLanguage,
  applyTheme,
  bootAppearance,
  choiceLabel,
  currentLanguage,
  onUiChange,
  t,
} from "./ui";

afterEach(() => {
  localStorage.clear();
  applyLanguage("en");
  applyTheme("system");
});

describe("theme", () => {
  it("pins light/dark on the root and lets 'system' follow the OS", () => {
    applyTheme("light");
    expect(document.documentElement.dataset.theme).toBe("light");
    applyTheme("dark");
    expect(document.documentElement.dataset.theme).toBe("dark");
    // "system": no pin at all — the CSS media query decides.
    applyTheme("system");
    expect(document.documentElement.dataset.theme).toBeUndefined();
  });

  it("remembers the choice so the next launch paints right away", () => {
    applyTheme("dark");
    expect(localStorage.getItem("oolu_theme")).toBe("dark");
    document.documentElement.removeAttribute("data-theme");
    bootAppearance();
    expect(document.documentElement.dataset.theme).toBe("dark");
  });
});

describe("language", () => {
  it("switches the chrome dictionary and notifies subscribers", () => {
    let notified = 0;
    const off = onUiChange(() => notified++);
    expect(t("files")).toBe("Files");
    applyLanguage("zh");
    expect(currentLanguage()).toBe("zh");
    expect(t("files")).toBe("文件");
    expect(document.documentElement.lang).toBe("zh");
    expect(notified).toBe(1);
    applyLanguage("zh"); // same choice again: no churn
    expect(notified).toBe(1);
    off();
  });

  it("falls back to English for junk codes and missing keys", () => {
    applyLanguage("tlh"); // not offered
    expect(currentLanguage()).toBe("en");
    expect(t("not-a-key")).toBe("not-a-key");
  });
});

describe("choice labels", () => {
  it("shows formal names, never raw codes", () => {
    expect(choiceLabel("en")).toBe("English");
    expect(choiceLabel("zh")).toBe("中文（简体）");
    expect(choiceLabel("es")).toBe("Español");
    expect(choiceLabel("fr")).toBe("Français");
    expect(choiceLabel("system")).toBe("System");
    applyLanguage("es");
    expect(choiceLabel("dark")).toBe("Oscuro");
    // Currency codes and other values pass through untouched.
    expect(choiceLabel("USD")).toBe("USD");
  });
});

describe("chrome beyond Settings", () => {
  it("the whole surface follows the language — Chat, Work, Files, feed words", async () => {
    const { applyLanguage, t } = await import("./ui");
    const { humanizeEvent } = await import("./humanize");

    applyLanguage("zh");
    expect(t("work.myNodes")).toBe("我的节点");
    expect(t("files.yours")).toBe("你的文件");
    expect(t("net.header")).toBe("网络访问");
    expect(t("run.approve")).toBe("批准");
    expect(humanizeEvent("workflow.started")).toBe("开始工作");

    applyLanguage("fr");
    expect(t("chat.welcome")).toContain("OoLu");
    expect(t("files.newFolder")).toBe("Nouveau dossier");
    expect(humanizeEvent("workflow.executed")).toBe("A exécuté les actions");
    // Unknown events still degrade to readable words, never raw codes.
    expect(humanizeEvent("weird.thing")).toBe("weird thing");

    applyLanguage("en");
    expect(humanizeEvent("workflow.started")).toBe("Started working");
  });

  it("templated strings place their values per language", async () => {
    const { applyLanguage, tf } = await import("./ui");
    applyLanguage("es");
    expect(tf("files.reallyDelete", { n: 3 })).toBe("¿Eliminar 3 de verdad?");
    applyLanguage("zh");
    expect(tf("work.healthy", { pct: 90 })).toBe("健康度 90%");
    applyLanguage("en");
    expect(tf("work.healthy", { pct: 90 })).toBe("90% healthy");
  });
});
