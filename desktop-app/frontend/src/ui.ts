// The settings node's app.theme and app.language, actually applied.
//
// Theme: a data-theme attribute on the document root — "light"/"dark"
// pin a palette, "system" removes the pin so the OS preference decides
// (the CSS carries both palettes). Language: a small dictionary for the
// UI chrome — labels, placeholders, buttons — switched live; the
// assistant's own words follow the model, not this table. Both choices
// are cached locally so the right look paints before settings load.

import { useEffect, useState } from "react";

export type UiLanguage = "en" | "zh" | "es" | "fr";

const THEME_KEY = "oolu_theme";
const LANG_KEY = "oolu_language";

// The formal names shown wherever a language is picked — never raw codes.
export const LANGUAGE_NAMES: Record<string, string> = {
  en: "English",
  zh: "中文（简体）",
  es: "Español",
  fr: "Français",
};

let listeners: (() => void)[] = [];

export function onUiChange(listener: () => void): () => void {
  listeners.push(listener);
  return () => {
    listeners = listeners.filter((l) => l !== listener);
  };
}

function notify() {
  for (const listener of [...listeners]) listener();
}

export function applyTheme(theme: string): void {
  const root = document.documentElement;
  if (theme === "light" || theme === "dark") {
    root.dataset.theme = theme;
  } else {
    delete root.dataset.theme; // "system": the OS preference decides
  }
  try {
    localStorage.setItem(THEME_KEY, theme);
  } catch {
    /* storage unavailable: the attribute still applied */
  }
}

let language: UiLanguage = "en";

export function currentLanguage(): UiLanguage {
  return language;
}

export function applyLanguage(lang: string): void {
  const next = (["en", "zh", "es", "fr"].includes(lang) ? lang : "en") as UiLanguage;
  const changed = next !== language;
  language = next;
  document.documentElement.lang = next;
  try {
    localStorage.setItem(LANG_KEY, next);
  } catch {
    /* storage unavailable: the language still applied */
  }
  if (changed) notify();
}

// Paint the cached choices before any settings request returns.
export function bootAppearance(): void {
  try {
    applyTheme(localStorage.getItem(THEME_KEY) ?? "system");
    applyLanguage(localStorage.getItem(LANG_KEY) ?? "en");
  } catch {
    /* first run: the defaults stand */
  }
}

// ---- the chrome dictionary --------------------------------------------------
type Entry = Record<UiLanguage, string>;

const STRINGS: Record<string, Entry> = {
  life: { en: "Life", zh: "生活", es: "Vida", fr: "Vie" },
  work: { en: "Work", zh: "工作", es: "Trabajo", fr: "Travail" },
  assistantSub: {
    en: "your assistant",
    zh: "你的助手",
    es: "tu asistente",
    fr: "votre assistant",
  },
  files: { en: "Files", zh: "文件", es: "Archivos", fr: "Fichiers" },
  filesSub: {
    en: "documents & sheets",
    zh: "文档与表格",
    es: "documentos y hojas",
    fr: "documents et feuilles",
  },
  settings: { en: "Settings", zh: "设置", es: "Ajustes", fr: "Réglages" },
  settingsSub: {
    en: "app, account, model, budget",
    zh: "应用、账户、模型、预算",
    es: "aplicación, cuenta, modelo, presupuesto",
    fr: "application, compte, modèle, budget",
  },
  friends: { en: "Friends", zh: "好友", es: "Amistades", fr: "Amis" },
  noder: { en: "Noder", zh: "节点", es: "Nodos", fr: "Nœuds" },
  startConversation: {
    en: "Start a conversation",
    zh: "发起对话",
    es: "Iniciar una conversación",
    fr: "Démarrer une conversation",
  },
  newConversation: {
    en: "New conversation",
    zh: "新对话",
    es: "Nueva conversación",
    fr: "Nouvelle conversation",
  },
  friendsNeedServer: {
    en: "Friends need a server",
    zh: "好友功能需要服务器",
    es: "Amistades requieren un servidor",
    fr: "Les amis nécessitent un serveur",
  },
  nodeActivityHere: {
    en: "Node activity appears here.",
    zh: "节点活动将显示在这里。",
    es: "La actividad de los nodos aparece aquí.",
    fr: "L'activité des nœuds apparaît ici.",
  },
  messageOoLu: {
    en: "Message OoLu…",
    zh: "给 OoLu 发消息…",
    es: "Mensaje a OoLu…",
    fr: "Message à OoLu…",
  },
  send: { en: "Send", zh: "发送", es: "Enviar", fr: "Envoyer" },
  cancel: { en: "cancel", zh: "取消", es: "cancelar", fr: "annuler" },
  forwardThis: {
    en: "Forward this message",
    zh: "转发此消息",
    es: "Reenviar este mensaje",
    fr: "Transférer ce message",
  },
  forwardSearch: {
    en: "search friends and nodes…",
    zh: "搜索好友和节点…",
    es: "buscar amistades y nodos…",
    fr: "rechercher amis et nœuds…",
  },
  newFileInFiles: {
    en: "New file in Files",
    zh: "存为新文件",
    es: "Nuevo archivo en Archivos",
    fr: "Nouveau fichier dans Fichiers",
  },
  noMatches: {
    en: "no matches",
    zh: "没有匹配项",
    es: "sin coincidencias",
    fr: "aucun résultat",
  },
  // Settings chrome (the per-item labels come from the settings node).
  groupApp: { en: "App", zh: "应用", es: "Aplicación", fr: "Application" },
  groupAccount: { en: "Account", zh: "账户", es: "Cuenta", fr: "Compte" },
  groupSubscription: {
    en: "Subscription",
    zh: "订阅",
    es: "Suscripción",
    fr: "Abonnement",
  },
  groupModel: { en: "Model", zh: "模型", es: "Modelo", fr: "Modèle" },
  groupBudget: { en: "Budget", zh: "预算", es: "Presupuesto", fr: "Budget" },
  privacyData: {
    en: "Privacy & data",
    zh: "隐私与数据",
    es: "Privacidad y datos",
    fr: "Confidentialité et données",
  },
  // Choice values that would otherwise show as raw tokens.
  "choice.system": { en: "System", zh: "跟随系统", es: "Sistema", fr: "Système" },
  "choice.light": { en: "Light", zh: "浅色", es: "Claro", fr: "Clair" },
  "choice.dark": { en: "Dark", zh: "深色", es: "Oscuro", fr: "Sombre" },
};

export function t(key: string): string {
  const entry = STRINGS[key];
  if (!entry) return key;
  return entry[language] ?? entry.en;
}

// The formal display name for a CHOICE value: language codes get their
// native names, theme values get translated words, everything else
// (currency codes and the like) passes through untouched.
export function choiceLabel(value: string): string {
  if (LANGUAGE_NAMES[value]) return LANGUAGE_NAMES[value];
  if (STRINGS[`choice.${value}`]) return t(`choice.${value}`);
  return value;
}

// Re-render on language change: subscribe once, read t() at render time.
export function useT(): typeof t {
  const [, force] = useState(0);
  useEffect(() => onUiChange(() => force((v) => v + 1)), []);
  return t;
}
