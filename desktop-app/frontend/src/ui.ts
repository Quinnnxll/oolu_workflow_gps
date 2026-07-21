// The settings node's app.theme and app.language, actually applied.
//
// Theme: a data-theme attribute on the document root — "light"/"dark"
// pin a palette, "system" removes the pin so the OS preference decides
// (the CSS carries both palettes). Language: a small dictionary for the
// UI chrome — labels, placeholders, buttons — switched live; the
// assistant's own words follow the model, not this table. Both choices
// are cached locally so the right look paints before settings load.

import { useEffect, useState } from "react";

import { accountScope } from "./api";

export type UiLanguage = "en" | "zh" | "zh-hant" | "es" | "fr";

const LANGUAGES: readonly UiLanguage[] = ["en", "zh", "zh-hant", "es", "fr"];

const THEME_KEY = "oolu_theme";
const LANG_KEY = "oolu_language";

// The formal names shown wherever a language is picked — never raw codes.
export const LANGUAGE_NAMES: Record<string, string> = {
  en: "English",
  zh: "中文（简体）",
  "zh-hant": "中文（繁體）",
  es: "Español",
  fr: "Français",
};

// The device's own language, mapped onto what the chrome speaks — what a
// first run (the sign-in screen included) shows before any choice exists.
export function deviceLanguage(locale?: string): UiLanguage {
  const raw = (locale ?? navigator.language ?? "en").toLowerCase();
  if (raw.startsWith("zh")) {
    // Traditional-script regions and explicit Hant tags read Traditional;
    // everything else under zh reads Simplified.
    return /hant|tw|hk|mo/.test(raw) ? "zh-hant" : "zh";
  }
  for (const lang of LANGUAGES) {
    if (lang !== "zh-hant" && raw.startsWith(lang)) return lang;
  }
  return "en";
}

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
  const next = (
    LANGUAGES.includes(lang as UiLanguage) ? lang : "en"
  ) as UiLanguage;
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

// The folded/unfolded state of the conversation list — one choice shared
// by Life and Work, surviving restarts like theme and language do.
const SIDEBAR_KEY = "oolu_sidebar_folded";

export function loadSidebarFolded(): boolean {
  try {
    return localStorage.getItem(SIDEBAR_KEY) === "1";
  } catch {
    return false;
  }
}

export function saveSidebarFolded(folded: boolean): void {
  try {
    localStorage.setItem(SIDEBAR_KEY, folded ? "1" : "0");
  } catch {
    /* storage unavailable: the fold still applied for this session */
  }
}

// A friend conversation's typing block, surviving pane switches and
// restarts. A DISCARDED representative draft lands here — kept, not
// buried — so the user can rework it in their own time. Keyed PER
// ACCOUNT: another sign-in on this device never reads these words, and
// sign-out purges every oolu_compose_* key (api.signOut).
const COMPOSE_KEY = "oolu_compose_";

function composeKey(peer: string): string {
  return `${COMPOSE_KEY}${accountScope()}::${peer}`;
}

export function loadCompose(peer: string): string {
  try {
    return localStorage.getItem(composeKey(peer)) ?? "";
  } catch {
    return "";
  }
}

export function saveCompose(peer: string, text: string): void {
  try {
    if (text) localStorage.setItem(composeKey(peer), text);
    else localStorage.removeItem(composeKey(peer));
  } catch {
    /* storage unavailable: the words still sit in the box this session */
  }
}

// Paint the cached choices before any settings request returns. With no
// stored choice yet (a first run — the SIGN-IN screen included), the
// device's own language decides, never a hardcoded English.
export function bootAppearance(): void {
  try {
    applyTheme(localStorage.getItem(THEME_KEY) ?? "system");
    applyLanguage(localStorage.getItem(LANG_KEY) ?? deviceLanguage());
  } catch {
    /* first run: the defaults stand */
  }
}

