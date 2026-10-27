import { ChevronDownIcon, ChevronsUpDownIcon } from "lucide-react";
import { useMemo, useState } from "react";
import type { ModelCaps, ProviderOut } from "@/client/types.gen";
import { Hint } from "@/components/common";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Command, CommandEmpty, CommandGroup, CommandInput, CommandItem, CommandList } from "@/components/ui/command";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuLabel,
  DropdownMenuRadioGroup,
  DropdownMenuRadioItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";

// Mirrors UPSTREAM_PREFIX in yggdrasil/llm/catalog.py: caps reported by the provider's own /models (OpenRouter).
const UPSTREAM_PREFIX = "upstream:";
// The combobox renders at most this many models; typing narrows the rest.
const MAX_SHOWN = 50;

function fmtTokens(n: number): string {
  return n >= 1_000_000 ? `${+(n / 1_000_000).toFixed(1)}M` : n >= 1000 ? `${Math.round(n / 1000)}K` : String(n);
}

export function capsOf(provider: ProviderOut | undefined, model: string | null | undefined): ModelCaps | undefined {
  return model ? provider?.model_caps[model.trim()] : undefined;
}

/** Where the capabilities come from: the provider's own model list, or models.dev. */
export function capsSource(caps: ModelCaps): string {
  return caps.catalog_id.startsWith(UPSTREAM_PREFIX) ? "上游 /models" : "models.dev";
}

/** Whether a model belongs in the picker for `task`: image models only for `image`, image-only models never elsewhere. */
function fitsTask(task: string | undefined, caps: ModelCaps | undefined): boolean {
  if (!task || !caps) return true;
  return task === "image" ? !!caps.image_output : !(caps.image_output && !caps.tools);
}

/**
 * Capability badges from models.dev or the provider's own /models; "能力未知" if the model isn't matched. The source
 * is shown as a line (`showId`) or as a hover hint (default; `hint={false}` drops it, e.g. inside a menu).
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
      {caps.image_output && <Badge variant="secondary">出图</Badge>}
      {caps.context != null && <Badge variant="outline">{fmtTokens(caps.context)} 上下文</Badge>}
      {caps.output != null && <Badge variant="outline">{fmtTokens(caps.output)} 输出</Badge>}
      {showId && <span className="basis-full font-mono text-xs text-muted-foreground">{sourceLine(caps)}</span>}
    </span>
  );
  return showId || !hint ? badges : <Hint content={sourceLine(caps)}>{badges}</Hint>;
}

function sourceLine(caps: ModelCaps): string {
  const id = caps.catalog_id.startsWith(UPSTREAM_PREFIX) ? caps.catalog_id.slice(UPSTREAM_PREFIX.length) : caps.catalog_id;
  return `${capsSource(caps)}: ${id}`;
}

// No "off": Claude 5 can't disable thinking (see _thinking_needs in admin/routes.py).
const EFFORTS = ["minimal", "low", "medium", "high", "xhigh"];
const DEFAULT_EFFORT = "__default__";

/**
 * Model Selector: a searchable combobox over the provider's cached models (with their capabilities), plus an optional
 * thinking-effort menu (`null` = provider default). Any typed name can be used, listed or not. `task` hides models
 * that can't serve it (image-generation models outside `image`, everything else inside it) when caps are known.
 */
