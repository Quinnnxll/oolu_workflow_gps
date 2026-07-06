import { useCallback, useEffect, useState } from "react";
import { api } from "../api";
import type { SettingItem } from "../api";

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
    </div>
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