// ---- the chrome dictionary --------------------------------------------------
// Entries carry the four authored languages; Traditional Chinese lives in
// the generated ZH_HANT table at the bottom of this file (falling back to
// Simplified, then English, when a key is missing there).
type Entry = Record<"en" | "zh" | "es" | "fr", string>;

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
  // Settings chrome.
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
  subscriptionNote: {
    en: "Your plan is a commitment, not a preference — it's shown here and managed in the account console (cancel the current plan there to change terms).",
    zh: "套餐是一项承诺，而非偏好设置——此处仅展示，管理请前往账户中心（先取消当前套餐才能更改条款）。",
    es: "Tu plan es un compromiso, no una preferencia: se muestra aquí y se gestiona en la consola de cuenta (cancela el plan actual allí para cambiar las condiciones).",
    fr: "Votre forfait est un engagement, pas une préférence : il s'affiche ici et se gère dans la console du compte (annulez-y le forfait actuel pour changer de conditions).",
  },
  modelNote: {
    en: "Where OoLu's brain lives. Subscription follows your OoLu plan (Claude first). Add your own API key below and switch the default model to own API to override the plan with your key — or run a local model server on this machine and choose local: no key, no cloud.",
    zh: "OoLu 的大脑所在。订阅模式跟随你的 OoLu 套餐（优先 Claude）。在下方添加自己的 API 密钥并把默认模型切换为自有 API，即可用你的密钥覆盖套餐——或在本机运行本地模型服务器并选择本地：无需密钥，不上云。",
    es: "Dónde vive el cerebro de OoLu. Suscripción sigue tu plan de OoLu (Claude primero). Añade tu propia clave de API abajo y cambia el modelo predeterminado a API propia para usar tu clave — o ejecuta un servidor de modelo local en esta máquina y elige local: sin clave, sin nube.",
    fr: "Où vit le cerveau d'OoLu. Abonnement suit votre forfait OoLu (Claude d'abord). Ajoutez votre clé API ci-dessous et passez le modèle par défaut sur API personnelle pour utiliser votre clé — ou lancez un serveur de modèle local sur cette machine et choisissez local : sans clé, sans cloud.",
  },
  managePlan: {
    en: "Manage plan",
    zh: "管理套餐",
    es: "Gestionar plan",
    fr: "Gérer le forfait",
  },
  managePlanDesc: {
    en: "Upgrade with deduction, cancel, or switch monthly/yearly.",
    zh: "升级（抵扣余额）、取消，或切换按月/按年。",
    es: "Mejora con deducción, cancela o cambia entre mensual y anual.",
    fr: "Mettez à niveau avec déduction, annulez ou basculez mensuel/annuel.",
  },
  openConsole: {
    en: "Open the account console",
    zh: "打开账户中心",
    es: "Abrir la consola de cuenta",
    fr: "Ouvrir la console du compte",
  },
  regionSuggests: {
    en: "Your region suggests",
    zh: "根据你的地区建议使用",
    es: "Tu región sugiere",
    fr: "Votre région suggère",
  },
  use: { en: "Use", zh: "使用", es: "Usar", fr: "Utiliser" },
  downloadData: {
    en: "Download my data",
    zh: "下载我的数据",
    es: "Descargar mis datos",
    fr: "Télécharger mes données",
  },
  downloadDataDesc: {
    en: "Everything this host holds about you, as one JSON document.",
    zh: "此主机上与你相关的全部数据，导出为一个 JSON 文档。",
    es: "Todo lo que este host guarda sobre ti, en un solo documento JSON.",
    fr: "Tout ce que cet hôte détient sur vous, en un seul document JSON.",
  },
  download: { en: "Download", zh: "下载", es: "Descargar", fr: "Télécharger" },
  deleteAccount: {
    en: "Delete my account",
    zh: "删除我的账户",
    es: "Eliminar mi cuenta",
    fr: "Supprimer mon compte",
  },
  deleteAccountDesc: {
    en: "Erases your messages, conversation, sign-in identities, and card details, and disables the account forever. Files in the shared drawer stay — delete yours in Files first.",
    zh: "删除你的消息、对话、登录身份和银行卡信息，并永久停用账户。共享抽屉中的文件会保留——请先在“文件”中删除属于你的文件。",
    es: "Borra tus mensajes, la conversación, las identidades de acceso y los datos de tarjeta, y desactiva la cuenta para siempre. Los archivos del cajón compartido permanecen: elimínalos antes en Archivos.",
    fr: "Efface vos messages, la conversation, les identités de connexion et les données de carte, et désactive le compte pour toujours. Les fichiers du tiroir partagé restent — supprimez d'abord les vôtres dans Fichiers.",
  },
  legal: { en: "Legal", zh: "法律条款", es: "Legal", fr: "Mentions légales" },
  legalDesc: {
    en: "The words this host serves at its public legal URLs.",
    zh: "此主机在其公开法律链接上提供的条款内容。",
    es: "Los textos que este host publica en sus URL legales públicas.",
    fr: "Les textes que cet hôte publie à ses URL légales publiques.",
  },
  // ---- Chat chrome: first-run card, composer, device asks, quick starts.
  "chat.welcome": {
    en: "Hey! ⚡ I'm OoLu, your get-it-done sidekick. What are we tackling first?",
    zh: "嘿！⚡ 我是 OoLu，你的干活搭档。我们先从什么开始？",
    es: "¡Hola! ⚡ Soy OoLu, tu compinche resolutivo. ¿Qué atacamos primero?",
    fr: "Salut ! ⚡ Je suis OoLu, ton acolyte qui fait avancer les choses. On attaque quoi en premier ?",
  },
  "chat.firstRunTitle": {
    en: "First time here? A minute to your first task:",
    zh: "第一次来？一分钟完成你的第一个任务：",
    es: "¿Primera vez aquí? Un minuto hasta tu primera tarea:",
    fr: "Première visite ? Une minute jusqu'à votre première tâche :",
  },
  "chat.sayHi": { en: "Say hi", zh: "打个招呼", es: "Saluda", fr: "Dites bonjour" },
  "chat.reminderRing": {
    en: "Reminder:",
    zh: "提醒：",
    es: "Recordatorio:",
    fr: "Rappel :",
  },
  "chat.sayHiTail": {
    en: "— hear how I talk.",
    zh: "——听听我怎么说话。",
    es: "— escucha cómo hablo.",
    fr: "— écoutez comment je parle.",
  },
  "chat.tryTask": {
    en: "Try a first task",
    zh: "试试第一个任务",
    es: "Prueba una primera tarea",
    fr: "Essayez une première tâche",
  },
  "chat.tryTaskTail": {
    en: "— I'll put it in the box; press Send and watch it run in Noder.",
    zh: "——我会把它填进输入框；按发送，然后在节点里看它运行。",
    es: "— la pongo en el cuadro; pulsa Enviar y mírala correr en Nodos.",
    fr: "— je la mets dans la zone ; appuyez sur Envoyer et regardez-la tourner dans Nœuds.",
  },
  "chat.brainTip": {
    en: "Give me a brain: open Settings in the list to add a model key or point me at a local model — tasks run without one, conversation gets smarter with one.",
    zh: "给我一个大脑：在列表中打开“设置”，添加模型密钥或指向本地模型——没有它任务也能运行，有了它对话更聪明。",
    es: "Dame un cerebro: abre Ajustes en la lista para añadir una clave de modelo o apuntarme a un modelo local — las tareas corren sin él, la conversación mejora con él.",
    fr: "Donnez-moi un cerveau : ouvrez Réglages dans la liste pour ajouter une clé de modèle ou m'orienter vers un modèle local — les tâches tournent sans, la conversation s'améliore avec.",
  },
  "chat.gotIt": {
    en: "Got it — hide this",
    zh: "知道了——隐藏",
    es: "Entendido — ocultar",
    fr: "Compris — masquer",
  },
  "chat.listening": { en: "Listening…", zh: "正在聆听…", es: "Escuchando…", fr: "J'écoute…" },
  "chat.tapToStop": {
    en: "Listening — tap to stop",
    zh: "正在聆听——点按停止",
    es: "Escuchando — toca para parar",
    fr: "J'écoute — touchez pour arrêter",
  },
  "chat.tapHold": {
    en: "Tap to send · hold to speak",
    zh: "点按发送 · 长按说话",
    es: "Toca para enviar · mantén para hablar",
    fr: "Touchez pour envoyer · maintenez pour parler",
  },
  "chat.reminderChip": { en: "reminder", zh: "提醒", es: "recordatorio", fr: "rappel" },
  "chat.openTask": {
    en: "open this task's action window",
    zh: "打开该任务的操作窗口",
    es: "abrir la ventana de acciones de esta tarea",
    fr: "ouvrir la fenêtre d'action de cette tâche",
  },
  "quick.whatCanYouDo": {
    en: "What can you do?",
    zh: "你能做什么？",
    es: "¿Qué sabes hacer?",
    fr: "Que sais-tu faire ?",
  },
  "quick.myTasks": { en: "My tasks", zh: "我的任务", es: "Mis tareas", fr: "Mes tâches" },
  "quick.myFiles": { en: "My files", zh: "我的文件", es: "Mis archivos", fr: "Mes fichiers" },
  "quick.myNodes": { en: "My nodes", zh: "我的节点", es: "Mis nodos", fr: "Mes nœuds" },
  "quick.mySettings": { en: "My settings", zh: "我的设置", es: "Mis ajustes", fr: "Mes réglages" },
  "mood.calm": { en: "here with you", zh: "在你身边", es: "aquí contigo", fr: "là avec vous" },
  "mood.happy": {
    en: "loving how that went ✨",
    zh: "结果太棒了 ✨",
    es: "encantado con cómo salió ✨",
    fr: "ravi du résultat ✨",
  },
  "mood.thinking": {
    en: "heads-down on your tasks",
    zh: "正埋头处理你的任务",
    es: "concentrado en tus tareas",
    fr: "plongé dans vos tâches",
  },
  "mood.worried": {
    en: "on it — sorting a problem",
    zh: "处理中——正在解决一个问题",
    es: "en ello — resolviendo un problema",
    fr: "dessus — je règle un problème",
  },
  "mood.excited": {
    en: "fired up and all ears! ⚡",
    zh: "干劲十足，洗耳恭听！⚡",
    es: "¡a tope y todo oídos! ⚡",
    fr: "gonflé à bloc et tout ouïe ! ⚡",
  },
  "device.shareLocation": {
    en: "Share my location",
    zh: "共享我的位置",
    es: "Compartir mi ubicación",
    fr: "Partager ma position",
  },
  "device.takePhoto": { en: "Take a photo", zh: "拍照", es: "Tomar una foto", fr: "Prendre une photo" },
  "device.chooseFile": {
    en: "Choose a file",
    zh: "选择文件",
    es: "Elegir un archivo",
    fr: "Choisir un fichier",
  },
  "device.notNow": { en: "Not now", zh: "暂不", es: "Ahora no", fr: "Pas maintenant" },
  "device.locationSettled": {
    en: "location request settled",
    zh: "位置请求已处理",
    es: "solicitud de ubicación resuelta",
    fr: "demande de position réglée",
  },
  "device.cameraSettled": {
    en: "camera request settled",
    zh: "相机请求已处理",
    es: "solicitud de cámara resuelta",
    fr: "demande de caméra réglée",
  },
  "device.fileSettled": {
    en: "file request settled",
    zh: "文件请求已处理",
    es: "solicitud de archivo resuelta",
    fr: "demande de fichier réglée",
  },
  // ---- The run card: one piece of work inside the conversation.
  "run.gone": {
    en: "This task is no longer available.",
    zh: "该任务已不可用。",
    es: "Esta tarea ya no está disponible.",
    fr: "Cette tâche n'est plus disponible.",
  },
  "run.starting": { en: "Starting…", zh: "启动中…", es: "Iniciando…", fr: "Démarrage…" },
  "run.approve": { en: "Approve", zh: "批准", es: "Aprobar", fr: "Approuver" },
  "run.reject": { en: "Reject", zh: "拒绝", es: "Rechazar", fr: "Refuser" },
  "run.retry": { en: "Retry", zh: "重试", es: "Reintentar", fr: "Réessayer" },
  "run.runAgain": {
    en: "Run again",
    zh: "再次运行",
    es: "Ejecutar de nuevo",
    fr: "Relancer",
  },
  "run.retrying": { en: "Retrying…", zh: "重试中…", es: "Reintentando…", fr: "Nouvel essai…" },
  "run.abort": { en: "Abort", zh: "中止", es: "Abortar", fr: "Abandonner" },
  "run.showSteps": { en: "what I did", zh: "我做了什么", es: "qué hice", fr: "ce que j'ai fait" },
  "run.hideSteps": {
    en: "hide what I did",
    zh: "隐藏我做了什么",
    es: "ocultar qué hice",
    fr: "masquer ce que j'ai fait",
  },
  "run.fetching": {
    en: "Fetching the record…",
    zh: "正在获取记录…",
    es: "Obteniendo el registro…",
    fr: "Récupération du journal…",
  },
  "run.nothingYet": {
    en: "Nothing recorded yet.",
    zh: "尚无记录。",
    es: "Nada registrado todavía.",
    fr: "Rien d'enregistré pour l'instant.",
  },
  "run.retriesOne": {
    en: "1 retry so far",
    zh: "已重试 1 次",
    es: "1 reintento hasta ahora",
    fr: "1 nouvel essai jusqu'ici",
  },
  "run.retriesMany": {
    en: "{n} retries so far",
    zh: "已重试 {n} 次",
    es: "{n} reintentos hasta ahora",
    fr: "{n} nouveaux essais jusqu'ici",
  },
  "run.nextRebuilds": {
    en: " — the next retry lets OoLu plan and rebuild the path",
    zh: "——下次重试将让 OoLu 规划并重建路径",
    es: " — el próximo reintento deja a OoLu planificar y reconstruir la ruta",
    fr: " — le prochain essai laisse OoLu planifier et reconstruire le chemin",
  },
  "status.needsAnswer": {
    en: "needs an answer",
    zh: "需要回答",
    es: "necesita una respuesta",
    fr: "attend une réponse",
  },
  "status.needsDecision": {
    en: "needs a decision",
    zh: "需要决定",
    es: "necesita una decisión",
    fr: "attend une décision",
  },
  "status.snag": { en: "hit a snag", zh: "遇到问题", es: "topó un problema", fr: "a accroché" },
  "status.done": { en: "done", zh: "完成", es: "hecho", fr: "terminé" },
  "status.failed": { en: "failed", zh: "失败", es: "falló", fr: "échoué" },
  "status.cancelled": { en: "cancelled", zh: "已取消", es: "cancelada", fr: "annulée" },
  "status.working": { en: "working…", zh: "进行中…", es: "trabajando…", fr: "en cours…" },
  // ---- The run's voice (humanize.statusSentence).
  "voice.clarification": {
    en: "I need an answer from you to continue.",
    zh: "我需要你的回答才能继续。",
    es: "Necesito una respuesta tuya para continuar.",
    fr: "J'ai besoin d'une réponse de votre part pour continuer.",
  },
  "voice.confirmation": {
    en: "I need your go-ahead before I act.",
    zh: "行动前我需要你的许可。",
    es: "Necesito tu visto bueno antes de actuar.",
    fr: "J'ai besoin de votre feu vert avant d'agir.",
  },
  "voice.approval": {
    en: "This needs an authorized approval before I act.",
    zh: "这需要经授权的批准我才能行动。",
    es: "Esto necesita una aprobación autorizada antes de actuar.",
    fr: "Ceci nécessite une approbation autorisée avant que j'agisse.",
  },
  "voice.incident": {
    en: "Something went wrong — tell me how to proceed.",
    zh: "出了点问题——告诉我该怎么办。",
    es: "Algo salió mal — dime cómo proceder.",
    fr: "Quelque chose a mal tourné — dites-moi comment procéder.",
  },
  "voice.completed": {
    en: "Done — here's the verified result.",
    zh: "完成——这是经验证的结果。",
    es: "Hecho — aquí está el resultado verificado.",
    fr: "Terminé — voici le résultat vérifié.",
  },
  "voice.failed": {
    en: "It didn't work.",
    zh: "没有成功。",
    es: "No funcionó.",
    fr: "Ça n'a pas fonctionné.",
  },
  "voice.cancelledSentence": {
    en: "Stopped, as you asked.",
    zh: "已按你的要求停止。",
    es: "Detenido, como pediste.",
    fr: "Arrêté, comme demandé.",
  },
  "voice.working": {
    en: "I'm on it — you can watch every step below.",
    zh: "我在处理——你可以在下方看到每一步。",
    es: "Estoy en ello — puedes ver cada paso abajo.",
    fr: "J'y suis — vous pouvez suivre chaque étape ci-dessous.",
  },
  // ---- Function words for audit events (humanize.humanizeEvent).
  "event.workflow.submitted": {
    en: "Accepted the job",
    zh: "已接受任务",
    es: "Aceptó el trabajo",
    fr: "A accepté le travail",
  },
  "event.workflow.started": {
    en: "Started working",
    zh: "开始工作",
    es: "Empezó a trabajar",
    fr: "S'est mis au travail",
  },
  "event.workflow.advance": {
    en: "Moved to the next step",
    zh: "进入下一步",
    es: "Pasó al siguiente paso",
    fr: "Est passé à l'étape suivante",
  },
  "event.workflow.advanced": {
    en: "Moved to the next step",
    zh: "进入下一步",
    es: "Pasó al siguiente paso",
    fr: "Est passé à l'étape suivante",
  },
  "event.workflow.executed": {
    en: "Carried out the actions",
    zh: "执行了操作",
    es: "Ejecutó las acciones",
    fr: "A exécuté les actions",
  },
  "event.workflow.paused": {
    en: "Paused — waiting on you",
    zh: "已暂停——等待你",
    es: "En pausa — esperándote",
    fr: "En pause — vous attend",
  },
  "event.workflow.resumed": {
    en: "Picked it back up",
    zh: "已继续",
    es: "Lo retomó",
    fr: "A repris",
  },
  "event.workflow.completed": {
    en: "Finished the job",
    zh: "完成了任务",
    es: "Terminó el trabajo",
    fr: "A terminé le travail",
  },
  "event.workflow.failed": {
    en: "Hit a failure",
    zh: "遇到失败",
    es: "Encontró un fallo",
    fr: "A rencontré un échec",
  },
  "event.workflow.incident": {
    en: "Ran into a problem",
    zh: "遇到问题",
    es: "Se topó con un problema",
    fr: "A rencontré un problème",
  },
  "event.workflow.cancelled": {
    en: "Stopped on your request",
    zh: "已按你的请求停止",
    es: "Se detuvo a petición tuya",
    fr: "Arrêté à votre demande",
  },
  "event.workflow.preflight_failed": {
    en: "Stopped before running — the preflight checks failed",
    zh: "运行前已停止——预检未通过",
    es: "Se detuvo antes de correr — fallaron las comprobaciones previas",
    fr: "Arrêté avant l'exécution — les vérifications préalables ont échoué",
  },
  "event.contract.held": {
    en: "Held the request for a manual commit",
    zh: "请求已暂挂，等待人工确认",
    es: "Retuvo la solicitud para un visto bueno manual",
    fr: "A retenu la demande pour une validation manuelle",
  },
  "event.contract.approved": {
    en: "An approver committed the request",
    zh: "审批人已确认该请求",
    es: "Un aprobador confirmó la solicitud",
    fr: "Un approbateur a validé la demande",
  },
  "event.contract.declined": {
    en: "An approver declined the request",
    zh: "审批人已拒绝该请求",
    es: "Un aprobador rechazó la solicitud",
    fr: "Un approbateur a refusé la demande",
  },
  "event.contract.expired": {
    en: "The held request expired undecided",
    zh: "暂挂的请求已过期，未做决定",
    es: "La solicitud retenida expiró sin decidirse",
    fr: "La demande retenue a expiré sans décision",
  },
  "event.feedback.received": {
    en: "Noted your feedback",
    zh: "已记下你的反馈",
    es: "Anotó tu comentario",
    fr: "A noté votre retour",
  },
  "event.skill.blocked": {
    en: "Blocked an unsafe action",
    zh: "拦截了不安全的操作",
    es: "Bloqueó una acción insegura",
    fr: "A bloqué une action dangereuse",
  },
  // Choice values that would otherwise show as raw tokens.
  "choice.system": { en: "System", zh: "跟随系统", es: "Sistema", fr: "Système" },
  "choice.light": { en: "Light", zh: "浅色", es: "Claro", fr: "Clair" },
  "choice.dark": { en: "Dark", zh: "深色", es: "Oscuro", fr: "Sombre" },
  "choice.fast": { en: "Fast", zh: "快速", es: "Rápido", fr: "Rapide" },
  "choice.reasoning": {
    en: "Reasoning",
    zh: "深度思考",
    es: "Razonamiento",
    fr: "Raisonnement",
  },
  "choice.subscription": {
    en: "Subscription",
    zh: "订阅",
    es: "Suscripción",
    fr: "Abonnement",
  },
  "choice.own-api": {
    en: "Own API key",
    zh: "自有 API 密钥",
    es: "Clave de API propia",
    fr: "Clé API personnelle",
  },
  "choice.local": { en: "Local", zh: "本地", es: "Local", fr: "Local" },
  "choice.auto": { en: "Auto", zh: "自动", es: "Automático", fr: "Auto" },
  "choice.metric": {
    en: "Metric (SI)",
    zh: "公制 (SI)",
    es: "Métrico (SI)",
    fr: "Métrique (SI)",
  },
  "choice.imperial": {
    en: "Imperial",
    zh: "英制",
    es: "Imperial",
    fr: "Impérial",
  },
  "choice.monthly": { en: "Monthly", zh: "按月", es: "Mensual", fr: "Mensuel" },
  "choice.yearly": { en: "Yearly", zh: "按年", es: "Anual", fr: "Annuel" },
  // ---- Work chrome: the noder's desk.
  "work.myNodes": { en: "My nodes", zh: "我的节点", es: "Mis nodos", fr: "Mes nœuds" },
  "work.addNodeTitle": {
    en: "Create a node or onboard an existing one",
    zh: "创建节点或接管已有节点",
    es: "Crea un nodo o hazte cargo de uno existente",
    fr: "Créer un nœud ou reprendre un nœud existant",
  },
  "work.empty": {
    en: "No nodes yet — press + to create or onboard one.",
    zh: "还没有节点——按 + 创建或接管一个。",
    es: "Aún no hay nodos — pulsa + para crear o incorporar uno.",
    fr: "Pas encore de nœud — appuyez sur + pour en créer ou en reprendre un.",
  },
  "work.pick": {
    en: "Pick a node to see what it has been doing.",
    zh: "选择一个节点，看看它一直在做什么。",
    es: "Elige un nodo para ver qué ha estado haciendo.",
    fr: "Choisissez un nœud pour voir ce qu'il a fait.",
  },
  "work.pickSub": {
    en: "Earnings and health update as runs verify.",
    zh: "收益与健康度随运行验证而更新。",
    es: "Ganancias y salud se actualizan a medida que se verifican las ejecuciones.",
    fr: "Gains et santé se mettent à jour au fil des exécutions vérifiées.",
  },
  "work.noRunsYet": {
    en: "no runs yet",
    zh: "尚无运行",
    es: "sin ejecuciones aún",
    fr: "aucune exécution",
  },
  "work.healthy": {
    en: "{pct}% healthy",
    zh: "健康度 {pct}%",
    es: "{pct}% saludable",
    fr: "{pct} % sain",
  },
  "regime.supernode": { en: "Supernode", zh: "超级节点", es: "Supernodo", fr: "Supernœud" },
  "regime.audit": { en: "Audit", zh: "审计", es: "Auditoría", fr: "Audit" },
  "regime.autogrow": {
    en: "Auto-growing",
    zh: "自动生长",
    es: "Autocrecimiento",
    fr: "Auto-croissance",
  },
  "regime.standalone": {
    en: "standalone",
    zh: "独立",
    es: "independiente",
    fr: "autonome",
  },
  "work.createTab": {
    en: "Create a node",
    zh: "创建节点",
    es: "Crear un nodo",
    fr: "Créer un nœud",
  },
  "work.onboardTab": {
    en: "Onboard existing",
    zh: "接管已有节点",
    es: "Incorporar existente",
    fr: "Reprendre existant",
  },
  "work.name": { en: "Name", zh: "名称", es: "Nombre", fr: "Nom" },
  "work.whatItDoes": {
    en: "What it does",
    zh: "它做什么",
    es: "Qué hace",
    fr: "Ce qu'il fait",
  },
  "work.fnLabel": {
    en: "Function (optional — bring your own code)",
    zh: "函数（可选——带上你自己的代码）",
    es: "Función (opcional — trae tu propio código)",
    fr: "Fonction (facultatif — apportez votre propre code)",
  },
  "work.uploadPy": {
    en: "Upload a .py function",
    zh: "上传 .py 函数",
    es: "Subir una función .py",
    fr: "Téléverser une fonction .py",
  },
  "work.fnPlaceholder": {
    en: "Paste or upload a self-contained Python function. It must call emit_result once with its output. It runs sandboxed — no network, no host credentials — and is screened and verified before it is ever stored.",
    zh: "粘贴或上传一个自包含的 Python 函数。它必须调用一次 emit_result 输出结果。它在沙箱中运行——无网络、无主机凭据——并在存储前经过筛查与验证。",
    es: "Pega o sube una función Python autocontenida. Debe llamar a emit_result una vez con su salida. Corre en un sandbox — sin red, sin credenciales del host — y se examina y verifica antes de almacenarse.",
    fr: "Collez ou téléversez une fonction Python autonome. Elle doit appeler emit_result une fois avec son résultat. Elle tourne en bac à sable — sans réseau, sans identifiants de l'hôte — et est examinée et vérifiée avant d'être stockée.",
  },
  "work.fixedNote": {
    en: "The choices below are fixed at creation — they can never be changed later.",
    zh: "以下选择在创建时即固定——之后永远无法更改。",
    es: "Las opciones de abajo quedan fijadas al crear — nunca podrán cambiarse.",
    fr: "Les choix ci-dessous sont fixés à la création — ils ne pourront jamais changer.",
  },
  "work.supernodeCheck": {
    en: "Supernode — manages many nodes for a group, a corporation, or a government division, with humans in full control (always audits)",
    zh: "超级节点——为团体、企业或政府部门管理众多节点，由人完全掌控（始终审计）",
    es: "Supernodo — gestiona muchos nodos para un grupo, una empresa o una división de gobierno, con humanos en pleno control (siempre audita)",
    fr: "Supernœud — gère de nombreux nœuds pour un groupe, une entreprise ou une administration, avec des humains aux commandes (audite toujours)",
  },
  "work.underSupernode": {
    en: "Under Supernode",
    zh: "隶属超级节点",
    es: "Bajo Supernodo",
    fr: "Sous Supernœud",
  },
  "work.noneStandalone": {
    en: "(none — standalone, no authority)",
    zh: "（无——独立节点，无权限级别）",
    es: "(ninguno — independiente, sin autoridad)",
    fr: "(aucun — autonome, sans autorité)",
  },
  "work.authority": { en: "Authority", zh: "权限级别", es: "Autoridad", fr: "Autorité" },
  "work.claimNote": {
    en: "A node created under a Supernode starts with NO responsible account. Its node id is the claim ticket: give it only to the person who should onboard, and never post it publicly — the user account that onboards becomes the responsible shown on the node.",
    zh: "在超级节点下创建的节点开始时没有负责人账户。节点 id 就是认领凭证：只交给应当接管的人，切勿公开发布——接管的用户账户将成为节点上显示的负责人。",
    es: "Un nodo creado bajo un Supernodo empieza SIN cuenta responsable. Su id de nodo es el vale de reclamo: dáselo solo a quien deba incorporarlo y nunca lo publiques — la cuenta que lo incorpora pasa a ser el responsable mostrado en el nodo.",
    fr: "Un nœud créé sous un Supernœud démarre SANS compte responsable. Son id de nœud est le ticket de réclamation : donnez-le uniquement à la personne qui doit le reprendre et ne le publiez jamais — le compte qui le reprend devient le responsable affiché sur le nœud.",
  },
  "work.auditCheck": {
    en: "Audit node — every request must be committed manually",
    zh: "审计节点——每个请求都必须人工确认",
    es: "Nodo de auditoría — cada solicitud debe confirmarse manualmente",
    fr: "Nœud d'audit — chaque demande doit être validée manuellement",
  },
  "work.autogrowCheck": {
    en: "Auto-growing — data passing this node may feed new development",
    zh: "自动生长——经过此节点的数据可用于新的开发",
    es: "Autocrecimiento — los datos que pasan por este nodo pueden alimentar nuevo desarrollo",
    fr: "Auto-croissance — les données transitant par ce nœud peuvent nourrir de nouveaux développements",
  },
  "work.policyCheck": {
    en: "I agree to the Node Policy — clone, fraud, and zombie nodes are detected and can be restricted or removed by the platform",
    zh: "我同意节点政策——克隆、欺诈和僵尸节点会被检测，平台可对其限制或移除",
    es: "Acepto la Política de Nodos — los nodos clonados, fraudulentos y zombis se detectan y la plataforma puede restringirlos o eliminarlos",
    fr: "J'accepte la Politique des Nœuds — les nœuds clones, frauduleux et zombies sont détectés et la plateforme peut les restreindre ou les retirer",
  },
  "work.policyFirst": {
    en: "Please agree to the Node Policy first — it is what lets the platform restrict or remove clone, fraud, and zombie nodes.",
    zh: "请先同意节点政策——正是它授权平台限制或移除克隆、欺诈和僵尸节点。",
    es: "Acepta primero la Política de Nodos — es lo que permite a la plataforma restringir o eliminar nodos clonados, fraudulentos y zombis.",
    fr: "Veuillez d'abord accepter la Politique des Nœuds — c'est elle qui permet à la plateforme de restreindre ou retirer les nœuds clones, frauduleux et zombies.",
  },
  "work.onboardNote": {
    en: "Take responsibility for a node that already exists. Audit, auto-growing, and any Supernode membership or authority were fixed when it was created — onboarding offers no choices. Onboarding names YOU: your user ID appears on the node as its responsible.",
    zh: "为一个已存在的节点承担责任。审计、自动生长以及任何超级节点归属或权限级别都在创建时已固定——接管时没有任何选项。接管将署上你的名字：你的用户 ID 会作为负责人显示在节点上。",
    es: "Asume la responsabilidad de un nodo que ya existe. Auditoría, autocrecimiento y cualquier pertenencia o autoridad de Supernodo quedaron fijadas al crearse — incorporarse no ofrece opciones. Incorporarse te nombra a TI: tu ID de usuario aparece en el nodo como su responsable.",
    fr: "Prenez la responsabilité d'un nœud déjà existant. Audit, auto-croissance et toute appartenance ou autorité de Supernœud ont été fixées à sa création — la reprise n'offre aucun choix. La reprise VOUS nomme : votre ID utilisateur apparaît sur le nœud comme responsable.",
  },
  "work.nodeId": { en: "Node id", zh: "节点 id", es: "Id del nodo", fr: "Id du nœud" },
  "work.working": { en: "Working…", zh: "处理中…", es: "Trabajando…", fr: "En cours…" },
  "work.createNode": { en: "Create node", zh: "创建节点", es: "Crear nodo", fr: "Créer le nœud" },
  "work.onboard": { en: "Onboard", zh: "接管", es: "Incorporar", fr: "Reprendre" },
  "work.responsible": { en: "responsible", zh: "负责人", es: "responsable", fr: "responsable" },
  "work.admin": { en: "admin", zh: "管理组", es: "administrador", fr: "admin" },
  "work.notOnboarded": {
    en: "not onboarded yet",
    zh: "尚未接管",
    es: "aún sin incorporar",
    fr: "pas encore repris",
  },
  "work.unclaimedNote": {
    en: "This node has no responsible account yet. Do not show its node id publicly — whoever onboards with it becomes the responsible. Share it only with the person meant to take responsibility; once they onboard, their user ID appears here.",
    zh: "此节点尚无负责人账户。不要公开展示其节点 id——凭它接管的人将成为负责人。只把它交给应当负责的人；对方接管后，其用户 ID 会显示在这里。",
    es: "Este nodo aún no tiene cuenta responsable. No muestres su id públicamente — quien se incorpore con él pasa a ser el responsable. Compártelo solo con la persona que deba asumirlo; cuando se incorpore, su ID de usuario aparecerá aquí.",
    fr: "Ce nœud n'a pas encore de compte responsable. N'affichez pas son id publiquement — quiconque le reprend en devient le responsable. Partagez-le uniquement avec la personne prévue ; une fois la reprise faite, son ID utilisateur apparaîtra ici.",
  },
  "work.under": { en: "under", zh: "隶属", es: "bajo", fr: "sous" },
  "work.memberNodes": {
    en: "Member nodes",
    zh: "成员节点",
    es: "Nodos miembros",
    fr: "Nœuds membres",
  },
  "work.keepIdPrivate": {
    en: "not onboarded — keep its id private",
    zh: "未接管——请保密其 id",
    es: "sin incorporar — mantén su id en privado",
    fr: "non repris — gardez son id privé",
  },
  "work.pending": { en: "Pending", zh: "待处理", es: "Pendiente", fr: "En attente" },
  "work.tabActivity": { en: "Activity", zh: "活动", es: "Actividad", fr: "Activité" },
  "work.tabInteract": { en: "Interact", zh: "交互", es: "Interactuar", fr: "Interagir" },
  "work.orderLabel": {
    en: "Execution order for",
    zh: "执行顺序：",
    es: "Orden de ejecución de",
    fr: "Ordre d'exécution de",
  },
  "work.orderStep": {
    en: "step {n}",
    zh: "第 {n} 步",
    es: "paso {n}",
    fr: "étape {n}",
  },
  "work.onDemand": {
    en: "on demand",
    zh: "按需调用",
    es: "bajo demanda",
    fr: "à la demande",
  },
  "work.orderHint": {
    en: "The org's SOP: work flows to the next number — the same number runs in parallel, empty means called whenever needed. Only the Supernode's own humans can set it.",
    zh: "组织的 SOP：工作按编号依次传递——相同编号并行运行，留空表示按需调用。只有该超级节点的所有者才能设置。",
    es: "El SOP de la organización: el trabajo fluye al siguiente número; el mismo número corre en paralelo y vacío significa que se llama cuando haga falta. Solo los humanos del Supernodo pueden fijarlo.",
    fr: "Le SOP de l'organisation : le travail passe au numéro suivant — le même numéro s'exécute en parallèle, vide signifie appelé au besoin. Seuls les humains du Supernœud peuvent le régler.",
  },
  "work.orderBad": {
    en: "a step is a whole number from 1 up — or empty for on demand",
    zh: "步骤编号是从 1 开始的整数——留空表示按需调用",
    es: "un paso es un número entero desde 1 — o vacío para bajo demanda",
    fr: "une étape est un nombre entier à partir de 1 — ou vide pour à la demande",
  },
  "work.imitate": {
    en: "Imitate",
    zh: "模仿学习",
    es: "Imitar",
    fr: "Imiter",
  },
  "work.imitateRecording": {
    en: "Learning…",
    zh: "学习中…",
    es: "Aprendiendo…",
    fr: "Apprentissage…",
  },
  "work.imitateHint": {
    en: "OoLu can't watch other apps — there is no screen or key recording. Teach here instead: name the goal, describe each step in order, and run the real work through this node while the lesson records — the execution logs pair with your words, and stop-and-build turns the demonstration into a capable node.",
    zh: "OoLu 无法监视其他应用——没有屏幕或按键录制。请在这里教学：说出目标，按顺序描述每一步，并在录制期间通过该节点运行真实工作——执行日志会与你的步骤自动配对，结束并构建即可把演示变成一个可用节点。",
    es: "OoLu no puede observar otras aplicaciones: no hay grabación de pantalla ni de teclas. Enseña aquí: nombra el objetivo, describe cada paso en orden y ejecuta el trabajo real a través de este nodo mientras la lección graba; los registros de ejecución se emparejan con tus palabras y al terminar se construye un nodo capaz.",
    fr: "OoLu ne peut pas observer les autres applications — pas d'enregistrement d'écran ni de touches. Enseignez ici : nommez l'objectif, décrivez chaque étape dans l'ordre et exécutez le vrai travail via ce nœud pendant l'enregistrement — les journaux d'exécution s'apparient avec vos mots, et l'arrêt construit un nœud capable.",
  },
  "work.imitateGoal": {
    en: "what should the new node do?",
    zh: "新节点要做什么？",
    es: "¿qué debe hacer el nuevo nodo?",
    fr: "que doit faire le nouveau nœud ?",
  },
  "work.imitateStart": {
    en: "Start the lesson",
    zh: "开始教学",
    es: "Empezar la lección",
    fr: "Commencer la leçon",
  },
  "work.imitateStepPh": {
    en: "describe the next step…",
    zh: "描述下一步…",
    es: "describe el siguiente paso…",
    fr: "décrivez l'étape suivante…",
  },
  "work.imitateAdd": {
    en: "Add step",
    zh: "添加步骤",
    es: "Añadir paso",
    fr: "Ajouter l'étape",
  },
  "work.imitateBuild": {
    en: "Stop & build the node",
    zh: "结束并构建节点",
    es: "Terminar y construir el nodo",
    fr: "Arrêter et construire le nœud",
  },
  "work.imitateDiscard": {
    en: "Discard lesson",
    zh: "放弃教学",
    es: "Descartar la lección",
    fr: "Abandonner la leçon",
  },
  "work.loadingActivity": {
    en: "Loading activity…",
    zh: "正在加载活动…",
    es: "Cargando actividad…",
    fr: "Chargement de l'activité…",
  },
  "work.noExecutions": {
    en: "No executions yet — runs appear here as the marketplace uses this node.",
    zh: "尚无执行——当市场使用此节点时，运行会显示在这里。",
    es: "Sin ejecuciones aún — las ejecuciones aparecen aquí cuando el mercado usa este nodo.",
    fr: "Aucune exécution — les exécutions apparaissent ici quand la place de marché utilise ce nœud.",
  },
  "work.yoursToAnswer": {
    en: "You are responsible for this node — every step above is yours to answer for.",
    zh: "你是此节点的负责人——上面的每一步都由你负责。",
    es: "Eres responsable de este nodo — cada paso de arriba responde ante ti.",
    fr: "Vous êtes responsable de ce nœud — chaque étape ci-dessus est de votre ressort.",
  },
  "work.nooneAnswers": {
    en: "No one answers for this node yet — it gets its responsible when the right person onboards with the node id.",
    zh: "此节点尚无人负责——当合适的人凭节点 id 接管时，它才有负责人。",
    es: "Nadie responde por este nodo todavía — tendrá responsable cuando la persona indicada se incorpore con el id del nodo.",
    fr: "Personne ne répond encore de ce nœud — il aura son responsable quand la bonne personne le reprendra avec l'id du nœud.",
  },
  "net.header": {
    en: "Network access",
    zh: "网络访问",
    es: "Acceso a la red",
    fr: "Accès réseau",
  },
  "net.none": {
    en: "No hosts granted — this node cannot reach the web at all until you name the exact hosts it may fetch from.",
    zh: "未授予任何主机——在你指明它可访问的具体主机之前，此节点完全无法访问网络。",
    es: "Sin hosts concedidos — este nodo no puede alcanzar la web en absoluto hasta que nombres los hosts exactos de los que puede leer.",
    fr: "Aucun hôte accordé — ce nœud ne peut pas du tout atteindre le web tant que vous ne nommez pas les hôtes exacts qu'il peut consulter.",
  },
  "net.withdraw": { en: "Withdraw", zh: "撤回", es: "Retirar", fr: "Retirer" },
  "net.grant": { en: "Grant host", zh: "授予主机", es: "Conceder host", fr: "Accorder l'hôte" },
  "net.hostLabel": {
    en: "Host to grant",
    zh: "要授予的主机",
    es: "Host a conceder",
    fr: "Hôte à accorder",
  },
  "net.openWeb": {
    en: "Web open — this organization is a verified entity under the global account, so its nodes are not limited to a host grant. It can still choose what to refuse below.",
    zh: "网络开放——该组织是全球账户下的已验证实体，其节点不受主机授权列表限制。它仍可在下方选择要拒绝的对象。",
    es: "Web abierta — esta organización es una entidad verificada bajo la cuenta global, así que sus nodos no están limitados a una lista de hosts. Aun así puede elegir qué rechazar abajo.",
    fr: "Web ouvert — cette organisation est une entité vérifiée sous le compte global, ses nœuds ne sont donc pas limités à une liste d'hôtes. Elle peut toujours choisir ce qu'elle refuse ci-dessous.",
  },
  "net.blockedHosts": {
    en: "Blocked hosts",
    zh: "已屏蔽的主机",
    es: "Hosts bloqueados",
    fr: "Hôtes bloqués",
  },
  "net.noBlockedHosts": {
    en: "No hosts blocked — the organization reaches the open web; name a host to refuse it (subdomains included), for every node under this Supernode.",
    zh: "未屏蔽任何主机——该组织可访问开放网络；填写主机名即可拒绝它（含子域名），对该超级节点下的所有节点生效。",
    es: "Ningún host bloqueado — la organización alcanza la web abierta; nombra un host para rechazarlo (subdominios incluidos), para cada nodo bajo este Supernodo.",
    fr: "Aucun hôte bloqué — l'organisation atteint le web ouvert ; nommez un hôte pour le refuser (sous-domaines compris), pour chaque nœud sous ce Supernœud.",
  },
  "net.blockHost": {
    en: "Block host",
    zh: "屏蔽主机",
    es: "Bloquear host",
    fr: "Bloquer l'hôte",
  },
  "net.blockHostLabel": {
    en: "Host to block",
    zh: "要屏蔽的主机",
    es: "Host a bloquear",
    fr: "Hôte à bloquer",
  },
  "net.blockedUsers": {
    en: "Blocked users",
    zh: "已屏蔽的用户",
    es: "Usuarios bloqueados",
    fr: "Utilisateurs bloqués",
  },
  "net.noBlockedUsers": {
    en: "No users blocked — like blocking a friend, a blocked user's messages reach neither this Supernode nor any node under it.",
    zh: "未屏蔽任何用户——如同屏蔽好友，被屏蔽用户的消息既到不了此超级节点，也到不了它之下的任何节点。",
    es: "Ningún usuario bloqueado — como bloquear a un amigo, los mensajes de un usuario bloqueado no llegan ni a este Supernodo ni a ningún nodo bajo él.",
    fr: "Aucun utilisateur bloqué — comme bloquer un ami, les messages d'un utilisateur bloqué n'atteignent ni ce Supernœud ni aucun nœud en dessous.",
  },
  "net.blockUser": {
    en: "Block user",
    zh: "屏蔽用户",
    es: "Bloquear usuario",
    fr: "Bloquer l'utilisateur",
  },
  "net.blockUserLabel": {
    en: "User to block",
    zh: "要屏蔽的用户",
    es: "Usuario a bloquear",
    fr: "Utilisateur à bloquer",
  },
  "net.unblock": {
    en: "Unblock",
    zh: "取消屏蔽",
    es: "Desbloquear",
    fr: "Débloquer",
  },
  "tpl.header": {
    en: "Org template",
    zh: "组织模板",
    es: "Plantilla de organización",
    fr: "Modèle d'organisation",
  },
  "tpl.button": {
    en: "Suggest structure",
    zh: "推荐组织结构",
    es: "Sugerir estructura",
    fr: "Suggérer une structure",
  },
  "tpl.hint": {
    en: "Reads this Supernode's description and resolves a lean working structure — deterministic first, recorded once, never re-reasoned.",
    zh: "读取此超级节点的描述并解析出精简的工作结构——优先确定性匹配，一次记录，绝不重复推理。",
    es: "Lee la descripción de este Supernodo y resuelve una estructura de trabajo esbelta — determinista primero, registrada una vez, nunca re-razonada.",
    fr: "Lit la description de ce Supernœud et résout une structure de travail sobre — déterministe d'abord, enregistrée une fois, jamais re-raisonnée.",
  },
  "tpl.source.recorded": {
    en: "recorded choice — resolved once, reused ever since",
    zh: "已记录的选择——只解析一次，此后一直复用",
    es: "elección registrada — resuelta una vez, reutilizada desde entonces",
    fr: "choix enregistré — résolu une fois, réutilisé depuis",
  },
  "tpl.source.matched": {
    en: "matched deterministically from the description",
    zh: "根据描述确定性匹配",
    es: "emparejada determinísticamente desde la descripción",
    fr: "appariée de façon déterministe depuis la description",
  },
  "tpl.source.model": {
    en: "evidence was thin — the model picked from the catalog",
    zh: "证据不足——模型从目录中选取",
    es: "la evidencia era escasa — el modelo eligió del catálogo",
    fr: "preuves minces — le modèle a choisi dans le catalogue",
  },
  "tpl.source.fallback": {
    en: "nothing matched — the lean generic structure",
    zh: "无匹配——使用精简通用结构",
    es: "nada coincidió — la estructura genérica esbelta",
    fr: "aucune correspondance — la structure générique sobre",
  },
  "tpl.import": {
    en: "Import {n} nodes",
    zh: "导入 {n} 个节点",
    es: "Importar {n} nodos",
    fr: "Importer {n} nœuds",
  },
  "tpl.allSeated": {
    en: "Every seat is already filled — nothing to import.",
    zh: "所有席位均已就位——无需导入。",
    es: "Todos los puestos ya están cubiertos — nada que importar.",
    fr: "Tous les sièges sont déjà occupés — rien à importer.",
  },
  "tpl.seated": {
    en: "already seated",
    zh: "已就位",
    es: "ya ocupado",
    fr: "déjà occupé",
  },
  "tpl.imported": {
    en: "Imported {n} nodes — each starts unclaimed: share a node's id only with the person who should onboard it.",
    zh: "已导入 {n} 个节点——均为未认领状态：仅将节点 id 分享给应接管它的人。",
    es: "Importados {n} nodos — cada uno empieza sin reclamar: comparte el id de un nodo solo con quien deba incorporarlo.",
    fr: "{n} nœuds importés — chacun commence non réclamé : ne partagez l'id d'un nœud qu'avec la personne qui doit le reprendre.",
  },
  "friends.sayHello": {
    en: "New friend — say hello!",
    zh: "新朋友——打个招呼吧！",
    es: "Nueva amistad — ¡saluda!",
    fr: "Nouvel ami — dites bonjour !",
  },
  "hold.from": { en: "from", zh: "来自", es: "de", fr: "de" },
  "hold.unknown": { en: "unknown", zh: "未知", es: "desconocido", fr: "inconnu" },
  "hold.allow": { en: "Allow", zh: "允许", es: "Permitir", fr: "Autoriser" },
  "hold.reject": { en: "Reject", zh: "拒绝", es: "Rechazar", fr: "Refuser" },
  "hold.sign": { en: "Sign & allow", zh: "签名并允许", es: "Firmar y permitir", fr: "Signer et autoriser" },
  "hold.signPh": {
    en: "type your name to sign",
    zh: "输入你的姓名以签名",
    es: "escribe tu nombre para firmar",
    fr: "tapez votre nom pour signer",
  },
  "hold.replyPh": {
    en: "type a reply to the requester",
    zh: "输入给请求者的回复",
    es: "escribe una respuesta al solicitante",
    fr: "tapez une réponse au demandeur",
  },
  "hold.sendReply": { en: "Send reply", zh: "发送回复", es: "Enviar respuesta", fr: "Envoyer la réponse" },
  "kyc.header": {
    en: "KYC — legal entity",
    zh: "KYC——法律实体",
    es: "KYC — entidad legal",
    fr: "KYC — entité légale",
  },
  "kyc.underReview": { en: "Under review", zh: "审核中", es: "En revisión", fr: "En cours d'examen" },
  "kyc.fastLane": {
    en: "fast lane — trusted company domain",
    zh: "快速通道——受信任的公司域名",
    es: "vía rápida — dominio de empresa de confianza",
    fr: "voie rapide — domaine d'entreprise de confiance",
  },
  "kyc.fastRow": {
    en: "fast lane — trusted domain",
    zh: "快速通道——受信任域名",
    es: "vía rápida — dominio de confianza",
    fr: "voie rapide — domaine de confiance",
  },
  "kyc.queue": { en: "standard queue", zh: "普通队列", es: "cola estándar", fr: "file standard" },
  "kyc.apply": { en: "Apply", zh: "申请", es: "Solicitar", fr: "Postuler" },
  "kyc.pitch": {
    en: "Obey the KYC policy to rank with global trust: verification rides on your paying plan, and a verified Supernode carries a trust multiplier for every node under it. Use a company mailbox — personal mailboxes are refused.",
    zh: "遵守 KYC 政策以获得全球信任排名：验证依托于你的付费套餐，通过验证的超级节点为其下每个节点带来信任倍数。请使用公司邮箱——个人邮箱会被拒绝。",
    es: "Cumple la política KYC para clasificar con confianza global: la verificación va con tu plan de pago, y un Supernodo verificado aporta un multiplicador de confianza a cada nodo bajo él. Usa un buzón de empresa — los personales se rechazan.",
    fr: "Respectez la politique KYC pour un classement avec confiance globale : la vérification s'appuie sur votre forfait payant, et un Supernœud vérifié porte un multiplicateur de confiance pour chaque nœud en dessous. Utilisez une boîte d'entreprise — les boîtes personnelles sont refusées.",
  },
  "kyc.rejectedTail": {
    en: ". You can apply again below.",
    zh: "。你可以在下方重新申请。",
    es: ". Puedes volver a solicitar abajo.",
    fr: ". Vous pouvez repostuler ci-dessous.",
  },
  "kyc.rejectedLead": {
    en: "The last application was rejected",
    zh: "上一次申请被拒绝",
    es: "La última solicitud fue rechazada",
    fr: "La dernière demande a été refusée",
  },
  "kyc.legalNamePh": {
    en: "legal entity name",
    zh: "法律实体名称",
    es: "nombre de la entidad legal",
    fr: "nom de l'entité légale",
  },
  "kyc.regNoPh": {
    en: "registration no. (optional)",
    zh: "注册号（可选）",
    es: "n.º de registro (opcional)",
    fr: "n° d'enregistrement (facultatif)",
  },
  "kyc.inbox": {
    en: "KYC reviews awaiting your verdict",
    zh: "等待你裁定的 KYC 审核",
    es: "Revisiones KYC a la espera de tu veredicto",
    fr: "Examens KYC en attente de votre verdict",
  },
  "kyc.approve": { en: "Approve", zh: "批准", es: "Aprobar", fr: "Approuver" },
  "kyc.verifiedBadge": {
    en: "KYC verified · global trust",
    zh: "KYC 已验证 · 全球信任",
    es: "KYC verificado · confianza global",
    fr: "KYC vérifié · confiance globale",
  },
  // ---- The node interact window and its reliability line.
  "interact.hint": {
    en: "Ask OoLu to act on this node — “pending” lists what waits, “sign <task id> as <your name>” passes a task to the next node, “reply <task id>: <message>”, or “build <what's missing>”.",
    zh: "让 OoLu 在此节点上行动——“pending”列出等待中的任务，“sign <任务 id> as <你的名字>”把任务传给下一个节点，“reply <任务 id>: <消息>”，或“build <缺失的东西>”。",
    es: "Pide a OoLu actuar en este nodo — “pending” lista lo que espera, “sign <id de tarea> as <tu nombre>” pasa una tarea al siguiente nodo, “reply <id de tarea>: <mensaje>”, o “build <lo que falta>”.",
    fr: "Demandez à OoLu d'agir sur ce nœud — « pending » liste ce qui attend, « sign <id de tâche> as <votre nom> » passe une tâche au nœud suivant, « reply <id de tâche> : <message> », ou « build <ce qui manque> ».",
  },
  "interact.reliabilityNone": {
    en: "Automation reliability: no verified runs yet — it grows with every task this node executes.",
    zh: "自动化可靠度：尚无经验证的运行——它随此节点执行的每个任务而增长。",
    es: "Fiabilidad de automatización: sin ejecuciones verificadas aún — crece con cada tarea que este nodo ejecuta.",
    fr: "Fiabilité d'automatisation : aucune exécution vérifiée pour l'instant — elle croît avec chaque tâche que ce nœud exécute.",
  },
  "interact.reliability": {
    en: "Automation reliability: {pct}% over {n} verified {runs} — every verified run takes this node closer to hands-off.",
    zh: "自动化可靠度：{n} 次经验证运行中达 {pct}%——每次验证运行都让此节点更接近全自动。",
    es: "Fiabilidad de automatización: {pct}% en {n} {runs} verificadas — cada ejecución verificada acerca este nodo al manos-libres.",
    fr: "Fiabilité d'automatisation : {pct} % sur {n} {runs} vérifiées — chaque exécution vérifiée rapproche ce nœud du sans-intervention.",
  },
  "interact.runOne": { en: "run", zh: "次", es: "ejecución", fr: "exécution" },
  "interact.runMany": { en: "runs", zh: "次", es: "ejecuciones", fr: "exécutions" },
  "interact.messageAbout": {
    en: "Message OoLu about {name}…",
    zh: "就 {name} 给 OoLu 发消息…",
    es: "Mensaje a OoLu sobre {name}…",
    fr: "Message à OoLu au sujet de {name}…",
  },
  "interact.thinking": {
    en: "Working on it — the reply lands when it's ready.",
    zh: "处理中——准备好就回复。",
    es: "Trabajando en ello — la respuesta llega cuando esté lista.",
    fr: "En cours — la réponse arrive dès qu'elle est prête.",
  },
  // ---- Files chrome: the drawer, tiles, and the open file.
  "files.yours": { en: "Your files", zh: "你的文件", es: "Tus archivos", fr: "Vos fichiers" },
  "files.nodes": {
    en: "This node's files",
    zh: "此节点的文件",
    es: "Archivos de este nodo",
    fr: "Fichiers de ce nœud",
  },
  "files.select": { en: "Select", zh: "选择", es: "Seleccionar", fr: "Sélectionner" },
  "files.done": { en: "Done", zh: "完成", es: "Listo", fr: "Terminé" },
  "files.add": { en: "Add", zh: "添加", es: "Añadir", fr: "Ajouter" },
  "files.addTitle": {
    en: "Upload from this device, or make a folder",
    zh: "从此设备上传，或新建文件夹",
    es: "Sube desde este dispositivo o crea una carpeta",
    fr: "Téléversez depuis cet appareil ou créez un dossier",
  },
  "files.upload": {
    en: "Upload from device",
    zh: "从设备上传",
    es: "Subir desde el dispositivo",
    fr: "Téléverser depuis l'appareil",
  },
  "files.newFolder": { en: "New folder", zh: "新建文件夹", es: "Nueva carpeta", fr: "Nouveau dossier" },
  "files.folderNamePh": {
    en: "folder name",
    zh: "文件夹名称",
    es: "nombre de la carpeta",
    fr: "nom du dossier",
  },
  "files.folderName": {
    en: "Folder name",
    zh: "文件夹名称",
    es: "Nombre de la carpeta",
    fr: "Nom du dossier",
  },
  "files.create": { en: "Create", zh: "创建", es: "Crear", fr: "Créer" },
  "files.selectedCount": {
    en: "{n} selected",
    zh: "已选择 {n} 项",
    es: "{n} seleccionados",
    fr: "{n} sélectionné(s)",
  },
  "files.forward": { en: "Forward…", zh: "转发…", es: "Reenviar…", fr: "Transférer…" },
  "files.deleteEllipsis": { en: "Delete…", zh: "删除…", es: "Eliminar…", fr: "Supprimer…" },
  "files.reallyDelete": {
    en: "Really delete {n}?",
    zh: "确定删除 {n} 项？",
    es: "¿Eliminar {n} de verdad?",
    fr: "Vraiment supprimer {n} ?",
  },
  "files.emptyNode": {
    en: "Nothing here yet — this node keeps its files to itself.",
    zh: "这里还没有内容——此节点的文件只归它自己。",
    es: "Nada aquí todavía — este nodo se guarda sus archivos.",
    fr: "Rien ici pour l'instant — ce nœud garde ses fichiers pour lui.",
  },
  "files.emptyLife": {
    en: "No files yet — ask OoLu to write something down, or press + to bring one in from this device.",
    zh: "还没有文件——让 OoLu 写点什么，或按 + 从此设备导入一个。",
    es: "Sin archivos aún — pide a OoLu que escriba algo, o pulsa + para traer uno de este dispositivo.",
    fr: "Pas encore de fichier — demandez à OoLu d'écrire quelque chose, ou appuyez sur + pour en importer un depuis cet appareil.",
  },
  "files.upOne": {
    en: "up one level",
    zh: "上一级",
    es: "subir un nivel",
    fr: "remonter d'un niveau",
  },
  "files.folderSub": {
    en: "folder · drop files to move",
    zh: "文件夹 · 拖放文件以移动",
    es: "carpeta · suelta archivos para mover",
    fr: "dossier · déposez des fichiers pour déplacer",
  },
  "files.emptyFolder": {
    en: "Empty folder — drag a file in, or ask OoLu to write one here.",
    zh: "空文件夹——拖入文件，或让 OoLu 在这里写一个。",
    es: "Carpeta vacía — arrastra un archivo o pide a OoLu que escriba uno aquí.",
    fr: "Dossier vide — glissez un fichier ou demandez à OoLu d'en écrire un ici.",
  },
  "file.opening": { en: "Opening…", zh: "正在打开…", es: "Abriendo…", fr: "Ouverture…" },
  "file.fetching": {
    en: "Fetching the file…",
    zh: "正在获取文件…",
    es: "Obteniendo el archivo…",
    fr: "Récupération du fichier…",
  },
  "file.backToFiles": { en: "← files", zh: "← 文件", es: "← archivos", fr: "← fichiers" },
  "file.download": { en: "download", zh: "下载", es: "descargar", fr: "télécharger" },
  "file.deleteAction": { en: "delete", zh: "删除", es: "eliminar", fr: "supprimer" },
  "file.forwardAction": { en: "forward", zh: "转发", es: "reenviar", fr: "transférer" },
  "file.saveTitle": {
    en: "save this file to the device — true bytes, true type",
    zh: "把此文件保存到设备——原始字节、真实类型",
    es: "guarda este archivo en el dispositivo — bytes reales, tipo real",
    fr: "enregistrer ce fichier sur l'appareil — vrais octets, vrai type",
  },
  "file.edit": { en: "Edit", zh: "编辑", es: "Editar", fr: "Modifier" },
  "file.emptyDoc": {
    en: "This document is empty.",
    zh: "这个文档是空的。",
    es: "Este documento está vacío.",
    fr: "Ce document est vide.",
  },
  "file.downloadDevice": {
    en: "Download to this device",
    zh: "下载到此设备",
    es: "Descargar a este dispositivo",
    fr: "Télécharger sur cet appareil",
  },
  "file.lifeDrawer": {
    en: "Your files (Life)",
    zh: "你的文件（生活）",
    es: "Tus archivos (Vida)",
    fr: "Vos fichiers (Vie)",
  },
  "file.copiedTo": {
    en: "copied to {name}",
    zh: "已复制到 {name}",
    es: "copiado a {name}",
    fr: "copié vers {name}",
  },
  // Units shown beside number inputs.
  "unit.days": { en: "days", zh: "天", es: "días", fr: "jours" },
  "unit.currency": {
    en: "in your currency",
    zh: "以你的货币计",
    es: "en tu moneda",
    fr: "dans votre devise",
  },

  // ---- layout: the foldable list and the phone's one-pane flow ---------
  "nav.back": { en: "Back", zh: "返回", es: "Atrás", fr: "Retour" },
  "nav.hideList": {
    en: "Hide the list",
    zh: "收起列表",
    es: "Ocultar la lista",
    fr: "Masquer la liste",
  },
  "nav.showList": {
    en: "Show the list",
    zh: "展开列表",
    es: "Mostrar la lista",
    fr: "Afficher la liste",
  },

  // ---- sign-in screen -------------------------------------------------
  "login.edgeIntro": {
    en: "Edge keeps everything on your side: this device, or a private server on your own network.",
    zh: "Edge 让一切留在你这边：这台设备，或你自己网络中的私有服务器。",
    es: "Edge lo mantiene todo de tu lado: este dispositivo o un servidor privado en tu propia red.",
    fr: "Edge garde tout de votre côté : cet appareil, ou un serveur privé sur votre propre réseau.",
  },
  "login.thisDevice": {
    en: "This device",
    zh: "这台设备",
    es: "Este dispositivo",
    fr: "Cet appareil",
  },
  "login.privateNetwork": {
    en: "Private network",
    zh: "私有网络",
    es: "Red privada",
    fr: "Réseau privé",
  },
  "login.deviceIntro": {
    en: "Your account, your engine, and everything you teach OoLu stay on this machine.",
    zh: "你的账户、你的引擎，以及你教给 OoLu 的一切都留在这台机器上。",
    es: "Tu cuenta, tu motor y todo lo que enseñas a OoLu se quedan en esta máquina.",
    fr: "Votre compte, votre moteur et tout ce que vous apprenez à OoLu restent sur cette machine.",
  },
  "login.continueEdge": {
    en: "Continue on Edge",
    zh: "在 Edge 上继续",
    es: "Continuar en Edge",
    fr: "Continuer sur Edge",
  },
  "login.networkIntro": {
    en: "A private server your group runs on its own network (a static address everyone can reach). You still sign in with a username and password — onboarding a node created under a Supernode has to name an actual person.",
    zh: "你的团队在自己网络中运行的私有服务器（一个大家都能访问的固定地址）。你仍需用用户名和密码登录——在超级节点下创建的节点必须对应一个真实的人。",
    es: "Un servidor privado que tu grupo ejecuta en su propia red (una dirección fija que todos pueden alcanzar). Sigues iniciando sesión con usuario y contraseña: un nodo creado bajo un Supernodo debe nombrar a una persona real.",
    fr: "Un serveur privé que votre groupe exécute sur son propre réseau (une adresse fixe accessible à tous). Vous vous connectez toujours avec un identifiant et un mot de passe — un nœud créé sous un Supernœud doit nommer une personne réelle.",
  },
  "login.serverAddress": {
    en: "Private server address",
    zh: "私有服务器地址",
    es: "Dirección del servidor privado",
    fr: "Adresse du serveur privé",
  },
  "login.enterServer": {
    en: "enter your private server's address",
    zh: "请输入你的私有服务器地址",
    es: "introduce la dirección de tu servidor privado",
    fr: "saisissez l'adresse de votre serveur privé",
  },
  "login.checkInbox": {
    en: "Check your inbox — enter the 6-digit code to finish.",
    zh: "查看你的邮箱——输入 6 位验证码完成。",
    es: "Revisa tu correo: introduce el código de 6 dígitos para terminar.",
    fr: "Consultez votre boîte mail — saisissez le code à 6 chiffres pour terminer.",
  },
  "login.resetEnterCode": {
    en: "Enter the e-mailed code and pick a new password.",
    zh: "输入邮件中的验证码并设置新密码。",
    es: "Introduce el código enviado por correo y elige una nueva contraseña.",
    fr: "Saisissez le code reçu par e-mail et choisissez un nouveau mot de passe.",
  },
  "login.resetEnterEmail": {
    en: "Enter your e-mail and we'll send a reset code.",
    zh: "输入你的邮箱，我们将发送重置验证码。",
    es: "Introduce tu correo y te enviaremos un código de restablecimiento.",
    fr: "Saisissez votre e-mail et nous vous enverrons un code de réinitialisation.",
  },
  "login.signInEdge": {
    en: "Sign in to your private network server.",
    zh: "登录你的私有网络服务器。",
    es: "Inicia sesión en tu servidor de red privada.",
    fr: "Connectez-vous à votre serveur de réseau privé.",
  },
  "login.registerEdge": {
    en: "Create your account on the private network server.",
    zh: "在私有网络服务器上创建你的账户。",
    es: "Crea tu cuenta en el servidor de red privada.",
    fr: "Créez votre compte sur le serveur de réseau privé.",
  },
  "login.signInGlobal": {
    en: "Sign in to OoLu Global.",
    zh: "登录 OoLu Global。",
    es: "Inicia sesión en OoLu Global.",
    fr: "Connectez-vous à OoLu Global.",
  },
  "login.registerGlobal": {
    en: "Create your OoLu Global account.",
    zh: "创建你的 OoLu Global 账户。",
    es: "Crea tu cuenta de OoLu Global.",
    fr: "Créez votre compte OoLu Global.",
  },
  "login.username": {
    en: "Username",
    zh: "用户名",
    es: "Nombre de usuario",
    fr: "Identifiant",
  },
  "login.email": { en: "E-mail", zh: "邮箱", es: "Correo", fr: "E-mail" },
  "login.code": {
    en: "6-digit code",
    zh: "6 位验证码",
    es: "Código de 6 dígitos",
    fr: "Code à 6 chiffres",
  },
  "login.password": {
    en: "Password",
    zh: "密码",
    es: "Contraseña",
    fr: "Mot de passe",
  },
  "login.newPassword": {
    en: "New password",
    zh: "新密码",
    es: "Nueva contraseña",
    fr: "Nouveau mot de passe",
  },
  "login.signIn": {
    en: "Sign in",
    zh: "登录",
    es: "Iniciar sesión",
    fr: "Se connecter",
  },
  "login.signingIn": {
    en: "Signing in…",
    zh: "登录中…",
    es: "Iniciando sesión…",
    fr: "Connexion…",
  },
  "login.createAccount": {
    en: "Create account",
    zh: "创建账户",
    es: "Crear cuenta",
    fr: "Créer un compte",
  },
  "login.creatingAccount": {
    en: "Creating account…",
    zh: "创建账户中…",
    es: "Creando cuenta…",
    fr: "Création du compte…",
  },
  "login.verify": { en: "Verify", zh: "验证", es: "Verificar", fr: "Vérifier" },
  "login.verifying": {
    en: "Verifying…",
    zh: "验证中…",
    es: "Verificando…",
    fr: "Vérification…",
  },
  "login.changePassword": {
    en: "Change password",
    zh: "修改密码",
    es: "Cambiar contraseña",
    fr: "Changer le mot de passe",
  },
  "login.changingPassword": {
    en: "Changing password…",
    zh: "修改密码中…",
    es: "Cambiando contraseña…",
    fr: "Changement du mot de passe…",
  },
  "login.sendPhoneCode": {
    en: "Send code",
    zh: "发送验证码",
    es: "Enviar código",
    fr: "Envoyer le code",
  },
  "login.sendCode": {
    en: "Send reset code",
    zh: "发送重置验证码",
    es: "Enviar código",
    fr: "Envoyer le code",
  },
  "login.sendingCode": {
    en: "Sending code…",
    zh: "发送验证码中…",
    es: "Enviando código…",
    fr: "Envoi du code…",
  },
  "login.emailNewPassword": {
    en: "Or e-mail me a new password",
    zh: "或者给我发送一个新密码",
    es: "O enviarme una nueva contraseña por correo",
    fr: "Ou envoyez-moi un nouveau mot de passe par e-mail",
  },
  "login.sendingNewPassword": {
    en: "Sending a new password…",
    zh: "正在发送新密码…",
    es: "Enviando una nueva contraseña…",
    fr: "Envoi d'un nouveau mot de passe…",
  },
  "login.newPasswordSent": {
    en: "If that address has an account, a new password is on its way — check your inbox, then change it in Settings.",
    zh: "如果该邮箱有账户，新密码已发出——请查收邮件，然后在设置中修改。",
    es: "Si esa dirección tiene una cuenta, una nueva contraseña está en camino — revisa tu bandeja y cámbiala en Ajustes.",
    fr: "Si cette adresse a un compte, un nouveau mot de passe arrive — vérifiez votre boîte, puis changez-le dans les Réglages.",
  },
  "login.google": {
    en: "Continue with Google",
    zh: "使用 Google 继续",
    es: "Continuar con Google",
    fr: "Continuer avec Google",
  },
  "login.phone": {
    en: "Continue with phone",
    zh: "使用手机号继续",
    es: "Continuar con teléfono",
    fr: "Continuer avec le téléphone",
  },
  "login.phoneIntro": {
    en: "Enter your phone number — we'll text you a sign-in code. New numbers get an account created on the spot.",
    zh: "输入手机号——我们会发送登录验证码。新号码将当场创建账户。",
    es: "Escribe tu número — te enviaremos un código por SMS. Los números nuevos obtienen cuenta al instante.",
    fr: "Entrez votre numéro — nous vous enverrons un code par SMS. Un nouveau numéro obtient un compte sur-le-champ.",
  },
  "login.phoneNumber": {
    en: "Phone number",
    zh: "手机号",
    es: "Número de teléfono",
    fr: "Numéro de téléphone",
  },
  "login.phoneCodeSent": {
    en: "Code sent — check your texts.",
    zh: "验证码已发送——请查看短信。",
    es: "Código enviado — revisa tus SMS.",
    fr: "Code envoyé — regardez vos SMS.",
  },
  "login.phoneEnterCode": {
    en: "Enter the code we texted you.",
    zh: "输入我们发送的验证码。",
    es: "Escribe el código que te enviamos.",
    fr: "Saisissez le code reçu par SMS.",
  },
  "login.phoneCreated": {
    en: "Account created! A password was texted to you — or choose your own now.",
    zh: "账户已创建！密码已通过短信发送——你也可以现在自行设置。",
    es: "¡Cuenta creada! Te enviamos una contraseña por SMS — o elige la tuya ahora.",
    fr: "Compte créé ! Un mot de passe vous a été envoyé par SMS — ou choisissez le vôtre maintenant.",
  },
  "login.phoneChoosePassword": {
    en: "Choose your password (the texted one works too).",
    zh: "设置你的密码（短信中的密码同样有效）。",
    es: "Elige tu contraseña (la del SMS también sirve).",
    fr: "Choisissez votre mot de passe (celui du SMS marche aussi).",
  },
  "login.savePassword": {
    en: "Save password",
    zh: "保存密码",
    es: "Guardar contraseña",
    fr: "Enregistrer le mot de passe",
  },
  "login.keepTexted": {
    en: "Keep the texted password",
    zh: "保留短信中的密码",
    es: "Conservar la contraseña del SMS",
    fr: "Garder le mot de passe du SMS",
  },
  "login.backToSignIn": {
    en: "Back to sign-in",
    zh: "返回登录",
    es: "Volver a iniciar sesión",
    fr: "Retour à la connexion",
  },
  "friends.message": {
    en: "Message",
    zh: "发消息",
    es: "Mensaje",
    fr: "Message",
  },
  "login.noAccount": {
    en: "No account?",
    zh: "还没有账户？",
    es: "¿Sin cuenta?",
    fr: "Pas de compte ?",
  },
  "login.createOne": {
    en: "Create one",
    zh: "创建一个",
    es: "Crea una",
    fr: "Créez-en un",
  },
  "login.forgot": {
    en: "Forgot password?",
    zh: "忘记密码？",
    es: "¿Olvidaste la contraseña?",
    fr: "Mot de passe oublié ?",
  },
  "login.wrongAddress": {
    en: "Wrong address?",
    zh: "地址填错了？",
    es: "¿Dirección equivocada?",
    fr: "Mauvaise adresse ?",
  },
  "login.startOver": {
    en: "Start over",
    zh: "重新开始",
    es: "Empezar de nuevo",
    fr: "Recommencer",
  },
  "login.haveAccount": {
    en: "Have an account?",
    zh: "已有账户？",
    es: "¿Tienes cuenta?",
    fr: "Vous avez un compte ?",
  },
  "login.googleFailed": {
    en: "Google sign-in failed",
    zh: "Google 登录失败",
    es: "Falló el inicio de sesión con Google",
    fr: "Échec de la connexion Google",
  },
  "login.signInFailed": {
    en: "sign-in failed",
    zh: "登录失败",
    es: "falló el inicio de sesión",
    fr: "échec de la connexion",
  },
  "login.registerFailed": {
    en: "registration failed",
    zh: "注册失败",
    es: "falló el registro",
    fr: "échec de l'inscription",
  },
  "login.codeSent": {
    en: "We sent a 6-digit code to {mail} — enter it here to finish.",
    zh: "我们已向 {mail} 发送了 6 位验证码——在此输入以完成。",
    es: "Enviamos un código de 6 dígitos a {mail}: introdúcelo aquí para terminar.",
    fr: "Nous avons envoyé un code à 6 chiffres à {mail} — saisissez-le ici pour terminer.",
  },
  "login.resetSent": {
    en: "If {mail} has an account, a 6-digit code is on its way.",
    zh: "如果 {mail} 有账户，6 位验证码已在路上。",
    es: "Si {mail} tiene una cuenta, un código de 6 dígitos está en camino.",
    fr: "Si {mail} possède un compte, un code à 6 chiffres est en route.",
  },
  "login.passwordChanged": {
    en: "Password changed — sign in with the new one.",
    zh: "密码已修改——请用新密码登录。",
    es: "Contraseña cambiada: inicia sesión con la nueva.",
    fr: "Mot de passe changé — connectez-vous avec le nouveau.",
  },

  // ---- model keys (Settings) ------------------------------------------
  "keys.none": {
    en: "No model key yet — OoLu answers with its built-in rules. Paste an Anthropic or OpenAI API key to give it a real mind. The key is encrypted on this machine and never shown again; only the fingerprint below proves it's in.",
    zh: "还没有模型密钥——OoLu 目前用内置规则回答。粘贴 Anthropic 或 OpenAI 的 API 密钥，给它一个真正的大脑。密钥在本机加密保存且不再显示；只有下方的指纹证明它已录入。",
    es: "Aún no hay clave de modelo: OoLu responde con sus reglas integradas. Pega una clave de API de Anthropic u OpenAI para darle una mente real. La clave se cifra en esta máquina y no se vuelve a mostrar; solo la huella de abajo prueba que está dentro.",
    fr: "Pas encore de clé de modèle — OoLu répond avec ses règles intégrées. Collez une clé d'API Anthropic ou OpenAI pour lui donner un vrai esprit. La clé est chiffrée sur cette machine et jamais réaffichée ; seule l'empreinte ci-dessous prouve qu'elle est là.",
  },
  "keys.providerKey": {
    en: "{provider} key",
    zh: "{provider} 密钥",
    es: "Clave de {provider}",
    fr: "Clé {provider}",
  },
  "keys.fingerprint": {
    en: "fingerprint {mark}",
    zh: "指纹 {mark}",
    es: "huella {mark}",
    fr: "empreinte {mark}",
  },
  "keys.remove": { en: "remove", zh: "移除", es: "quitar", fr: "retirer" },
  "keys.add": {
    en: "Add a model key",
    zh: "添加模型密钥",
    es: "Añadir una clave de modelo",
    fr: "Ajouter une clé de modèle",
  },
  "keys.addDesc": {
    en: "Stored encrypted on this machine only — it never syncs, never appears in settings, and never comes back out.",
    zh: "仅在本机加密存储——不会同步、不会出现在设置中，也永远不会被读出。",
    es: "Se guarda cifrada solo en esta máquina: nunca se sincroniza, nunca aparece en los ajustes y nunca vuelve a salir.",
    fr: "Stockée chiffrée sur cette machine uniquement — jamais synchronisée, jamais visible dans les réglages, jamais restituée.",
  },
  "keys.paste": {
    en: "paste key",
    zh: "粘贴密钥",
    es: "pegar clave",
    fr: "coller la clé",
  },
  "keys.addButton": { en: "Add", zh: "添加", es: "Añadir", fr: "Ajouter" },
  "keys.working": {
    en: "✓ working — the model answered ({source}).",
    zh: "✓ 正常——模型已应答（{source}）。",
    es: "✓ funciona: el modelo respondió ({source}).",
    fr: "✓ opérationnelle — le modèle a répondu ({source}).",
  },
  "keys.notWorking": {
    en: "✗ {error}",
    zh: "✗ {error}",
    es: "✗ {error}",
    fr: "✗ {error}",
  },
  "keys.nowDefault": {
    en: "Your {provider} key is now the default model.",
    zh: "你的 {provider} 密钥现在是默认模型。",
    es: "Tu clave de {provider} es ahora el modelo predeterminado.",
    fr: "Votre clé {provider} est désormais le modèle par défaut.",
  },
  "keys.test": {
    en: "Test the model",
    zh: "测试模型",
    es: "Probar el modelo",
    fr: "Tester le modèle",
  },
  "keys.testDesc": {
    en: "Make one real call and confirm the model answers — the sure way to tell a working key from a silent one.",
    zh: "发起一次真实调用并确认模型应答——分辨密钥是否真正可用的可靠办法。",
    es: "Haz una llamada real y confirma que el modelo responde: la forma segura de distinguir una clave que funciona de una silenciosa.",
    fr: "Faites un appel réel et confirmez que le modèle répond — le moyen sûr de distinguer une clé qui marche d'une clé muette.",
  },
  "keys.testButton": {
    en: "Test connection",
    zh: "测试连接",
    es: "Probar conexión",
    fr: "Tester la connexion",
  },
  "keys.testing": {
    en: "Testing…",
    zh: "测试中…",
    es: "Probando…",
    fr: "Test en cours…",
  },

  // ---- payment methods (Settings) --------------------------------------
  "pay.title": {
    en: "Payment methods",
    zh: "支付方式",
    es: "Métodos de pago",
    fr: "Moyens de paiement",
  },
  "pay.testBanner": {
    en: "Pre-launch test mode — the real transaction port is closed. Cards here are named test cards; no money can move.",
    zh: "上线前测试模式——真实交易通道处于关闭状态。这里的卡都是命名的测试卡，不会有资金流动。",
    es: "Modo de prueba previo al lanzamiento: el puerto de transacciones reales está cerrado. Las tarjetas aquí son de prueba; no se mueve dinero.",
    fr: "Mode test pré-lancement — le port de transactions réelles est fermé. Les cartes ici sont des cartes de test ; aucun argent ne circule.",
  },
  "pay.chargingWhen": {
    en: "Charging opens when: {reasons}.",
    zh: "满足以下条件后开启扣款：{reasons}。",
    es: "El cobro se abre cuando: {reasons}.",
    fr: "La facturation s'ouvre quand : {reasons}.",
  },
  "pay.noCards": {
    en: "No saved cards yet.",
    zh: "尚无已保存的卡。",
    es: "Aún no hay tarjetas guardadas.",
    fr: "Aucune carte enregistrée pour l'instant.",
  },
  "pay.expires": {
    en: "expires {m}/{y}",
    zh: "有效期至 {m}/{y}",
    es: "caduca {m}/{y}",
    fr: "expire {m}/{y}",
  },
  "pay.makeDefault": {
    en: "make default",
    zh: "设为默认",
    es: "hacer predeterminada",
    fr: "définir par défaut",
  },
  "pay.remove": { en: "remove", zh: "移除", es: "quitar", fr: "retirer" },
  "pay.default": {
    en: "default",
    zh: "默认",
    es: "predeterminada",
    fr: "par défaut",
  },
  "pay.addTestCard": {
    en: "Add a test card",
    zh: "添加测试卡",
    es: "Añadir una tarjeta de prueba",
    fr: "Ajouter une carte de test",
  },
  "pay.addButton": { en: "Add", zh: "添加", es: "Añadir", fr: "Ajouter" },

  // ---- the representative (Settings + threads + inbox) -----------------
  handiwork: {
    en: "Handiwork",
    zh: "手工坊",
    es: "Artesanía",
    fr: "Artisanat",
  },
  "rep.title": {
    en: "Representative",
    zh: "个人代表",
    es: "Representante",
    fr: "Représentant",
  },
  "rep.intro": {
    en: "OoLu can draft replies in your voice, from how you actually write. Drafts wait for your word; auto only ever sends routine, grounded replies — commitments always come back to you — and switches on only after your approvals earn it.",
    zh: "OoLu 可以按你真实的写作方式，用你的语气起草回复。草稿会等待你的决定；自动模式只发送常规且有依据的回复——涉及承诺的内容永远交回给你——并且只有在你的批准记录足够好之后才会生效。",
    es: "OoLu puede redactar respuestas con tu voz, a partir de cómo escribes realmente. Los borradores esperan tu decisión; el modo auto solo envía respuestas rutinarias y fundamentadas —los compromisos siempre vuelven a ti— y se activa solo cuando tus aprobaciones lo merecen.",
    fr: "OoLu peut rédiger des réponses avec votre voix, à partir de votre vraie façon d'écrire. Les brouillons attendent votre décision ; l'auto n'envoie que des réponses routinières et fondées — les engagements vous reviennent toujours — et ne s'active qu'une fois mérité par vos approbations.",
  },
  "rep.mode": { en: "Mode", zh: "模式", es: "Modo", fr: "Mode" },
  "rep.modeDesc": {
    en: "Off, draft suggestions, or earned auto-replies.",
    zh: "关闭、草稿建议，或需先赢得信任的自动回复。",
    es: "Apagado, sugerencias en borrador o auto-respuestas ganadas.",
    fr: "Désactivé, suggestions en brouillon, ou réponses auto méritées.",
  },
  "rep.modeUnearned": {
    en: "Auto is on but not yet earned — it drafts until your record qualifies.",
    zh: "自动模式已开启但尚未赢得信任——在记录达标前只会起草。",
    es: "Auto está activado pero aún no ganado: redacta borradores hasta que tu historial califique.",
    fr: "L'auto est activé mais pas encore mérité — il rédige des brouillons jusqu'à ce que votre historique qualifie.",
  },
  "rep.modeOff": { en: "off", zh: "关闭", es: "apagado", fr: "désactivé" },
  "rep.modeDraft": { en: "draft", zh: "草稿", es: "borrador", fr: "brouillon" },
  "rep.modeAuto": { en: "auto", zh: "自动", es: "auto", fr: "auto" },
  "rep.aboutYou": {
    en: "About you",
    zh: "关于你",
    es: "Sobre ti",
    fr: "À propos de vous",
  },
  "rep.aboutDesc": {
    en: "A short standing note the drafts lean on (role, tone, facts).",
    zh: "草稿所依据的简短常备说明（角色、语气、事实）。",
    es: "Una nota breve y permanente en la que se apoyan los borradores (rol, tono, hechos).",
    fr: "Une courte note permanente sur laquelle s'appuient les brouillons (rôle, ton, faits).",
  },
  "rep.aboutPlaceholder": {
    en: "e.g. engineer; keeps replies short",
    zh: "例如：工程师；回复简短",
    es: "p. ej., ingeniera; respuestas cortas",
    fr: "p. ex. ingénieur ; réponses courtes",
  },
  "rep.save": { en: "Save", zh: "保存", es: "Guardar", fr: "Enregistrer" },
  "rep.stats": {
    en: "{exchanges} exchanges learned · {pending} draft(s) waiting · {verdict} · voice: {adapter}",
    zh: "已学习 {exchanges} 段对话 · {pending} 份草稿待定 · {verdict} · 声音：{adapter}",
    es: "{exchanges} intercambios aprendidos · {pending} borrador(es) en espera · {verdict} · voz: {adapter}",
    fr: "{exchanges} échanges appris · {pending} brouillon(s) en attente · {verdict} · voix : {adapter}",
  },
  "rep.noVerdicts": {
    en: "no verdicts yet",
    zh: "尚无裁决",
    es: "aún sin veredictos",
    fr: "aucun verdict pour l'instant",
  },
  "rep.sentAsWritten": {
    en: "{pct}% sent as written",
    zh: "{pct}% 原样发送",
    es: "{pct}% enviados tal cual",
    fr: "{pct}% envoyés tels quels",
  },
  "rep.autoSentCount": {
    en: "{n} auto-sent",
    zh: "自动发送 {n} 条",
    es: "{n} auto-enviados",
    fr: "{n} envoyés auto",
  },
  "rep.drafts": { en: "Drafts", zh: "草稿", es: "Borradores", fr: "Brouillons" },
  "rep.draftsNew": {
    en: "Drafts · {n} new",
    zh: "草稿 · {n} 条新",
    es: "Borradores · {n} nuevos",
    fr: "Brouillons · {n} nouveaux",
  },
  "rep.draftsSub": {
    en: "replies in your voice, awaiting you",
    zh: "以你的语气写好的回复，等你定夺",
    es: "respuestas con tu voz, esperándote",
    fr: "des réponses avec votre voix, qui vous attendent",
  },
  "rep.inboxTitle": {
    en: "Drafts awaiting your word",
    zh: "等待你决定的草稿",
    es: "Borradores a la espera de tu palabra",
    fr: "Brouillons en attente de votre décision",
  },
  "rep.nothingWaiting": {
    en: "Nothing waiting.",
    zh: "暂无待处理。",
    es: "Nada en espera.",
    fr: "Rien en attente.",
  },
  "rep.inboxIntro": {
    en: "When your representative drafts a reply — from ✍ in a thread, or on its own in auto mode — it lands here for your decision.",
    zh: "当你的代表起草回复时——无论是在会话中点 ✍，还是自动模式下自行起草——都会送到这里等你决定。",
    es: "Cuando tu representante redacta una respuesta —desde ✍ en un hilo o por sí mismo en modo auto— llega aquí para tu decisión.",
    fr: "Quand votre représentant rédige une réponse — via ✍ dans un fil, ou de lui-même en mode auto — elle arrive ici pour votre décision.",
  },
  "rep.answering": {
    en: "To {peer}, answering: “{text}”",
    zh: "回复 {peer}，所答内容：“{text}”",
    es: "Para {peer}, respondiendo a: «{text}»",
    fr: "À {peer}, en réponse à : « {text} »",
  },
  "rep.drafted": {
    en: "Your representative drafted:",
    zh: "你的代表已起草：",
    es: "Tu representante redactó:",
    fr: "Votre représentant a rédigé :",
  },
  "rep.send": { en: "Send", zh: "发送", es: "Enviar", fr: "Envoyer" },
  "rep.edit": { en: "Edit", zh: "编辑", es: "Editar", fr: "Modifier" },
  "rep.discard": { en: "Discard", zh: "丢弃", es: "Descartar", fr: "Abandonner" },
  "rep.ignore": { en: "Ignore", zh: "忽略", es: "Ignorar", fr: "Ignorer" },
  "rep.discarded": {
    en: "Kept in your typing box with {peer} — I'll offer a fresh draft if they write again, when you toggle me back on, or if it's still unread tomorrow.",
    zh: "已存入你与 {peer} 的输入框——若对方再来消息、你重新开启代表模式，或明天仍未读，我会再拟一稿。",
    es: "Guardado en tu cuadro de escritura con {peer} — ofreceré un borrador nuevo si vuelven a escribir, cuando me reactives, o si sigue sin leer mañana.",
    fr: "Conservé dans votre zone de saisie avec {peer} — je proposerai un nouveau brouillon s'ils réécrivent, quand vous me réactiverez, ou s'il reste non lu demain.",
  },
  "rep.waitingTitle": {
    en: "Waiting on you",
    zh: "等待你的信息",
    es: "Esperándote",
    fr: "En attente de vous",
  },
  "rep.waitingCard": {
    en: "To reply to {peer} — “{text}” — I need:",
    zh: "要回复 {peer}——“{text}”——我需要：",
    es: "Para responder a {peer} — “{text}” — necesito:",
    fr: "Pour répondre à {peer} — « {text} » — j'ai besoin de :",
  },
  "rep.waitingHint": {
    en: "Answer me in the OoLu chat and I'll draft it — or press Ignore to mark it read with no reply.",
    zh: "在 OoLu 对话中回答我，我就会拟稿——或按“忽略”将其标为已读且不回复。",
    es: "Respóndeme en el chat de OoLu y lo redactaré — o pulsa Ignorar para marcarlo leído sin respuesta.",
    fr: "Répondez-moi dans la discussion OoLu et je le rédigerai — ou appuyez sur Ignorer pour le marquer lu sans réponse.",
  },
  "rep.sendEdited": {
    en: "Send edited",
    zh: "发送修改稿",
    es: "Enviar editado",
    fr: "Envoyer la version modifiée",
  },
  "rep.cancel": { en: "Cancel", zh: "取消", es: "Cancelar", fr: "Annuler" },
  "rep.editing": {
    en: "Editing the drafted reply — Send records your version.",
    zh: "正在编辑草拟的回复——发送会记录你的版本。",
    es: "Editando la respuesta redactada: Enviar registra tu versión.",
    fr: "Modification de la réponse rédigée — Envoyer enregistre votre version.",
  },
  "rep.draftButton": {
    en: "Draft a reply in your voice",
    zh: "用你的语气起草回复",
    es: "Redactar una respuesta con tu voz",
    fr: "Rédiger une réponse avec votre voix",
  },
  "friends.who": {
    en: "Who do you want to add?",
    zh: "你想添加谁？",
    es: "¿A quién quieres añadir?",
    fr: "Qui voulez-vous ajouter ?",
  },
  "friends.whoHint": {
    en: "Enter their exact username or e-mail — there is no directory to browse, so nobody finds you unless you gave them your name. They decide whether to accept.",
    zh: "输入对方的确切用户名或邮箱——没有可浏览的目录，除非你给了对方你的名字，否则没人能找到你。是否接受由对方决定。",
    es: "Introduce su nombre de usuario o correo exacto: no hay directorio que explorar, así que nadie te encuentra a menos que le dieras tu nombre. La otra persona decide si acepta.",
    fr: "Saisissez leur identifiant ou e-mail exact — il n'y a pas d'annuaire à parcourir, donc personne ne vous trouve sauf si vous lui avez donné votre nom. C'est à l'autre d'accepter.",
  },
  "friends.usernameOrEmail": {
    en: "username or e-mail",
    zh: "用户名或邮箱",
    es: "usuario o correo",
    fr: "identifiant ou e-mail",
  },
  "friends.find": { en: "Find", zh: "查找", es: "Buscar", fr: "Rechercher" },
  "friends.looking": {
    en: "Looking…",
    zh: "查找中…",
    es: "Buscando…",
    fr: "Recherche…",
  },
  "friends.sendRequest": {
    en: "Send friend request",
    zh: "发送好友请求",
    es: "Enviar solicitud",
    fr: "Envoyer une demande",
  },
  "friends.requestSent": {
    en: "Request sent — waiting for them.",
    zh: "请求已发送——等待对方处理。",
    es: "Solicitud enviada: esperando su respuesta.",
    fr: "Demande envoyée — en attente de sa réponse.",
  },
  "friends.openChat": {
    en: "Open chat",
    zh: "打开聊天",
    es: "Abrir chat",
    fr: "Ouvrir la discussion",
  },
  "friends.accept": { en: "Accept", zh: "接受", es: "Aceptar", fr: "Accepter" },
  "friends.decline": { en: "Decline", zh: "拒绝", es: "Rechazar", fr: "Refuser" },
  "friends.block": { en: "Block", zh: "屏蔽", es: "Bloquear", fr: "Bloquer" },
  "friends.unblock": {
    en: "Unblock",
    zh: "取消屏蔽",
    es: "Desbloquear",
    fr: "Débloquer",
  },
  "friends.incoming": {
    en: "Friend requests waiting for you:",
    zh: "等待你处理的好友请求：",
    es: "Solicitudes de amistad esperándote:",
    fr: "Demandes d'amitié en attente :",
  },
  "friends.title": {
    en: "Friends & sign-in",
    zh: "好友与登录",
    es: "Amistades e inicio de sesión",
    fr: "Amis et connexion",
  },
  "friends.allowNonfriend": {
    en: "Receive messages from non-friends",
    zh: "接收非好友的消息",
    es: "Recibir mensajes de no amigos",
    fr: "Recevoir des messages de non-amis",
  },
  "friends.allowNonfriendDesc": {
    en: "Off means only accepted friends can message you; strangers must send a friend request first.",
    zh: "关闭后只有已接受的好友才能给你发消息；陌生人必须先发送好友请求。",
    es: "Desactivado significa que solo tus amigos aceptados pueden escribirte; los desconocidos deben enviar una solicitud primero.",
    fr: "Désactivé signifie que seuls vos amis acceptés peuvent vous écrire ; les inconnus doivent d'abord envoyer une demande.",
  },
  "friends.setPassword": {
    en: "Change password",
    zh: "修改密码",
    es: "Cambiar la contraseña",
    fr: "Changer le mot de passe",
  },
  "friends.setPasswordDesc": {
    en: "Every account has a password — Google and phone sign-ups get one auto-generated and sent to them. Change yours here any time.",
    zh: "每个账户都有密码——通过 Google 或手机号注册的账户会自动生成密码并发送给你。可随时在此修改。",
    es: "Cada cuenta tiene una contraseña — los registros con Google o teléfono reciben una autogenerada. Cámbiala aquí cuando quieras.",
    fr: "Chaque compte a un mot de passe — les inscriptions via Google ou téléphone en reçoivent un auto-généré. Changez-le ici à tout moment.",
  },
  "friends.passwordSet": {
    en: "Password set — you can sign in with your username and it next time.",
    zh: "密码已设置——下次可用用户名和它登录。",
    es: "Contraseña establecida: la próxima vez puedes iniciar sesión con tu usuario y ella.",
    fr: "Mot de passe défini — la prochaine fois, connectez-vous avec votre identifiant et lui.",
  },
  "friends.save": { en: "Save", zh: "保存", es: "Guardar", fr: "Enregistrer" },
  "work.seatOnboard": {
    en: "onboard",
    zh: "已入驻",
    es: "a bordo",
    fr: "à bord",
  },
  "work.seatOnDemand": {
    en: "on demand",
    zh: "按需",
    es: "bajo demanda",
    fr: "à la demande",
  },
  "work.assignHint": {
    en: "Assign a user to this seat — the org's staffing hand.",
    zh: "为该席位指派用户——组织的人事之手。",
    es: "Asigna un usuario a este puesto: la mano de personal de la organización.",
    fr: "Affectez un utilisateur à ce siège — la main RH de l'organisation.",
  },
  "work.assignUser": {
    en: "assign user",
    zh: "指派用户",
    es: "asignar usuario",
    fr: "affecter un utilisateur",
  },
  "work.assign": {
    en: "Assign",
    zh: "指派",
    es: "Asignar",
    fr: "Affecter",
  },
  "tpl.rebranchNote": {
    en: "A seat's function outgrew the branch threshold — the structure should re-reason and branch the work:",
    zh: "某席位的函数超过了分支阈值——结构应重新推理并拆分工作：",
    es: "La función de un puesto superó el umbral de ramificación: la estructura debería re-razonar y ramificar el trabajo:",
    fr: "La fonction d'un siège a dépassé le seuil de branchement — la structure devrait re-raisonner et brancher le travail :",
  },
  "tpl.rebranch": {
    en: "Re-reason structure",
    zh: "重新推理结构",
    es: "Re-razonar la estructura",
    fr: "Re-raisonner la structure",
  },
  "work.tabCode": { en: "Code", zh: "代码", es: "Código", fr: "Code" },
  "work.codeAllFiles": {
    en: "all files",
    zh: "全部文件",
    es: "todos los archivos",
    fr: "tous les fichiers",
  },
  "work.codeNoSummary": {
    en: "No description recorded for this node yet.",
    zh: "此节点暂无描述。",
    es: "Este nodo aún no tiene descripción.",
    fr: "Aucune description enregistrée pour ce nœud.",
  },
  "work.codeEmpty": {
    en: "Nothing built here yet — the node's function and assets will appear as files.",
    zh: "尚未构建任何内容——节点的函数与资源会以文件形式出现在这里。",
    es: "Aún no hay nada construido: la función y los recursos del nodo aparecerán como archivos.",
    fr: "Rien de construit ici pour l'instant — la fonction et les ressources du nœud apparaîtront comme des fichiers.",
  },
  "work.codeLanguagesNote": {
    en: "Nodes speak the mainstream languages: Python runs natively; JavaScript (main.js), C (main.c), C++ (main.cpp) and shell entries run through the sandbox's toolchains; JSON, HTML, Markdown and React sources ride as staged assets.",
    zh: "节点支持主流语言：Python 原生运行；JavaScript（main.js）、C（main.c）、C++（main.cpp）与 shell 入口经沙箱工具链运行；JSON、HTML、Markdown 与 React 源码作为随行资源。",
    es: "Los nodos hablan los lenguajes principales: Python se ejecuta de forma nativa; JavaScript (main.js), C (main.c), C++ (main.cpp) y shell pasan por las herramientas del sandbox; JSON, HTML, Markdown y fuentes React viajan como recursos.",
    fr: "Les nœuds parlent les langages courants : Python s'exécute nativement ; JavaScript (main.js), C (main.c), C++ (main.cpp) et shell passent par les outils du bac à sable ; JSON, HTML, Markdown et les sources React voyagent comme ressources.",
  },
  "work.tabAccess": {
    en: "Access",
    zh: "访问",
    es: "Acceso",
    fr: "Accès",
  },
  "work.newMemberName": {
    en: "new member node name",
    zh: "新成员节点名称",
    es: "nombre del nuevo nodo miembro",
    fr: "nom du nouveau nœud membre",
  },
  "work.memberSupernode": {
    en: "Supernode member",
    zh: "超级节点成员",
    es: "Miembro supernodo",
    fr: "Membre supernœud",
  },
  "work.createMember": {
    en: "Create member",
    zh: "创建成员",
    es: "Crear miembro",
    fr: "Créer le membre",
  },
  "net.globalOpenNote": {
    en: "Signed in to the global service, the web is open by default — no grants needed. Add hosts here only to NARROW what this node may reach; block lists always bind.",
    zh: "登录全局服务后，网络默认全部开放——无需授权。在此添加主机只会收窄此节点可访问的范围；屏蔽列表始终生效。",
    es: "Con sesión en el servicio global, la web está abierta por defecto: sin permisos previos. Añade hosts aquí solo para ACOTAR lo que este nodo alcanza; las listas de bloqueo siempre rigen.",
    fr: "Connecté au service global, le web est ouvert par défaut — aucune autorisation requise. Ajoutez des hôtes ici uniquement pour RESTREINDRE ce que ce nœud atteint ; les listes de blocage s'appliquent toujours.",
  },
  "profile.open": {
    en: "Profile of",
    zh: "查看资料：",
    es: "Perfil de",
    fr: "Profil de",
  },
  "profile.openHint": {
    en: "Name note, pin, mute, hide, delete — all live here.",
    zh: "备注名、置顶、免打扰、隐藏、删除——都在这里。",
    es: "Nota de nombre, fijar, silenciar, ocultar, eliminar: todo vive aquí.",
    fr: "Note de nom, épingler, muet, masquer, supprimer — tout est ici.",
  },
  "profile.pin": { en: "Pin", zh: "置顶", es: "Fijar", fr: "Épingler" },
  "profile.unpin": {
    en: "Unpin",
    zh: "取消置顶",
    es: "Desfijar",
    fr: "Désépingler",
  },
  "profile.mute": { en: "Mute", zh: "免打扰", es: "Silenciar", fr: "Muet" },
  "profile.unmute": {
    en: "Unmute",
    zh: "取消免打扰",
    es: "Reactivar avisos",
    fr: "Réactiver",
  },
  "profile.hide": { en: "Hide", zh: "隐藏", es: "Ocultar", fr: "Masquer" },
  "profile.hideHint": {
    en: "Hiding removes the thread from the list as it stands — new words bring it back by themselves.",
    zh: "隐藏会把会话从列表移走——对方再说话时它会自己回来。",
    es: "Ocultar quita el hilo de la lista tal como está: palabras nuevas lo traen de vuelta solas.",
    fr: "Masquer retire le fil de la liste en l'état — de nouveaux mots le ramènent d'eux-mêmes.",
  },
  "profile.delete": { en: "Delete", zh: "删除", es: "Eliminar", fr: "Supprimer" },
  "profile.deleteFriend": {
    en: "Delete friend",
    zh: "删除好友",
    es: "Eliminar amigo",
    fr: "Supprimer l'ami",
  },
  "profile.deleteFriendHint": {
    en: "Unfriends without blocking. Messages stay, and the thread returns if they write again.",
    zh: "仅解除好友关系，不拉黑。消息保留；对方再来信时会话会回来。",
    es: "Deja de ser amigo sin bloquear. Los mensajes se quedan y el hilo vuelve si escribe de nuevo.",
    fr: "Retire l'ami sans bloquer. Les messages restent et le fil revient s'il écrit à nouveau.",
  },
  "profile.confirmDelete": {
    en: "Yes, delete",
    zh: "确认删除",
    es: "Sí, eliminar",
    fr: "Oui, supprimer",
  },
  "profile.backToChat": {
    en: "Back to the conversation",
    zh: "返回会话",
    es: "Volver a la conversación",
    fr: "Retour à la conversation",
  },
  "noder.deleteHint": {
    en: "Removes it from the list. The run's record is preserved, and the thread returns if the node speaks again.",
    zh: "从列表移除。运行记录仍保留；节点再有动静时会话会回来。",
    es: "Lo quita de la lista. El registro de la ejecución se conserva y el hilo vuelve si el nodo habla de nuevo.",
    fr: "Le retire de la liste. Le journal d'exécution est conservé et le fil revient si le nœud reparle.",
  },
  "friends.myQr": {
    en: "My QR code",
    zh: "我的二维码",
    es: "Mi código QR",
    fr: "Mon code QR",
  },
  "friends.scanQr": {
    en: "Scan a code",
    zh: "扫码添加",
    es: "Escanear un código",
    fr: "Scanner un code",
  },
  "friends.qrHint": {
    en: "Side by side? One of you shows this code, the other scans it — friends in one tap.",
    zh: "面对面？一人出示二维码，另一人扫一扫——一步成为好友。",
    es: "¿Uno al lado del otro? Uno muestra este código y el otro lo escanea: amigos en un toque.",
    fr: "Côte à côte ? L'un montre ce code, l'autre le scanne — amis en un geste.",
  },
  "friends.scanning": {
    en: "Point the camera at your friend's code…",
    zh: "将相机对准对方的二维码…",
    es: "Apunta la cámara al código de tu amigo…",
    fr: "Pointez la caméra vers le code de votre ami…",
  },
  "friends.scanStop": {
    en: "Stop scanning",
    zh: "停止扫码",
    es: "Dejar de escanear",
    fr: "Arrêter le scan",
  },
  "friends.cameraError": {
    en: "The camera could not be opened.",
    zh: "无法打开相机。",
    es: "No se pudo abrir la cámara.",
    fr: "Impossible d'ouvrir la caméra.",
  },
  "friends.rename": {
    en: "Rename",
    zh: "备注名",
    es: "Renombrar",
    fr: "Renommer",
  },
  "friends.renameHint": {
    en: "Your note, only you see it — “Anna from the conference”.",
    zh: "只有你能看到的备注——比如“会展上认识的安娜”。",
    es: "Tu nota, solo tú la ves: «Anna, de la conferencia».",
    fr: "Votre note, visible de vous seul — « Anna, de la conférence ».",
  },
  "friends.namePlaceholder": {
    en: "name note (empty = username)",
    zh: "备注名（留空恢复用户名）",
    es: "nota de nombre (vacío = usuario)",
    fr: "note de nom (vide = identifiant)",
  },
  "friends.since": {
    en: "friends since {date}",
    zh: "{date} 成为好友",
    es: "amigos desde {date}",
    fr: "amis depuis {date}",
  },
  "sec.title": { en: "Security", zh: "安全", es: "Seguridad", fr: "Sécurité" },
  "sec.intro": {
    en: "OoLu can place orders and make bookings for you — but every one waits for you to re-confirm the exact amount and enter a fresh code from your authenticator app. A stolen session can't spend your money.",
    zh: "OoLu 可以为你下单和预订——但每一笔都要你重新确认确切金额并输入认证器应用中的最新验证码。会话被盗也无法动用你的钱。",
    es: "OoLu puede hacer pedidos y reservas por ti, pero cada uno espera a que reconfirmes el importe exacto e introduzcas un código nuevo de tu app de autenticación. Una sesión robada no puede gastar tu dinero.",
    fr: "OoLu peut passer des commandes et faire des réservations pour vous — mais chacune attend que vous reconfirmiez le montant exact et saisissiez un nouveau code de votre application d'authentification. Une session volée ne peut pas dépenser votre argent.",
  },
  "sec.2fa": {
    en: "Two-factor authentication",
    zh: "双重认证",
    es: "Autenticación de dos factores",
    fr: "Authentification à deux facteurs",
  },
  "sec.2faOn": {
    en: "On — orders can be authorized.",
    zh: "已开启——可以授权订单。",
    es: "Activada: se pueden autorizar pedidos.",
    fr: "Activée — les commandes peuvent être autorisées.",
  },
  "sec.2faOff": {
    en: "Off — set it up to let OoLu place orders.",
    zh: "未开启——设置后 OoLu 才能下单。",
    es: "Desactivada: actívala para que OoLu haga pedidos.",
    fr: "Désactivée — activez-la pour qu'OoLu passe des commandes.",
  },
  "sec.setUp": { en: "Set up", zh: "设置", es: "Configurar", fr: "Configurer" },
  "sec.disable": { en: "Turn off", zh: "关闭", es: "Desactivar", fr: "Désactiver" },
  "sec.scanAdd": {
    en: "Add this to your authenticator app",
    zh: "将此添加到你的认证器应用",
    es: "Añade esto a tu app de autenticación",
    fr: "Ajoutez ceci à votre application d'authentification",
  },
  "sec.scanDesc": {
    en: "Enter this key in Google Authenticator, Aegis, or 1Password, then type the 6-digit code it shows to confirm.",
    zh: "在 Google Authenticator、Aegis 或 1Password 中输入此密钥，然后输入它显示的 6 位验证码进行确认。",
    es: "Introduce esta clave en Google Authenticator, Aegis o 1Password y luego escribe el código de 6 dígitos que muestra para confirmar.",
    fr: "Saisissez cette clé dans Google Authenticator, Aegis ou 1Password, puis tapez le code à 6 chiffres affiché pour confirmer.",
  },
  "sec.enterCode": {
    en: "Authenticator code",
    zh: "认证器验证码",
    es: "Código del autenticador",
    fr: "Code de l'authentificateur",
  },
  "sec.confirm": { en: "Confirm", zh: "确认", es: "Confirmar", fr: "Confirmer" },
  "sec.ordersWaiting": {
    en: "Orders waiting for your go-ahead:",
    zh: "等待你批准的订单：",
    es: "Pedidos que esperan tu aprobación:",
    fr: "Commandes en attente de votre accord :",
  },
  "sec.confirmAmount": {
    en: "Re-enter the exact amount",
    zh: "重新输入确切金额",
    es: "Reintroduce el importe exacto",
    fr: "Ressaisissez le montant exact",
  },
  "sec.amount": { en: "amount", zh: "金额", es: "importe", fr: "montant" },
  "sec.authorize": {
    en: "Authorize & pay",
    zh: "授权并支付",
    es: "Autorizar y pagar",
    fr: "Autoriser et payer",
  },
  "sec.decline": { en: "Decline", zh: "拒绝", es: "Rechazar", fr: "Refuser" },
  "rep.toggleOn": {
    en: "✍ Representative: on",
    zh: "✍ 个人代表：开",
    es: "✍ Representante: sí",
    fr: "✍ Représentant : oui",
  },
  "rep.toggleOff": {
    en: "✍ Representative: off",
    zh: "✍ 个人代表：关",
    es: "✍ Representante: no",
    fr: "✍ Représentant : non",
  },
  "rep.toggleHint": {
    en: "Draft replies to waiting friends in your voice — you only filter.",
    zh: "用你的语气为等候中的好友起草回复——你只需筛选。",
    es: "Redacta respuestas con tu voz para amistades en espera: tú solo filtras.",
    fr: "Rédige des réponses avec votre voix pour les amis en attente — vous ne faites que filtrer.",
  },
  "rep.autoToPeer": {
    en: "Auto-replies to {peer} (earned replies only; commitments always wait for you)",
    zh: "对 {peer} 的自动回复（仅限已赢得信任的回复；涉及承诺的内容永远等你决定）",
    es: "Auto-respuestas a {peer} (solo respuestas ganadas; los compromisos siempre te esperan)",
    fr: "Réponses auto à {peer} (réponses méritées uniquement ; les engagements vous attendent toujours)",
  },
};