export function ModelSelector({
  id,
  provider,
  value,
  onChange,
  effort,
  task,
}: {
  id?: string;
  provider: ProviderOut | undefined;
  value: string;
  onChange: (model: string) => void;
  effort?: { value: string | null; onChange: (effort: string | null) => void };
  task?: string;
}) {
  const [open, setOpen] = useState(false);
  const [search, setSearch] = useState("");
  const models = useMemo(
    () => (provider?.models ?? []).filter((m) => fitsTask(task, provider?.model_caps[m])),
    [provider, task],
  );
  const query = search.trim();
  const matches = useMemo(() => {
    const q = query.toLowerCase();
    return q ? models.filter((m) => m.toLowerCase().includes(q)) : models;
  }, [models, query]);
  const shown = matches.slice(0, MAX_SHOWN);
  const typed = value.trim();
  const caps = capsOf(provider, typed);
  const pick = (model: string) => {
    onChange(model);
    setOpen(false);
  };
  return (
    <div className="grid gap-1.5">
      <div className="flex gap-2">
        <Popover
          // Modal so the list scrolls inside FormDialog (whose scroll lock would otherwise swallow the wheel).
          modal
          open={open}
          onOpenChange={(o) => {
            setOpen(o);
            if (o) setSearch("");
          }}
        >
          <PopoverTrigger asChild>
            <Button
              id={id}
              type="button"
              variant="outline"
              role="combobox"
              aria-expanded={open}
              className="min-w-0 flex-1 justify-between font-normal"
            >
              {typed ? (
                <span className="truncate font-mono text-xs">{typed}</span>
              ) : (
                <span className="truncate text-muted-foreground">
                  {models.length ? "选择或输入模型" : "输入模型名（可先在 Providers 拉取模型列表）"}
                </span>
              )}
              <ChevronsUpDownIcon data-icon="inline-end" className="text-muted-foreground" />
            </Button>
          </PopoverTrigger>
          <PopoverContent className="w-(--radix-popover-trigger-width) min-w-80 p-0" align="start">
            <Command shouldFilter={false}>
              <CommandInput
                aria-label="搜索或输入模型名"
                placeholder="搜索或输入模型名"
                className="font-mono text-xs"
                value={search}
                onValueChange={setSearch}
              />
              <CommandList>
                <CommandEmpty>{models.length ? "没有匹配的模型" : "没有缓存的模型列表，直接输入模型名"}</CommandEmpty>
                {!!shown.length && (
                  <CommandGroup heading={provider?.name}>
                    {shown.map((m) => (
                      <CommandItem key={m} value={m} data-checked={m === typed} onSelect={() => pick(m)}>
                        <div className="grid min-w-0 gap-1">
                          <span className="truncate font-mono text-xs">{m}</span>
                          <CapBadges caps={provider?.model_caps[m]} hint={false} />
                        </div>
                      </CommandItem>
                    ))}
                  </CommandGroup>
                )}
                {matches.length > shown.length && (
                  <p className="px-3 py-2 text-xs text-muted-foreground">
                    还有 {matches.length - shown.length} 个，{query ? "继续输入以缩小范围" : "输入以筛选"}
                  </p>
                )}
                {query && !models.includes(query) && (
                  <CommandGroup>
                    <CommandItem value={`\u0000use:${query}`} onSelect={() => pick(query)}>
                      使用 <span className="truncate font-mono text-xs">{query}</span>
                    </CommandItem>
                  </CommandGroup>
                )}
                {typed && !query && (
                  <CommandGroup>
                    <CommandItem value={"\u0000clear"} onSelect={() => pick("")} className="text-muted-foreground">
                      清空
                    </CommandItem>
                  </CommandGroup>
                )}
              </CommandList>
            </Command>
          </PopoverContent>
        </Popover>
        {effort && (
          <DropdownMenu>
            <DropdownMenuTrigger asChild>
              <Button type="button" variant="outline" aria-label={`思考强度：${effort.value ?? "默认"}`}>
                <span className="text-muted-foreground">思考</span>
                {effort.value ?? "默认"}
                <ChevronDownIcon data-icon="inline-end" />
              </Button>
            </DropdownMenuTrigger>
            <DropdownMenuContent align="end" className="w-56">
              <DropdownMenuLabel>思考强度</DropdownMenuLabel>
              {caps?.reasoning === false && (
                <DropdownMenuLabel className="font-normal">
                  {capsSource(caps)} 标记这个模型不支持思考，只能用默认
                </DropdownMenuLabel>
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
      </div>
      {typed && <CapBadges caps={caps} />}
    </div>
  );
}
