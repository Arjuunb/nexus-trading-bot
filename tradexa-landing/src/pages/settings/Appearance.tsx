import { SettingsHeader, Section, SettingRow } from "@/components/settings/primitives";
import { SegmentedControl } from "@/components/ui/SegmentedControl";
import { Switch } from "@/components/ui/Switch";
import { useSettings } from "@/settings/store";
import { cn } from "@/lib/utils";

const ACCENTS = ["#EAB54F", "#2FBF71", "#4F8CFF", "#B072F0", "#E5605B", "#E08D3C"];

export default function Appearance() {
  const { settings, update } = useSettings();
  const a = settings.appearance;

  return (
    <>
      <SettingsHeader title="Appearance" description="Personalise how TradeLogX Nexus looks. Changes apply and save instantly." />

      <div className="space-y-5">
        <Section title="Theme">
          <SettingRow
            label="Color theme"
            description="TradeLogX Nexus is tuned for dark. Light is experimental."
          >
            <SegmentedControl
              value={a.theme}
              onChange={(v) => update("appearance", { theme: v })}
              options={[
                { value: "dark", label: "Dark" },
                { value: "light", label: "Light" },
                { value: "system", label: "System" },
              ]}
            />
          </SettingRow>

          <SettingRow label="Accent color" description="Used for highlights, primary actions and focus.">
            <div className="flex items-center gap-2 sm:justify-end">
              {ACCENTS.map((c) => (
                <button
                  key={c}
                  aria-label={`Accent ${c}`}
                  onClick={() => update("appearance", { accent: c })}
                  className={cn(
                    "h-7 w-7 rounded-full border-2 transition-transform hover:scale-110",
                    a.accent === c ? "border-white" : "border-transparent",
                  )}
                  style={{ background: c }}
                />
              ))}
            </div>
          </SettingRow>
        </Section>

        <Section title="Charts">
          <SettingRow label="Up / profit color">
            <div className="flex items-center gap-2 sm:justify-end">
              <input
                type="color"
                value={a.chartUp}
                onChange={(e) => update("appearance", { chartUp: e.target.value })}
                className="h-8 w-14 cursor-pointer rounded-lg border border-line bg-transparent"
                aria-label="Chart up color"
              />
              <span className="font-mono text-xs text-white/50">{a.chartUp}</span>
            </div>
          </SettingRow>
          <SettingRow label="Down / loss color">
            <div className="flex items-center gap-2 sm:justify-end">
              <input
                type="color"
                value={a.chartDown}
                onChange={(e) => update("appearance", { chartDown: e.target.value })}
                className="h-8 w-14 cursor-pointer rounded-lg border border-line bg-transparent"
                aria-label="Chart down color"
              />
              <span className="font-mono text-xs text-white/50">{a.chartDown}</span>
            </div>
          </SettingRow>
        </Section>

        <Section title="Interface">
          <SettingRow label="Compact mode" description="Tighter spacing and denser tables.">
            <Switch label="Compact mode" checked={a.compact} onChange={(v) => update("appearance", { compact: v })} />
          </SettingRow>
          <SettingRow label="Animations" description="Motion and transitions across the app.">
            <Switch label="Animations" checked={a.animations} onChange={(v) => update("appearance", { animations: v })} />
          </SettingRow>
        </Section>
      </div>
    </>
  );
}