// The settings node's own catalog, translated. The BACKEND stays the
// single source of which settings exist and what they mean (its English
// label/description ride along as the fallback); this table only puts
// those words in the interface language — the fix for "I changed the
// language and Settings kept speaking English".
const SETTING_STRINGS: Record<string, { label: Entry; desc?: Entry }> = {
  "app.theme": {
    label: { en: "Theme", zh: "主题", es: "Tema", fr: "Thème" },
    desc: {
      en: "The app's colour theme.",
      zh: "应用的配色主题。",
      es: "El tema de color de la aplicación.",
      fr: "Le thème de couleurs de l'application.",
    },
  },
  "app.language": {
    label: { en: "Language", zh: "语言", es: "Idioma", fr: "Langue" },
    desc: {
      en: "Interface language.",
      zh: "界面语言。",
      es: "Idioma de la interfaz.",
      fr: "Langue de l'interface.",
    },
  },
  "model.web_search": {
    label: {
      en: "Model web search",
      zh: "模型联网搜索",
      es: "Búsqueda web del modelo",
      fr: "Recherche web du modèle",
    },
    desc: {
      en: "Let the model search the web for current facts when it needs to (runs inside the provider's API call — Claude today; a local model never searches).",
      zh: "允许模型在需要时联网搜索最新信息（在服务商的 API 调用内完成——目前为 Claude；本地模型从不联网）。",
      es: "Permite que el modelo busque en la web datos actuales cuando lo necesite (ocurre dentro de la llamada a la API del proveedor — Claude hoy; un modelo local nunca busca).",
      fr: "Autorise le modèle à chercher sur le web des faits actuels quand il en a besoin (dans l'appel API du fournisseur — Claude aujourd'hui ; un modèle local ne cherche jamais).",
    },
  },
  "app.notifications": {
    label: {
      en: "Notifications",
      zh: "通知",
      es: "Notificaciones",
      fr: "Notifications",
    },
    desc: {
      en: "Notify me when a task finishes or needs me.",
      zh: "当任务完成或需要我时通知我。",
      es: "Avísame cuando una tarea termine o me necesite.",
      fr: "Me prévenir quand une tâche se termine ou a besoin de moi.",
    },
  },
  "app.voice_replies": {
    label: {
      en: "Speak replies aloud",
      zh: "朗读回复",
      es: "Leer respuestas en voz alta",
      fr: "Lire les réponses à voix haute",
    },
    desc: {
      en: "OoLu reads its replies out loud along with the message. Turn off here for silent conversations.",
      zh: "OoLu 会在显示消息的同时朗读回复。想安静对话就在这里关闭。",
      es: "OoLu lee sus respuestas en voz alta junto con el mensaje. Desactívalo aquí para conversaciones en silencio.",
      fr: "OoLu lit ses réponses à voix haute avec le message. Désactivez ici pour des conversations silencieuses.",
    },
  },
  "account.display_name": {
    label: {
      en: "Display name",
      zh: "显示名称",
      es: "Nombre visible",
      fr: "Nom affiché",
    },
    desc: {
      en: "The name shown on your account.",
      zh: "账户上显示的名称。",
      es: "El nombre que se muestra en tu cuenta.",
      fr: "Le nom affiché sur votre compte.",
    },
  },
  "account.currency": {
    label: {
      en: "Spending currency",
      zh: "消费货币",
      es: "Moneda de gasto",
      fr: "Devise de dépense",
    },
    desc: {
      en: "The legal currency of your region — every cap and spending amount is entered and shown in it. Conversion to the meter's internal unit uses fixed reference rates.",
      zh: "你所在地区的法定货币——所有上限和消费金额都以它输入和显示。换算到内部计量单位使用固定参考汇率。",
      es: "La moneda legal de tu región: cada límite e importe de gasto se introduce y se muestra en ella. La conversión a la unidad interna usa tasas de referencia fijas.",
      fr: "La devise légale de votre région — chaque plafond et montant s'y saisit et s'y affiche. La conversion vers l'unité interne utilise des taux de référence fixes.",
    },
  },
  "account.units": {
    label: {
      en: "Measurement units",
      zh: "计量单位",
      es: "Unidades de medida",
      fr: "Unités de mesure",
    },
    desc: {
      en: "Which measurement system OoLu answers in — metres and kilograms (metric/SI) or feet and pounds (imperial). Auto follows your region: imperial for the US, SI everywhere else.",
      zh: "OoLu 回答时使用的计量系统——米和千克（公制/SI）或英尺和磅（英制）。自动模式跟随你所在地区：美国用英制，其他地区用 SI。",
      es: "El sistema de medida con el que responde OoLu: metros y kilogramos (métrico/SI) o pies y libras (imperial). Automático sigue tu región: imperial para EE. UU., SI en el resto.",
      fr: "Le système de mesure utilisé par OoLu — mètres et kilogrammes (métrique/SI) ou pieds et livres (impérial). Auto suit votre région : impérial pour les États-Unis, SI partout ailleurs.",
    },
  },
  "account.log_retention_days": {
    label: {
      en: "Execution log retention",
      zh: "执行日志保留期",
      es: "Retención de registros de ejecución",
      fr: "Rétention des journaux d'exécution",
    },
    desc: {
      en: "How long each node keeps its daily execution log files (in its Files drawer under logs/) before pruning. Set it to your legal record-keeping requirement.",
      zh: "每个节点在清理前保留其每日执行日志文件（位于其文件抽屉的 logs/ 下）的时长。请按你的法定留存要求设置。",
      es: "Cuánto tiempo guarda cada nodo sus registros diarios de ejecución (en su cajón de Archivos bajo logs/) antes de depurarlos. Ajústalo a tu obligación legal de conservación.",
      fr: "Combien de temps chaque nœud conserve ses journaux d'exécution quotidiens (dans son tiroir Fichiers sous logs/) avant élagage. Réglez-le sur votre obligation légale de conservation.",
    },
  },
  "account.autobuild_consent": {
    label: {
      en: "Auto-build nodes on my paths",
      zh: "自动构建路径上的节点",
      es: "Autoconstruir nodos en mis rutas",
      fr: "Auto-construire des nœuds sur mes chemins",
    },
    desc: {
      en: "Let OoLu build missing nodes and publish them under my account. Off by default: when a task has no existing path, OoLu asks you to turn this on before building anything new.",
      zh: "允许 OoLu 构建缺失的节点并以我的账户发布。默认关闭：当任务没有现成路径时，OoLu 会先征求你的同意再构建。",
      es: "Permite que OoLu construya nodos faltantes y los publique bajo mi cuenta. Desactivado por defecto: cuando una tarea no tiene ruta, OoLu te pide activarlo antes de construir nada nuevo.",
      fr: "Autorise OoLu à construire les nœuds manquants et à les publier sous mon compte. Désactivé par défaut : sans chemin existant, OoLu vous demande de l'activer avant toute construction.",
    },
  },
  "subscription.plan": {
    label: { en: "Plan", zh: "套餐", es: "Plan", fr: "Forfait" },
    desc: {
      en: "Your current plan. Managed in the account console — cancel the current plan there to change terms.",
      zh: "你当前的套餐。在账户中心管理——先在那里取消当前套餐才能更改条款。",
      es: "Tu plan actual. Se gestiona en la consola de cuenta: cancela allí el plan actual para cambiar las condiciones.",
      fr: "Votre forfait actuel. Géré dans la console du compte — annulez-y le forfait actuel pour changer de conditions.",
    },
  },
  "subscription.billing_cycle": {
    label: {
      en: "Billing cycle",
      zh: "计费周期",
      es: "Ciclo de facturación",
      fr: "Cycle de facturation",
    },
    desc: {
      en: "Monthly or yearly. Managed in the account console with the plan.",
      zh: "按月或按年。与套餐一起在账户中心管理。",
      es: "Mensual o anual. Se gestiona en la consola de cuenta junto con el plan.",
      fr: "Mensuel ou annuel. Géré dans la console du compte avec le forfait.",
    },
  },
  "model.source": {
    label: {
      en: "Default model",
      zh: "默认模型",
      es: "Modelo predeterminado",
      fr: "Modèle par défaut",
    },
    desc: {
      en: "Where the brain lives. Subscription follows your OoLu plan (Claude first). Own API makes the key you added below the default model, overriding the plan. Local uses a model server running on this machine — no key, no cloud.",
      zh: "大脑所在。订阅跟随你的 OoLu 套餐（优先 Claude）。自有 API 使下方添加的密钥成为默认模型，覆盖套餐。本地使用本机上运行的模型服务器——无需密钥，不上云。",
      es: "Dónde vive el cerebro. Suscripción sigue tu plan de OoLu (Claude primero). API propia hace de la clave añadida abajo el modelo predeterminado, por encima del plan. Local usa un servidor de modelo en esta máquina: sin clave, sin nube.",
      fr: "Où vit le cerveau. Abonnement suit votre forfait OoLu (Claude d'abord). API personnelle fait de la clé ajoutée ci-dessous le modèle par défaut, au-dessus du forfait. Local utilise un serveur de modèle sur cette machine — sans clé, sans cloud.",
    },
  },
  "model.provider": {
    label: {
      en: "Model provider",
      zh: "模型服务商",
      es: "Proveedor del modelo",
      fr: "Fournisseur du modèle",
    },
    desc: {
      en: "Which of your own keys answers when the default model is own API. Auto tries Anthropic first, then OpenAI — whichever has a key configured.",
      zh: "当默认模型为自有 API 时由哪把密钥应答。自动会先试 Anthropic，再试 OpenAI——哪个配置了密钥就用哪个。",
      es: "Cuál de tus claves responde cuando el modelo predeterminado es API propia. Automático prueba primero Anthropic y luego OpenAI, según cuál tenga clave configurada.",
      fr: "Laquelle de vos clés répond quand le modèle par défaut est API personnelle. Auto essaie Anthropic d'abord, puis OpenAI — selon la clé configurée.",
    },
  },
  "model.local_url": {
    label: {
      en: "Local model URL",
      zh: "本地模型地址",
      es: "URL del modelo local",
      fr: "URL du modèle local",
    },
    desc: {
      en: "The OpenAI-compatible endpoint of the model server on this machine (Ollama, LM Studio, llama.cpp server). Used only when the default model is local.",
      zh: "本机模型服务器的 OpenAI 兼容端点（Ollama、LM Studio、llama.cpp server）。仅在默认模型为本地时使用。",
      es: "El punto de conexión compatible con OpenAI del servidor de modelo en esta máquina (Ollama, LM Studio, llama.cpp server). Solo se usa cuando el modelo predeterminado es local.",
      fr: "Le point d'accès compatible OpenAI du serveur de modèle sur cette machine (Ollama, LM Studio, llama.cpp server). Utilisé seulement quand le modèle par défaut est local.",
    },
  },
  "model.local_model": {
    label: {
      en: "Local model name",
      zh: "本地模型名称",
      es: "Nombre del modelo local",
      fr: "Nom du modèle local",
    },
    desc: {
      en: "The model to request from the local server, e.g. llama3.2 or qwen3. Required when the default model is local.",
      zh: "向本地服务器请求的模型名，例如 llama3.2 或 qwen3。默认模型为本地时必填。",
      es: "El modelo que se pide al servidor local, p. ej. llama3.2 o qwen3. Obligatorio cuando el modelo predeterminado es local.",
      fr: "Le modèle demandé au serveur local, p. ex. llama3.2 ou qwen3. Requis quand le modèle par défaut est local.",
    },
  },
  "model.tier": {
    label: {
      en: "Model tier",
      zh: "模型档位",
      es: "Nivel del modelo",
      fr: "Niveau du modèle",
    },
    desc: {
      en: "Fast answers cheaply; reasoning thinks harder and costs more per turn.",
      zh: "快速档回答便宜；深度思考档想得更深，每轮更贵。",
      es: "Rápido responde barato; razonamiento piensa más y cuesta más por turno.",
      fr: "Rapide répond à moindre coût ; raisonnement réfléchit plus et coûte plus par tour.",
    },
  },
  "budget.model_cap": {
    label: {
      en: "Model spending cap",
      zh: "模型消费上限",
      es: "Límite de gasto del modelo",
      fr: "Plafond de dépense du modèle",
    },
    desc: {
      en: "Stop calling the model once metered chat spending reaches this amount in your spending currency (0 = no cap). Tasks still run.",
      zh: "当计量的对话消费达到该金额（以你的消费货币计）时停止调用模型（0 = 不设上限）。任务仍会运行。",
      es: "Deja de llamar al modelo cuando el gasto medido de chat alcance este importe en tu moneda (0 = sin límite). Las tareas siguen ejecutándose.",
      fr: "Cesse d'appeler le modèle quand la dépense mesurée de chat atteint ce montant dans votre devise (0 = sans plafond). Les tâches continuent.",
    },
  },
  "budget.hard_cap": {
    label: {
      en: "Hard spending cap",
      zh: "硬性消费上限",
      es: "Límite duro de gasto",
      fr: "Plafond strict de dépense",
    },
    desc: {
      en: "Refuse any task estimated above this amount in your spending currency (0 = no cap).",
      zh: "拒绝任何预估超过该金额（以你的消费货币计）的任务（0 = 不设上限）。",
      es: "Rechaza cualquier tarea estimada por encima de este importe en tu moneda (0 = sin límite).",
      fr: "Refuse toute tâche estimée au-dessus de ce montant dans votre devise (0 = sans plafond).",
    },
  },
  "budget.review_threshold": {
    label: {
      en: "Review threshold",
      zh: "复核阈值",
      es: "Umbral de revisión",
      fr: "Seuil de validation",
    },
    desc: {
      en: "Ask me to confirm tasks estimated above this amount in your spending currency (0 = off).",
      zh: "预估超过该金额（以你的消费货币计）的任务需先经我确认（0 = 关闭）。",
      es: "Pídeme confirmar tareas estimadas por encima de este importe en tu moneda (0 = desactivado).",
      fr: "Me demander de confirmer les tâches estimées au-dessus de ce montant dans votre devise (0 = désactivé).",
    },
  },
  "budget.monthly_limit": {
    label: {
      en: "Monthly limit",
      zh: "每月限额",
      es: "Límite mensual",
      fr: "Limite mensuelle",
    },
    desc: {
      en: "A soft monthly spending target in your spending currency (0 = none).",
      zh: "以你的消费货币计的每月软性支出目标（0 = 无）。",
      es: "Un objetivo mensual de gasto orientativo en tu moneda (0 = ninguno).",
      fr: "Un objectif mensuel indicatif de dépense dans votre devise (0 = aucun).",
    },
  },
};

