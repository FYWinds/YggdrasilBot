import { ChevronDownIcon } from "lucide-react";
import type { ModelCaps, ProviderOut } from "@/client/types.gen";
import { Hint } from "@/components/common";
import { Badge } from "@/components/ui/badge";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuLabel,
  DropdownMenuRadioGroup,
  DropdownMenuRadioItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { InputGroup, InputGroupAddon, InputGroupButton, InputGroupInput } from "@/components/ui/input-group";

function fmtTokens(n: number): string {
  return n >= 1_000_000 ? `${+(n / 1_000_000).toFixed(1)}M` : n >= 1000 ? `${Math.round(n / 1000)}K` : String(n);
}

export function capsOf(provider: ProviderOut | undefined, model: string | null | undefined): ModelCaps | undefined {
  return model ? provider?.model_caps[model.trim()] : undefined;
}

/**
 * Capability badges from models.dev; "能力未知" if the model isn't matched. The catalog id is shown as a line
 * (`showId`) or as a hover hint (default; `hint={false}` drops it, e.g. inside a menu).
 */
export function CapBadges({
  caps,
  showId = false,
  hint = true,
}: {
  caps: ModelCaps | undefined;
  showId?: boolean;
  hint?: boolean;
}) {
  if (!caps) return <span className="text-xs text-muted-foreground">能力未知</span>;
  const flags: [boolean, string][] = [
    [caps.vision, "看图"],
    [caps.pdf, "PDF"],
    [caps.tools, "工具"],
    [caps.reasoning, "推理"],
  ];
  const badges = (
    <span className="inline-flex flex-wrap items-center gap-1">
      {flags.map(([on, label]) => (
        <Badge key={label} variant={on ? "secondary" : "outline"} className={on ? "" : "text-muted-foreground line-through"}>
          {label}
        </Badge>
      ))}
      {caps.context != null && <Badge variant="outline">{fmtTokens(caps.context)} 上下文</Badge>}
      {caps.output != null && <Badge variant="outline">{fmtTokens(caps.output)} 输出</Badge>}
      {showId && <span className="basis-full font-mono text-xs text-muted-foreground">{caps.catalog_id}</span>}
    </span>
  );
  return showId || !hint ? badges : <Hint content={`models.dev: ${caps.catalog_id}`}>{badges}</Hint>;
}

// No "off": Claude 5 can't disable thinking (see _thinking_needs in admin/routes.py).
const EFFORTS = ["minimal", "low", "medium", "high", "xhigh"];
const DEFAULT_EFFORT = "__default__";

/**
 * Model Selector: free-text model name with the provider's cached models (and their capabilities) in a menu, plus an
 * optional thinking-effort menu (`null` = provider default). Any name is allowed; typing filters the menu.
 */
export function ModelSelector({
  id,
  provider,
  value,
  onChange,
  effort,
}: {
  id?: string;
  provider: ProviderOut | undefined;
  value: string;
  onChange: (model: string) => void;
  effort?: { value: string | null; onChange: (effort: string | null) => void };
}) {
  const models = provider?.models ?? [];
  const typed = value.trim();
  const caps = capsOf(provider, typed);
  const matching = typed && !models.includes(typed) ? models.filter((m) => m.toLowerCase().includes(typed.toLowerCase())) : [];
  const shown = matching.length ? matching : models;
  return (
    <div className="grid gap-1.5">
      <InputGroup>
        <InputGroupInput
          id={id}
          className="font-mono text-xs"
          value={value}
          placeholder={models.length ? "选择或输入模型" : "输入模型名（可先在 Providers 拉取模型列表）"}
          autoComplete="off"
          onChange={(e) => onChange(e.target.value)}
        />
        <InputGroupAddon align="inline-end">
          <DropdownMenu>
            <DropdownMenuTrigger asChild>
              <InputGroupButton size="icon-xs" aria-label="从模型列表选择" disabled={!models.length}>
                <ChevronDownIcon />
              </InputGroupButton>
            </DropdownMenuTrigger>
            <DropdownMenuContent align="end" className="max-h-80 w-auto max-w-md min-w-64">
              <DropdownMenuLabel>
                {provider?.name}
                {matching.length ? ` · 匹配「${typed}」` : ""}
              </DropdownMenuLabel>
              <DropdownMenuRadioGroup value={typed} onValueChange={onChange}>
                {shown.map((m) => (
                  <DropdownMenuRadioItem key={m} value={m} textValue={m} className="flex-col items-start gap-1">
                    <span className="font-mono text-xs">{m}</span>
                    <CapBadges caps={provider?.model_caps[m]} hint={false} />
                  </DropdownMenuRadioItem>
                ))}
              </DropdownMenuRadioGroup>
            </DropdownMenuContent>
          </DropdownMenu>
          {effort && (
            <DropdownMenu>
              <DropdownMenuTrigger asChild>
                <InputGroupButton variant="outline" aria-label={`思考强度：${effort.value ?? "默认"}`}>
                  <span className="text-muted-foreground">思考</span>
                  {effort.value ?? "默认"}
                  <ChevronDownIcon data-icon="inline-end" />
                </InputGroupButton>
              </DropdownMenuTrigger>
              <DropdownMenuContent align="end" className="w-56">
                <DropdownMenuLabel>思考强度</DropdownMenuLabel>
                {caps?.reasoning === false && (
                  <DropdownMenuLabel className="font-normal">models.dev 标记这个模型不支持思考，只能用默认</DropdownMenuLabel>
                )}
                <DropdownMenuSeparator />
                <DropdownMenuRadioGroup
                  value={effort.value ?? DEFAULT_EFFORT}
                  onValueChange={(v) => effort.onChange(v === DEFAULT_EFFORT ? null : v)}
                >
                  <DropdownMenuRadioItem value={DEFAULT_EFFORT}>默认（不传）</DropdownMenuRadioItem>
                  {EFFORTS.map((e) => (
                    <DropdownMenuRadioItem key={e} value={e}>
                      {e}
                    </DropdownMenuRadioItem>
                  ))}
                </DropdownMenuRadioGroup>
              </DropdownMenuContent>
            </DropdownMenu>
          )}
        </InputGroupAddon>
      </InputGroup>
      {typed && <CapBadges caps={caps} />}
    </div>
  );
}
