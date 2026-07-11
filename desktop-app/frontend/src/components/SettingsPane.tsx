import { useCallback, useEffect, useState } from "react";
import { accountConsoleUrl, api, signOut } from "../api";
import {
  applyLanguage,
  applyTheme,
  choiceLabel,
  settingDesc,
  settingLabel,
  t,
  unitLabel,
  useT,
} from "../ui";
import type {
  ModelKeyView,
  PaymentProfileView,
  PaymentsStatus,
  SettingItem,
} from "../api";

// The settings node, rendered. Every control is generated from the
// declared catalog — the app never hardcodes a knob, so a control exists
// exactly when (and within the bounds that) the node declares it. Saving
// goes back through the node, which is the same door OoLu uses.

const GROUP_KEYS: Record<string, string> = {
  app: "groupApp",
  account: "groupAccount",
  subscription: "groupSubscription",
  model: "groupModel",
  budget: "groupBudget",
};

export function SettingsPane() {
  const [items, setItems] = useState<SettingItem[]>([]);
  const [error, setError] = useState("");
  const tr = useT(); // re-renders the chrome when the language changes

  const refresh = useCallback(async () => {
    try {
      setItems((await api.settings()).items ?? []);
    } catch (e) {
      setError((e as Error).message);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  // Changes made in the account console (another tab) come back with the
  // user: refreshing on focus is what links the plan change back to OoLu.
  useEffect(() => {
    const onFocus = () => void refresh();
    window.addEventListener("focus", onFocus);
    return () => window.removeEventListener("focus", onFocus);
  }, [refresh]);

  async function save(key: string, value: unknown) {
    setError("");
    try {
      // The node validates; a refusal is shown, never silently accepted.
      setItems((await api.setSettings({ [key]: value })).items);
      // Appearance choices take effect the moment the node accepts them.
      if (key === "app.theme") applyTheme(String(value));
      if (key === "app.language") applyLanguage(String(value));
    } catch (e) {
      setError((e as Error).message);
      void refresh();
    }
  }

  const groups = Array.from(new Set(items.map((i) => i.group)));

  return (
    <div className="settings-pane">
      <div className="convo-group">{tr("settings")}</div>
      {error && <div className="error">{error}</div>}
      {groups.map((group) => (
        <section key={group} className="settings-group">
          <h3>{tr(GROUP_KEYS[group] ?? group)}</h3>
          {group === "subscription" && (
            <p className="muted">{tr("subscriptionNote")}</p>
          )}
          {group === "model" && <p className="muted">{tr("modelNote")}</p>}
          {items
            .filter((i) => i.group === group)
            .map((item) => (
              <SettingRow key={item.key} item={item} onSave={save} />
            ))}
          {group === "subscription" && (
            <div className="setting-row">
              <div className="setting-label">
                <span>{tr("managePlan")}</span>
                <span className="setting-desc">{tr("managePlanDesc")}</span>
              </div>
              <div className="setting-control">
                <button
                  onClick={() => window.open(accountConsoleUrl(), "_blank")}
                >
                  {tr("openConsole")}
                </button>
              </div>
            </div>
          )}
          {group === "model" && <ModelKeysSection />}
        </section>
      ))}
      <PaymentSection />
      <PrivacySection />
    </div>
  );
}

// The data-subject's rights, self-serve: everything as one downloadable
// document, and erasure that spells out what it removes — plus the legal
// documents every host serves at stable public URLs.
export function PrivacySection() {
  const tr = useT();
  const [confirming, setConfirming] = useState(false);
  const [password, setPassword] = useState("");
  const [notice, setNotice] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  async function download() {
    setError("");
    setBusy(true);
    try {
      const data = await api.exportAccount();
      const blob = new Blob([JSON.stringify(data, null, 2)], {
        type: "application/json",
      });
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = "oolu-account-export.json";
      a.click();
      URL.revokeObjectURL(url);
      setNotice("Your data downloaded as oolu-account-export.json.");
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  async function erase() {
    setError("");
    setBusy(true);
    try {
      const result = await api.deleteAccount(password);
      setNotice(result.notes.join(" "));
      signOut();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="settings-group privacy">
      <h3>{tr("privacyData")}</h3>
      <div className="setting-row">
        <div className="setting-label">
          <span>{tr("downloadData")}</span>
          <span className="setting-desc">{tr("downloadDataDesc")}</span>
        </div>
        <div className="setting-control">
          <button disabled={busy} onClick={() => void download()}>
            {tr("download")}
          </button>
        </div>
      </div>
      <div className="setting-row">
        <div className="setting-label">
          <span>{tr("deleteAccount")}</span>
          <span className="setting-desc">{tr("deleteAccountDesc")}</span>
        </div>
        <div className="setting-control row">
          {!confirming ? (
            <button className="linklike" onClick={() => setConfirming(true)}>
              Delete…
            </button>
          ) : (
            <>
              <input
                aria-label="Password to confirm deletion"
                type="password"
                placeholder="your password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
              />
              <button
                disabled={busy || !password}
                onClick={() => void erase()}
              >
                Delete forever
              </button>
              <button
                className="linklike"
                onClick={() => {
                  setConfirming(false);
                  setPassword("");
                  setError("");
                }}
              >
                cancel
              </button>
            </>
          )}
        </div>
      </div>
      <div className="setting-row">
        <div className="setting-label">
          <span>{tr("legal")}</span>
          <span className="setting-desc">{tr("legalDesc")}</span>
        </div>
        <div className="setting-control row">
          <button
            className="linklike"
            onClick={() => window.open("/v1/legal/terms", "_blank")}
          >
            Terms
          </button>
          <button
            className="linklike"
            onClick={() => window.open("/v1/legal/privacy", "_blank")}
          >
            Privacy
          </button>
          <button
            className="linklike"
            onClick={() => window.open("/v1/legal/node-policy", "_blank")}
          >
            Node Policy
          </button>
        </div>
      </div>
      {notice && <div className="muted">{notice}</div>}
      {error && <div className="error">{error}</div>}
    </section>
  );
}

// The BYO-key door: paste a provider API key once, keep only a fingerprint.
const KEY_PROVIDERS = ["anthropic", "openai"];

function ModelKeysSection() {
  const [keys, setKeys] = useState<ModelKeyView[] | null>(null);
  const [provider, setProvider] = useState("anthropic");
  const [draft, setDraft] = useState("");
  const [error, setError] = useState("");
  const [test, setTest] = useState("");
  const [testing, setTesting] = useState(false);

  async function runTest() {
    setTest("");
    setTesting(true);
    try {
      const r = await api.testModelKey();
      setTest(
        r.ok
          ? `✓ working — the model answered (${r.source ?? "model"}).`
          : `✗ ${r.error ?? "the model did not answer"}`,
      );
    } catch (e) {
      setTest(`✗ ${(e as Error).message}`);
    } finally {
      setTesting(false);
    }
  }

  const refresh = useCallback(async () => {
    try {
      const { items } = await api.modelKeys();
      // Model keys not enabled on this host (or an unexpected shape):
      // say nothing at all.
      setKeys(Array.isArray(items) ? items : null);
    } catch {
      setKeys(null);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  if (keys === null) return null;

  return (
    <>
      {keys.length === 0 && (
        <p className="muted">
          No model key yet — OoLu answers with its built-in rules. Paste an
          Anthropic or OpenAI API key to give it a real mind. The key is
          encrypted on this machine and never shown again; only the
          fingerprint below proves it's in.
        </p>
      )}
      {error && <div className="error">{error}</div>}

      {keys.map((k) => (
        <div key={k.provider} className="setting-row">
          <div className="setting-label">
            <span>{k.provider} key</span>
            <span className="setting-desc">fingerprint {k.fingerprint}</span>
          </div>
          <div className="setting-control">
            <button
              className="linklike"
              onClick={async () => {
                await api.removeModelKey(k.provider);
                void refresh();
              }}
            >
              remove
            </button>
          </div>
        </div>
      ))}

      <div className="setting-row">
        <div className="setting-label">
          <span>Add a model key</span>
          <span className="setting-desc">
            Stored encrypted on this machine only — it never syncs, never
            appears in settings, and never comes back out.
          </span>
        </div>
        <div className="setting-control row">
          <select
            aria-label="Key provider"
            value={provider}
            onChange={(e) => setProvider(e.target.value)}
          >
            {KEY_PROVIDERS.map((p) => (
              <option key={p} value={p}>
                {p}
              </option>
            ))}
          </select>
          <input
            type="password"
            aria-label="API key"
            placeholder="paste key"
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
          />
          <button
            disabled={draft.trim().length < 8}
            onClick={async () => {
              setError("");
              setTest("");
              try {
                const r = await api.addModelKey(provider, draft.trim());
                setDraft("");
                await refresh();
                // Confirm it actually answers, right away — no more
                // "billed but is it working?" mystery.
                await runTest();
                if (r.source_switched) {
                  setTest(
                    (t) =>
                      `${t} Your ${provider} key is now the default model.`,
                  );
                }
              } catch (e) {
                setError((e as Error).message);
              }
            }}
          >
            Add
          </button>
        </div>
      </div>

      {keys.length > 0 && (
        <div className="setting-row">
          <div className="setting-label">
            <span>Test the model</span>
            <span className="setting-desc">
              Make one real call and confirm the model answers — the sure
              way to tell a working key from a silent one.
            </span>
            {test && <span className="setting-desc">{test}</span>}
          </div>
          <div className="setting-control">
            <button disabled={testing} onClick={() => void runTest()}>
              {testing ? "Testing…" : "Test connection"}
            </button>
          </div>
        </div>
      )}
    </>
  );
}

// The pre-launch brands the test vault accepts — mirrors billing.TEST_CARDS.
const TEST_BRANDS = ["visa", "mastercard", "amex", "unionpay"];

function PaymentSection() {
  const [profile, setProfile] = useState<PaymentProfileView | null>(null);
  const [status, setStatus] = useState<PaymentsStatus | null>(null);
  const [brand, setBrand] = useState("visa");
  const [error, setError] = useState("");

  const refresh = useCallback(async () => {
    try {
      const wallet = await api.paymentMethods();
      // Payments not enabled (or an unexpected shape): say nothing at all.
      setProfile(Array.isArray(wallet.cards) ? wallet : null);
      setStatus(await api.paymentsStatus());
    } catch {
      setProfile(null);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  if (!profile) return null;

  return (
    <section className="settings-group">
      <h3>Payment methods</h3>
      {profile.mode === "test" && (
        <p className="test-banner">
          Pre-launch test mode — the real transaction port is closed. Cards
          here are named test cards; no money can move.
        </p>
      )}
      {status && !status.open && (
        <p className="muted">
          Charging opens when: {status.reasons.join("; ")}.
        </p>
      )}
      {error && <div className="error">{error}</div>}

      {profile.cards.length === 0 && (
        <p className="muted">No saved cards yet.</p>
      )}
      {profile.cards.map((card) => (
        <div key={card.pm_ref} className="setting-row">
          <div className="setting-label">
            <span>
              {card.brand} •••• {card.last4}
              {profile.default_pm === card.pm_ref ? "  (default)" : ""}
            </span>
            <span className="setting-desc">
              expires {card.exp_month}/{card.exp_year}
            </span>
          </div>
          <div className="setting-control row">
            {profile.default_pm !== card.pm_ref && (
              <button
                className="linklike"
                onClick={async () => {
                  await api.setDefaultCard(card.pm_ref);
                  void refresh();
                }}
              >
                make default
              </button>
            )}
            <button
              className="linklike"
              onClick={async () => {
                await api.removeCard(card.pm_ref);
                void refresh();
              }}
            >
              remove
            </button>
          </div>
        </div>
      ))}

      <div className="setting-row">
        <div className="setting-label">
          <span>Add a test card</span>
          <span className="setting-desc">
            Live mode will confirm real cards with Stripe in your browser —
            card numbers never touch OoLu's servers.
          </span>
        </div>
        <div className="setting-control row">
          <select
            aria-label="Test card brand"
            value={brand}
            onChange={(e) => setBrand(e.target.value)}
          >
            {TEST_BRANDS.map((b) => (
              <option key={b} value={b}>
                {b}
              </option>
            ))}
          </select>
          <button
            onClick={async () => {
              setError("");
              try {
                await api.addTestCard(brand);
                void refresh();
              } catch (e) {
                setError((e as Error).message);
              }
            }}
          >
            Add
          </button>
        </div>
      </div>
    </section>
  );
}

// The legal currency this device's region suggests, from the browser
// locale — a suggestion the user can take with one click, never an
// automatic change.
const REGION_CURRENCY: Record<string, string> = {
  US: "USD", GB: "GBP", JP: "JPY", CN: "CNY", HK: "HKD", SG: "SGD",
  IN: "INR", KR: "KRW", CA: "CAD", AU: "AUD", CH: "CHF", BR: "BRL",
  MX: "MXN", NG: "NGN", KE: "KES", ZA: "ZAR", MW: "MWK",
  AT: "EUR", BE: "EUR", DE: "EUR", ES: "EUR", FI: "EUR", FR: "EUR",
  GR: "EUR", IE: "EUR", IT: "EUR", NL: "EUR", PT: "EUR",
};

export function regionCurrency(locale?: string): string | null {
  try {
    const region = new Intl.Locale(locale ?? navigator.language).maximize()
      .region;
    return region ? (REGION_CURRENCY[region] ?? null) : null;
  } catch {
    return null;
  }
}

function SettingRow({
  item,
  onSave,
}: {
  item: SettingItem;
  onSave: (key: string, value: unknown) => Promise<void>;
}) {
  // The currency row suggests the region's legal currency when it differs.
  const suggested =
    item.key === "account.currency" ? regionCurrency() : null;
  const showSuggestion =
    suggested !== null &&
    suggested !== String(item.value ?? "") &&
    (item.choices ?? []).includes(suggested);
  // The catalog's words, in the interface language (the server's English
  // rides along as the fallback for knobs the dictionary doesn't know).
  const desc = settingDesc(item.key, item.description);
  return (
    <div className="setting-row">
      <div className="setting-label">
        <span>{settingLabel(item.key, item.label)}</span>
        {desc && <span className="setting-desc">{desc}</span>}
        {showSuggestion && (
          <span className="setting-desc">
            {t("regionSuggests")} {suggested}.{" "}
            <button
              type="button"
              className="linklike"
              onClick={() => void onSave(item.key, suggested)}
            >
              {t("use")} {suggested}
            </button>
          </span>
        )}
      </div>
      <div className="setting-control">
        <SettingControl item={item} onSave={onSave} />
        {item.kind === "number" && item.unit && (
          <span className="setting-unit">{unitLabel(item.unit)}</span>
        )}
      </div>
    </div>
  );
}

function SettingControl({
  item,
  onSave,
}: {
  item: SettingItem;
  onSave: (key: string, value: unknown) => Promise<void>;
}) {
  if (item.managed) {
    // Display-only: the value's owner is a dedicated flow, and the node
    // itself refuses writes — this control never even offers one.
    return <span className="managed-value">{String(item.value ?? "")}</span>;
  }
  // Accessible names follow the visible words — the same language.
  const label = settingLabel(item.key, item.label);
  if (item.kind === "bool") {
    return (
      <input
        type="checkbox"
        aria-label={label}
        checked={item.value === true}
        onChange={(e) => void onSave(item.key, e.target.checked)}
      />
    );
  }
  if (item.kind === "choice") {
    return (
      <select
        aria-label={label}
        value={String(item.value ?? "")}
        onChange={(e) => void onSave(item.key, e.target.value)}
      >
        {(item.choices ?? []).map((c) => (
          <option key={c} value={c}>
            {choiceLabel(c)}
          </option>
        ))}
      </select>
    );
  }
  if (item.kind === "number") {
    return (
      <input
        type="number"
        aria-label={label}
        defaultValue={Number(item.value ?? 0)}
        min={item.minimum ?? undefined}
        max={item.maximum ?? undefined}
        onBlur={(e) => void onSave(item.key, Number(e.target.value))}
      />
    );
  }
  return (
    <input
      type="text"
      aria-label={label}
      defaultValue={String(item.value ?? "")}
      maxLength={item.max_length ?? undefined}
      onBlur={(e) => void onSave(item.key, e.target.value)}
    />
  );
}