// A catalog item's words in the interface language — the server's own
// English label/description stand whenever no translation is declared
// (a new knob is never blocked on the dictionary).
// Traditional Chinese reads the generated table, then Simplified.
function fromEntry(entry: Entry, keyForHant: string): string | undefined {
  if (language === "zh-hant") return ZH_HANT[keyForHant] ?? entry.zh;
  return entry[language as keyof Entry];
}

export function settingLabel(key: string, fallback: string): string {
  const entry = SETTING_STRINGS[key];
  return entry
    ? (fromEntry(entry.label, `setting.${key}.label`) ?? fallback)
    : fallback;
}

export function settingDesc(
  key: string,
  fallback: string | null | undefined,
): string | null {
  const entry = SETTING_STRINGS[key];
  if (entry?.desc) {
    return fromEntry(entry.desc, `setting.${key}.desc`) ?? fallback ?? null;
  }
  return fallback ?? null;
}

export function unitLabel(unit: string): string {
  if (STRINGS[`unit.${unit}`]) return t(`unit.${unit}`);
  return unit;
}

export function t(key: string): string {
  const entry = STRINGS[key];
  if (!entry) return key;
  if (language === "zh-hant") return ZH_HANT[key] ?? entry.zh ?? entry.en;
  return entry[language] ?? entry.en;
}

