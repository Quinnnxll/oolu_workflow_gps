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
  "choice.monthly": { en: "Monthly", zh: "按月", es: "Mensual", fr: "Mensuel" },
  "choice.yearly": { en: "Yearly", zh: "按年", es: "Anual", fr: "Annuel" },
  // Units shown beside number inputs.
  "unit.days": { en: "days", zh: "天", es: "días", fr: "jours" },
  "unit.currency": {
    en: "in your currency",
    zh: "以你的货币计",
    es: "en tu moneda",
    fr: "dans votre devise",
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
export function settingLabel(key: string, fallback: string): string {
  const entry = SETTING_STRINGS[key];
  return entry ? (entry.label[language] ?? fallback) : fallback;
}

export function settingDesc(
  key: string,
  fallback: string | null | undefined,
): string | null {
  const entry = SETTING_STRINGS[key];
  if (entry?.desc) return entry.desc[language] ?? fallback ?? null;
  return fallback ?? null;
}

export function unitLabel(unit: string): string {
  if (STRINGS[`unit.${unit}`]) return t(`unit.${unit}`);
  return unit;
}

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
