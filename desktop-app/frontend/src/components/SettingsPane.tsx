import { useCallback, useEffect, useState } from "react";
import { api } from "../api";
import type { PaymentProfileView, PaymentsStatus, SettingItem } from "../api";

// The settings node, rendered. Every control is generated from the
// declared catalog — the app never hardcodes a knob, so a control exists
// exactly when (and within the bounds that) the node declares it. Saving
// goes back through the node, which is the same door OoLu uses.

const GROUP_LABELS: Record<string, string> = {
  app: "App",
  account: "Account",
  subscription: "Subscription",
  budget: "Budget",
};

export function SettingsPane() {
  const [items, setItems] = useState<SettingItem[]>([]);
  const [error, setError] = useState("");

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

  async function save(key: string, value: unknown) {
    setError("");
    try {
      // The node validates; a refusal is shown, never silently accepted.
      setItems((await api.setSettings({ [key]: value })).items);
    } catch (e) {
      setError((e as Error).message);
      void refresh();
    }
  }

  const groups = Array.from(new Set(items.map((i) => i.group)));

  return (
    <div className="settings-pane">
      <div className="convo-group">Settings</div>
      {error && <div className="error">{error}</div>}
      {groups.map((group) => (
        <section key={group} className="settings-group">
          <h3>{GROUP_LABELS[group] ?? group}</h3>
          {group === "subscription" && (
            <p className="muted">
              Billing isn't enabled yet — this records your intended plan.
            </p>
          )}
          {items
            .filter((i) => i.group === group)
            .map((item) => (
              <SettingRow key={item.key} item={item} onSave={save} />
            ))}
        </section>
      ))}
      <PaymentSection />
    </div>
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

function SettingRow({
  item,
  onSave,
}: {
  item: SettingItem;
  onSave: (key: string, value: unknown) => Promise<void>;
}) {
  return (
    <div className="setting-row">
      <div className="setting-label">
        <span>{item.label}</span>
        {item.description && (
          <span className="setting-desc">{item.description}</span>
        )}
      </div>
      <div className="setting-control">
        <SettingControl item={item} onSave={onSave} />
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
  if (item.kind === "bool") {
    return (
      <input
        type="checkbox"
        aria-label={item.label}
        checked={item.value === true}
        onChange={(e) => void onSave(item.key, e.target.checked)}
      />
    );
  }
  if (item.kind === "choice") {
    return (
      <select
        aria-label={item.label}
        value={String(item.value ?? "")}
        onChange={(e) => void onSave(item.key, e.target.value)}
      >
        {(item.choices ?? []).map((c) => (
          <option key={c} value={c}>
            {c}
          </option>
        ))}
      </select>
    );
  }
  if (item.kind === "number") {
    return (
      <input
        type="number"
        aria-label={item.label}
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
      aria-label={item.label}
      defaultValue={String(item.value ?? "")}
      maxLength={item.max_length ?? undefined}
      onBlur={(e) => void onSave(item.key, e.target.value)}
    />
  );
}