// A node's display name: node names are the user's own words and stay as
// written — except the product-seeded starter node, whose name is chrome.
export function displayNodeName(name: string): string {
  return name === "Handiwork" ? t("handiwork") : name;
}

// t() with placeholders: tf("files.reallyDelete", { n: 3 }). Keys keep
// whole sentences so every language can order its words its own way.
export function tf(
  key: string,
  vars: Record<string, string | number>,
): string {
  let out = t(key);
  for (const [name, value] of Object.entries(vars)) {
    out = out.split(`{${name}}`).join(String(value));
  }
  return out;
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

// Traditional Chinese, generated from the Simplified table with
// OpenCC (s2twp — Taiwan-standard phrasing). Regenerate rather than
// hand-edit drift in: every zh string above has a row here.
const ZH_HANT: Record<string, string> = {
  "life": "生活",
  "work": "工作",
  "assistantSub": "你的助手",
  "files": "檔案",
  "filesSub": "文件與表格",
  "settings": "設定",
  "settingsSub": "應用、賬戶、模型、預算",
  "friends": "好友",
  "noder": "節點",
  "startConversation": "發起對話",
  "newConversation": "新對話",
  "friendsNeedServer": "好友功能需要伺服器",
  "nodeActivityHere": "節點活動將顯示在這裡。",
  "messageOoLu": "給 OoLu 發訊息…",
  "send": "傳送",
  "cancel": "取消",
  "forwardThis": "轉發此訊息",
  "forwardSearch": "搜尋好友和節點…",
  "newFileInFiles": "存為新檔案",
  "noMatches": "沒有匹配項",
  "groupApp": "應用",
  "groupAccount": "賬戶",
  "groupSubscription": "訂閱",
  "groupModel": "模型",
  "groupBudget": "預算",
  "privacyData": "隱私與資料",
  "subscriptionNote": "套餐是一項承諾，而非偏好設定——此處僅展示，管理請前往賬戶中心（先取消當前套餐才能更改條款）。",
  "modelNote": "OoLu 的大腦所在。訂閱模式跟隨你的 OoLu 套餐（優先 Claude）。在下方新增自己的 API 金鑰並把預設模型切換為自有 API，即可用你的金鑰覆蓋套餐——或在本機執行本地模型伺服器並選擇本地：無需金鑰，不上雲。",
  "managePlan": "管理套餐",
  "managePlanDesc": "升級（抵扣餘額）、取消，或切換按月/按年。",
  "openConsole": "開啟賬戶中心",
  "regionSuggests": "根據你的地區建議使用",
  "use": "使用",
  "downloadData": "下載我的資料",
  "downloadDataDesc": "此主機上與你相關的全部資料，匯出為一個 JSON 文件。",
  "download": "下載",
  "deleteAccount": "刪除我的賬戶",
  "deleteAccountDesc": "刪除你的訊息、對話、登入身份和銀行卡資訊，並永久停用賬戶。共享抽屜中的檔案會保留——請先在“檔案”中刪除屬於你的檔案。",
  "legal": "法律條款",
  "legalDesc": "此主機在其公開法律連結上提供的條款內容。",
  "chat.welcome": "嘿！⚡ 我是 OoLu，你的幹活搭檔。我們先從什麼開始？",
  "chat.firstRunTitle": "第一次來？一分鐘完成你的第一個任務：",
  "chat.sayHi": "打個招呼",
  "chat.sayHiTail": "——聽聽我怎麼說話。",
  "chat.tryTask": "試試第一個任務",
  "chat.tryTaskTail": "——我會把它填進輸入框；按傳送，然後在節點裡看它執行。",
  "chat.brainTip": "給我一個大腦：在列表中開啟“設定”，新增模型金鑰或指向本地模型——沒有它任務也能執行，有了它對話更聰明。",
  "chat.gotIt": "知道了——隱藏",
  "chat.listening": "正在聆聽…",
  "chat.tapToStop": "正在聆聽——點按停止",
  "chat.tapHold": "點按傳送 · 長按說話",
  "chat.reminderChip": "提醒",
  "chat.openTask": "開啟該任務的操作視窗",
  "quick.whatCanYouDo": "你能做什麼？",
  "quick.myTasks": "我的任務",
  "quick.myFiles": "我的檔案",
  "quick.myNodes": "我的節點",
  "quick.mySettings": "我的設定",
  "mood.calm": "在你身邊",
  "mood.happy": "結果太棒了 ✨",
  "mood.thinking": "正埋頭處理你的任務",
  "mood.worried": "處理中——正在解決一個問題",
  "mood.excited": "幹勁十足，洗耳恭聽！⚡",
  "device.shareLocation": "共享我的位置",
  "device.takePhoto": "拍照",
  "device.chooseFile": "選擇檔案",
  "device.notNow": "暫不",
  "device.locationSettled": "位置請求已處理",
  "device.cameraSettled": "相機請求已處理",
  "device.fileSettled": "檔案請求已處理",
  "run.gone": "該任務已不可用。",
  "run.starting": "啟動中…",
  "run.approve": "批准",
  "run.reject": "拒絕",
  "run.retry": "重試",
  "run.runAgain": "再次執行",
  "run.retrying": "重試中…",
  "run.abort": "中止",
  "run.showSteps": "我做了什麼",
  "run.hideSteps": "隱藏我做了什麼",
  "run.fetching": "正在獲取記錄…",
  "run.nothingYet": "尚無記錄。",
  "run.retriesOne": "已重試 1 次",
  "run.retriesMany": "已重試 {n} 次",
  "run.nextRebuilds": "——下次重試將讓 OoLu 規劃並重建路徑",
  "status.needsAnswer": "需要回答",
  "status.needsDecision": "需要決定",
  "status.snag": "遇到問題",
  "status.done": "完成",
  "status.failed": "失敗",
  "status.cancelled": "已取消",
  "status.working": "進行中…",
  "voice.clarification": "我需要你的回答才能繼續。",
  "voice.confirmation": "行動前我需要你的許可。",
  "voice.approval": "這需要經授權的批准我才能行動。",
  "voice.incident": "出了點問題——告訴我該怎麼辦。",
  "voice.completed": "完成——這是經驗證的結果。",
  "voice.failed": "沒有成功。",
  "voice.cancelledSentence": "已按你的要求停止。",
  "voice.working": "我在處理——你可以在下方看到每一步。",
  "event.workflow.submitted": "已接受任務",
  "event.workflow.started": "開始工作",
  "event.workflow.advance": "進入下一步",
  "event.workflow.advanced": "進入下一步",
  "event.workflow.executed": "執行了操作",
  "event.workflow.paused": "已暫停——等待你",
  "event.workflow.resumed": "已繼續",
  "event.workflow.completed": "完成了任務",
  "event.workflow.failed": "遇到失敗",
  "event.workflow.incident": "遇到問題",
  "event.workflow.cancelled": "已按你的請求停止",
  "event.workflow.preflight_failed": "執行前已停止——預檢未透過",
  "event.contract.held": "請求已暫掛，等待人工確認",
  "event.contract.approved": "審批人已確認該請求",
  "event.contract.declined": "審批人已拒絕該請求",
  "event.contract.expired": "暫掛的請求已過期，未做決定",
  "event.feedback.received": "已記下你的反饋",
  "event.skill.blocked": "攔截了不安全的操作",
  "choice.system": "跟隨系統",
  "choice.light": "淺色",
  "choice.dark": "深色",
  "choice.fast": "快速",
  "choice.reasoning": "深度思考",
  "choice.subscription": "訂閱",
  "choice.own-api": "自有 API 金鑰",
  "choice.local": "本地",
  "choice.auto": "自動",
  "choice.monthly": "按月",
  "choice.yearly": "按年",
  "work.myNodes": "我的節點",
  "work.addNodeTitle": "建立節點或接管已有節點",
  "work.empty": "還沒有節點——按 + 建立或接管一個。",
  "work.pick": "選擇一個節點，看看它一直在做什麼。",
  "work.pickSub": "收益與健康度隨執行驗證而更新。",
  "work.noRunsYet": "尚無執行",
  "work.healthy": "健康度 {pct}%",
  "regime.supernode": "超級節點",
  "regime.audit": "審計",
  "regime.autogrow": "自動生長",
  "regime.standalone": "獨立",
  "work.createTab": "建立節點",
  "work.onboardTab": "接管已有節點",
  "work.name": "名稱",
  "work.whatItDoes": "它做什麼",
  "work.fnLabel": "函式（可選——帶上你自己的程式碼）",
  "work.uploadPy": "上傳 .py 函式",
  "work.fnPlaceholder": "貼上或上傳一個自包含的 Python 函式。它必須呼叫一次 emit_result 輸出結果。它在沙箱中執行——無網路、無主機憑據——並在儲存前經過篩查與驗證。",
  "work.fixedNote": "以下選擇在建立時即固定——之後永遠無法更改。",
  "work.supernodeCheck": "超級節點——為團體、企業或政府部門管理眾多節點，由人完全掌控（始終審計）",
  "work.underSupernode": "隸屬超級節點",
  "work.noneStandalone": "（無——獨立節點，無許可權級別）",
  "work.authority": "許可權級別",
  "work.claimNote": "在超級節點下建立的節點開始時沒有負責人賬戶。節點 id 就是認領憑證：只交給應當接管的人，切勿公開發布——接管的使用者賬戶將成為節點上顯示的負責人。",
  "work.auditCheck": "審計節點——每個請求都必須人工確認",
  "work.autogrowCheck": "自動生長——經過此節點的資料可用於新的開發",
  "work.policyCheck": "我同意節點政策——克隆、欺詐和殭屍節點會被檢測，平臺可對其限制或移除",
  "work.policyFirst": "請先同意節點政策——正是它授權平臺限制或移除克隆、欺詐和殭屍節點。",
  "work.onboardNote": "為一個已存在的節點承擔責任。審計、自動生長以及任何超級節點歸屬或許可權級別都在建立時已固定——接管時沒有任何選項。接管將署上你的名字：你的使用者 ID 會作為負責人顯示在節點上。",
  "work.nodeId": "節點 id",
  "work.working": "處理中…",
  "work.createNode": "建立節點",
  "work.onboard": "接管",
  "work.responsible": "負責人",
  "work.admin": "管理組",
  "work.notOnboarded": "尚未接管",
  "work.unclaimedNote": "此節點尚無負責人賬戶。不要公開展示其節點 id——憑它接管的人將成為負責人。只把它交給應當負責的人；對方接管後，其使用者 ID 會顯示在這裡。",
  "work.under": "隸屬",
  "work.memberNodes": "成員節點",
  "work.keepIdPrivate": "未接管——請保密其 id",
  "work.pending": "待處理",
  "work.tabActivity": "活動",
  "work.tabInteract": "互動",
  "work.orderLabel": "執行順序：",
  "work.orderStep": "第 {n} 步",
  "work.onDemand": "按需調用",
  "work.orderHint": "組織的 SOP：工作按編號依次傳遞——相同編號並行執行，留空表示按需調用。只有該超級節點的所有者才能設定。",
  "work.orderBad": "步驟編號是從 1 開始的整數——留空表示按需調用",
  "work.imitate": "模仿學習",
  "work.imitateRecording": "學習中…",
  "work.imitateHint": "OoLu 無法監視其他應用——沒有螢幕或按鍵錄製。請在這裡教學：說出目標，按順序描述每一步，並在錄製期間透過該節點執行真實工作——執行日誌會與你的步驟自動配對，結束並構建即可把演示變成一個可用節點。",
  "work.imitateGoal": "新節點要做什麼？",
  "work.imitateStart": "開始教學",
  "work.imitateStepPh": "描述下一步…",
  "work.imitateAdd": "新增步驟",
  "work.imitateBuild": "結束並構建節點",
  "work.imitateDiscard": "放棄教學",
  "work.loadingActivity": "正在載入活動…",
  "work.noExecutions": "尚無執行——當市場使用此節點時，執行會顯示在這裡。",
  "work.yoursToAnswer": "你是此節點的負責人——上面的每一步都由你負責。",
  "work.nooneAnswers": "此節點尚無人負責——當合適的人憑節點 id 接管時，它才有負責人。",
  "net.header": "網路訪問",
  "net.none": "未授予任何主機——在你指明它可訪問的具體主機之前，此節點完全無法訪問網路。",
  "net.withdraw": "撤回",
  "net.grant": "授予主機",
  "net.hostLabel": "要授予的主機",
  "hold.from": "來自",
  "hold.unknown": "未知",
  "hold.allow": "允許",
  "hold.reject": "拒絕",
  "hold.sign": "簽名並允許",
  "hold.signPh": "輸入你的姓名以簽名",
  "hold.replyPh": "輸入給請求者的回覆",
  "hold.sendReply": "傳送回覆",
  "kyc.header": "KYC——法律實體",
  "kyc.underReview": "稽核中",
  "kyc.fastLane": "快速通道——受信任的公司域名",
  "kyc.fastRow": "快速通道——受信任域名",
  "kyc.queue": "普通佇列",
  "kyc.apply": "申請",
  "kyc.pitch": "遵守 KYC 政策以獲得全球信任排名：驗證依託於你的付費套餐，透過驗證的超級節點為其下每個節點帶來信任倍數。請使用公司郵箱——個人郵箱會被拒絕。",
  "kyc.rejectedTail": "。你可以在下方重新申請。",
  "kyc.rejectedLead": "上一次申請被拒絕",
  "kyc.legalNamePh": "法律實體名稱",
  "kyc.regNoPh": "註冊號（可選）",
  "kyc.inbox": "等待你裁定的 KYC 稽核",
  "kyc.approve": "批准",
  "kyc.verifiedBadge": "KYC 已驗證 · 全球信任",
  "interact.hint": "讓 OoLu 在此節點上行動——“pending”列出等待中的任務，“sign <任務 id> as <你的名字>”把任務傳給下一個節點，“reply <任務 id>: <訊息>”，或“build <缺失的東西>”。",
  "interact.reliabilityNone": "自動化可靠度：尚無經驗證的執行——它隨此節點執行的每個任務而增長。",
  "interact.reliability": "自動化可靠度：{n} 次經驗證執行中達 {pct}%——每次驗證執行都讓此節點更接近全自動。",
  "interact.runOne": "次",
  "interact.runMany": "次",
  "interact.messageAbout": "就 {name} 給 OoLu 發訊息…",
  "interact.thinking": "處理中——準備好就回覆。",
  "files.yours": "你的檔案",
  "files.nodes": "此節點的檔案",
  "files.select": "選擇",
  "files.done": "完成",
  "files.add": "新增",
  "files.addTitle": "從此裝置上傳，或新建資料夾",
  "files.upload": "從裝置上傳",
  "files.newFolder": "新建資料夾",
  "files.folderNamePh": "資料夾名稱",
  "files.folderName": "資料夾名稱",
  "files.create": "建立",
  "files.selectedCount": "已選擇 {n} 項",
  "files.forward": "轉發…",
  "files.deleteEllipsis": "刪除…",
  "files.reallyDelete": "確定刪除 {n} 項？",
  "files.emptyNode": "這裡還沒有內容——此節點的檔案只歸它自己。",
  "files.emptyLife": "還沒有檔案——讓 OoLu 寫點什麼，或按 + 從此裝置匯入一個。",
  "files.upOne": "上一級",
  "files.folderSub": "資料夾 · 拖放檔案以移動",
  "files.emptyFolder": "空資料夾——拖入檔案，或讓 OoLu 在這裡寫一個。",
  "file.opening": "正在開啟…",
  "file.fetching": "正在獲取檔案…",
  "file.backToFiles": "← 檔案",
  "file.download": "下載",
  "file.deleteAction": "刪除",
  "file.forwardAction": "轉發",
  "file.saveTitle": "把此檔案儲存到裝置——原始位元組、真實型別",
  "file.edit": "編輯",
  "file.emptyDoc": "這個文件是空的。",
  "file.downloadDevice": "下載到此裝置",
  "file.lifeDrawer": "你的檔案（生活）",
  "file.copiedTo": "已複製到 {name}",
  "unit.days": "天",
  "unit.currency": "以你的貨幣計",
  "nav.back": "返回",
  "nav.hideList": "收起列表",
  "nav.showList": "展開列表",
  "login.edgeIntro": "Edge 讓一切留在你這邊：這臺裝置，或你自己網路中的私有伺服器。",
  "login.thisDevice": "這臺裝置",
  "login.privateNetwork": "私有網路",
  "login.deviceIntro": "你的賬戶、你的引擎，以及你教給 OoLu 的一切都留在這臺機器上。",
  "login.continueEdge": "在 Edge 上繼續",
  "login.networkIntro": "你的團隊在自己網路中執行的私有伺服器（一個大家都能訪問的固定地址）。你仍需用使用者名稱和密碼登入——在超級節點下建立的節點必須對應一個真實的人。",
  "login.serverAddress": "私有伺服器地址",
  "login.enterServer": "請輸入你的私有伺服器地址",
  "login.checkInbox": "檢視你的郵箱——輸入 6 位驗證碼完成。",
  "login.resetEnterCode": "輸入郵件中的驗證碼並設定新密碼。",
  "login.resetEnterEmail": "輸入你的郵箱，我們將傳送重置驗證碼。",
  "login.signInEdge": "登入你的私有網路伺服器。",
  "login.registerEdge": "在私有網路伺服器上建立你的賬戶。",
  "login.signInGlobal": "登入 OoLu Global。",
  "login.registerGlobal": "建立你的 OoLu Global 賬戶。",
  "login.username": "使用者名稱",
  "login.email": "郵箱",
  "login.code": "6 位驗證碼",
  "login.password": "密碼",
  "login.newPassword": "新密碼",
  "login.signIn": "登入",
  "login.signingIn": "登入中…",
  "login.createAccount": "建立賬戶",
  "login.creatingAccount": "建立賬戶中…",
  "login.verify": "驗證",
  "login.verifying": "驗證中…",
  "login.changePassword": "修改密碼",
  "login.changingPassword": "修改密碼中…",
  "login.sendCode": "傳送重置驗證碼",
  "login.sendingCode": "傳送驗證碼中…",
  "login.google": "使用 Google 繼續",
  "login.phone": "使用手機號繼續",
  "login.noAccount": "還沒有賬戶？",
  "login.createOne": "建立一個",
  "login.forgot": "忘記密碼？",
  "login.wrongAddress": "地址填錯了？",
  "login.startOver": "重新開始",
  "login.haveAccount": "已有賬戶？",
  "login.googleFailed": "Google 登入失敗",
  "login.signInFailed": "登入失敗",
  "login.registerFailed": "註冊失敗",
  "login.codeSent": "我們已向 {mail} 傳送了 6 位驗證碼——在此輸入以完成。",
  "login.resetSent": "如果 {mail} 有賬戶，6 位驗證碼已在路上。",
  "login.passwordChanged": "密碼已修改——請用新密碼登入。",
  "keys.none": "還沒有模型金鑰——OoLu 目前用內建規則回答。貼上 Anthropic 或 OpenAI 的 API 金鑰，給它一個真正的大腦。金鑰在本機加密儲存且不再顯示；只有下方的指紋證明它已錄入。",
  "keys.providerKey": "{provider} 金鑰",
  "keys.fingerprint": "指紋 {mark}",
  "keys.remove": "移除",
  "keys.add": "新增模型金鑰",
  "keys.addDesc": "僅在本機加密儲存——不會同步、不會出現在設定中，也永遠不會被讀出。",
  "keys.paste": "貼上金鑰",
  "keys.addButton": "新增",
  "keys.working": "✓ 正常——模型已應答（{source}）。",
  "keys.notWorking": "✗ {error}",
  "keys.nowDefault": "你的 {provider} 金鑰現在是預設模型。",
  "keys.test": "測試模型",
  "keys.testDesc": "發起一次真實呼叫並確認模型應答——分辨金鑰是否真正可用的可靠辦法。",
  "keys.testButton": "測試連線",
  "keys.testing": "測試中…",
  "pay.title": "支付方式",
  "pay.testBanner": "上線前測試模式——真實交易通道處於關閉狀態。這裡的卡都是命名的測試卡，不會有資金流動。",
  "pay.chargingWhen": "滿足以下條件後開啟扣款：{reasons}。",
  "pay.noCards": "尚無已儲存的卡。",
  "pay.expires": "有效期至 {m}/{y}",
  "pay.makeDefault": "設為預設",
  "pay.remove": "移除",
  "pay.default": "預設",
  "pay.addTestCard": "新增測試卡",
  "pay.addButton": "新增",
  "handiwork": "手工坊",
  "rep.title": "個人代表",
  "rep.intro": "OoLu 可以按你真實的寫作方式，用你的語氣起草回覆。草稿會等待你的決定；自動模式只傳送常規且有依據的回覆——涉及承諾的內容永遠交回給你——並且只有在你的批准記錄足夠好之後才會生效。",
  "rep.mode": "模式",
  "rep.modeDesc": "關閉、草稿建議，或需先贏得信任的自動回覆。",
  "rep.modeUnearned": "自動模式已開啟但尚未贏得信任——在記錄達標前只會起草。",
  "rep.modeOff": "關閉",
  "rep.modeDraft": "草稿",
  "rep.modeAuto": "自動",
  "rep.aboutYou": "關於你",
  "rep.aboutDesc": "草稿所依據的簡短常備說明（角色、語氣、事實）。",
  "rep.aboutPlaceholder": "例如：工程師；回覆簡短",
  "rep.save": "儲存",
  "rep.stats": "已學習 {exchanges} 段對話 · {pending} 份草稿待定 · {verdict} · 聲音：{adapter}",
  "rep.noVerdicts": "尚無裁決",
  "rep.sentAsWritten": "{pct}% 原樣傳送",
  "rep.autoSentCount": "自動傳送 {n} 條",
  "rep.drafts": "草稿",
  "rep.draftsNew": "草稿 · {n} 條新",
  "rep.draftsSub": "以你的語氣寫好的回覆，等你定奪",
  "rep.inboxTitle": "等待你決定的草稿",
  "rep.nothingWaiting": "暫無待處理。",
  "rep.inboxIntro": "當你的代表起草回覆時——無論是在會話中點 ✍，還是自動模式下自行起草——都會送到這裡等你決定。",
  "rep.answering": "回覆 {peer}，所答內容：“{text}”",
  "rep.drafted": "你的代表已起草：",
  "rep.send": "傳送",
  "rep.edit": "編輯",
  "rep.discard": "丟棄",
  "rep.sendEdited": "傳送修改稿",
  "rep.cancel": "取消",
  "rep.editing": "正在編輯草擬的回覆——傳送會記錄你的版本。",
  "rep.draftButton": "用你的語氣起草回覆",
  "friends.who": "你想新增誰？",
  "friends.whoHint": "輸入對方的確切使用者名稱或郵箱——沒有可瀏覽的目錄，除非你給了對方你的名字，否則沒人能找到你。是否接受由對方決定。",
  "friends.usernameOrEmail": "使用者名稱或郵箱",
  "friends.find": "查詢",
  "friends.looking": "查詢中…",
  "friends.sendRequest": "傳送好友請求",
  "friends.requestSent": "請求已傳送——等待對方處理。",
  "friends.openChat": "開啟聊天",
  "friends.accept": "接受",
  "friends.decline": "拒絕",
  "friends.block": "遮蔽",
  "friends.unblock": "取消遮蔽",
  "friends.incoming": "等待你處理的好友請求：",
  "friends.title": "好友與登入",
  "friends.allowNonfriend": "接收非好友的訊息",
  "friends.allowNonfriendDesc": "關閉後只有已接受的好友才能給你發訊息；陌生人必須先傳送好友請求。",
  "friends.setPassword": "設定登入密碼",
  "friends.setPasswordDesc": "如果你是透過 Google 登入的，在此設定密碼，下次也能用使用者名稱+密碼登入。",
  "friends.passwordSet": "密碼已設定——下次可用使用者名稱和它登入。",
  "friends.save": "儲存",
  "friends.myQr": "我的二維碼",
  "friends.scanQr": "掃碼新增",
  "friends.qrHint": "面對面？一人出示二維碼，另一人掃一掃——一步成為好友。",
  "friends.scanning": "將相機對準對方的二維碼…",
  "friends.scanStop": "停止掃碼",
  "friends.cameraError": "無法開啟相機。",
  "friends.rename": "備註名",
  "friends.renameHint": "只有你能看到的備註——比如「會展上認識的安娜」。",
  "friends.namePlaceholder": "備註名（留空恢復使用者名稱）",
  "friends.since": "{date} 成為好友",
  "sec.title": "安全",
  "sec.intro": "OoLu 可以為你下單和預訂——但每一筆都要你重新確認確切金額並輸入認證器應用中的最新驗證碼。會話被盜也無法動用你的錢。",
  "sec.2fa": "雙重認證",
  "sec.2faOn": "已開啟——可以授權訂單。",
  "sec.2faOff": "未開啟——設定後 OoLu 才能下單。",
  "sec.setUp": "設定",
  "sec.disable": "關閉",
  "sec.scanAdd": "將此新增到你的認證器應用",
  "sec.scanDesc": "在 Google Authenticator、Aegis 或 1Password 中輸入此金鑰，然後輸入它顯示的 6 位驗證碼進行確認。",
  "sec.enterCode": "認證器驗證碼",
  "sec.confirm": "確認",
  "sec.ordersWaiting": "等待你批准的訂單：",
  "sec.confirmAmount": "重新輸入確切金額",
  "sec.amount": "金額",
  "sec.authorize": "授權並支付",
  "sec.decline": "拒絕",
  "rep.toggleOn": "✍ 個人代表：開",
  "rep.toggleOff": "✍ 個人代表：關",
  "rep.toggleHint": "用你的語氣為等候中的好友起草回覆——你只需篩選。",
  "rep.autoToPeer": "對 {peer} 的自動回覆（僅限已贏得信任的回覆；涉及承諾的內容永遠等你決定）",
};
