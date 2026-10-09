import { useQuery } from "@tanstack/react-query";
import { format } from "date-fns";
import { CalendarIcon, ChevronRightIcon, XIcon } from "lucide-react";
import { type ReactNode, useEffect, useState } from "react";
import type { DateRange } from "react-day-picker";
import { zhCN } from "react-day-picker/locale";
import { listGroupsOptions } from "@/client/@tanstack/react-query.gen";
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
  AlertDialogTrigger,
} from "@/components/ui/alert-dialog";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Calendar } from "@/components/ui/calendar";
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from "@/components/ui/collapsible";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { Textarea } from "@/components/ui/textarea";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group";
import { errorMessage } from "@/lib/api";
import { fmtJson, parseJson } from "@/lib/format";
import { cn } from "@/lib/utils";

export function PageHeader({ title, description, actions }: { title: string; description?: ReactNode; actions?: ReactNode }) {
  return (
    <div className="mb-6 flex flex-wrap items-end justify-between gap-4">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">{title}</h1>
        {description && <p className="mt-1 text-sm text-muted-foreground">{description}</p>}
      </div>
      {actions && <div className="flex items-center gap-2">{actions}</div>}
    </div>
  );
}

export function Field({
  label,
  hint,
  children,
  className,
}: {
  label: ReactNode;
  hint?: ReactNode;
  children: ReactNode;
  className?: string;
}) {
  return (
    <div className={cn("grid gap-1.5", className)}>
      <Label>{label}</Label>
      {children}
      {hint && <p className="text-xs text-muted-foreground">{hint}</p>}
    </div>
  );
}

export function QueryState({ isLoading, error }: { isLoading?: boolean; error?: unknown }) {
  if (isLoading) return <p className="py-6 text-sm text-muted-foreground">加载中…</p>;
  if (error) return <p className="py-6 text-sm whitespace-pre-wrap text-destructive">{errorMessage(error)}</p>;
  return null;
}

export function Empty({ children = "暂无数据" }: { children?: ReactNode }) {
  return <p className="py-6 text-center text-sm text-muted-foreground">{children}</p>;
}

/** Free-form list of strings; Enter or comma adds, Backspace on empty removes the last. */
export function TagInput({
  value,
  onChange,
  placeholder = "输入后回车添加",
}: {
  value: string[];
  onChange: (v: string[]) => void;
  placeholder?: string;
}) {
  const [draft, setDraft] = useState("");
  const add = () => {
    const t = draft.trim();
    if (t && !value.includes(t)) onChange([...value, t]);
    setDraft("");
  };
  return (
    <div className="flex min-h-8 w-full flex-wrap items-center gap-1.5 rounded-lg border border-input px-2 py-1 transition-colors focus-within:border-ring focus-within:ring-3 focus-within:ring-ring/50 dark:bg-input/30">
      {value.map((tag) => (
        <Badge key={tag} variant="secondary" className="gap-1">
          {tag}
          <button type="button" aria-label={`移除 ${tag}`} onClick={() => onChange(value.filter((t) => t !== tag))}>
            <XIcon className="size-3" />
          </button>
        </Badge>
      ))}
      <input
        className="min-w-24 flex-1 bg-transparent text-sm outline-none placeholder:text-muted-foreground"
        value={draft}
        placeholder={placeholder}
        onChange={(e) => setDraft(e.target.value)}
        onBlur={add}
        onKeyDown={(e) => {
          if (e.key === "Enter" || e.key === ",") {
            e.preventDefault();
            add();
          } else if (e.key === "Backspace" && !draft && value.length) {
            onChange(value.slice(0, -1));
          }
        }}
      />
    </div>
  );
}

/** Pick any subset of a fixed option list. */
export function MultiToggle({
  options,
  value,
  onChange,
}: {
  options: readonly string[];
  value: string[];
  onChange: (v: string[]) => void;
}) {
  return (
    <ToggleGroup type="multiple" variant="outline" size="sm" spacing={1.5} className="flex-wrap" value={value} onValueChange={onChange}>
      {options.map((o) => (
        <ToggleGroupItem key={o} value={o}>
          {o}
        </ToggleGroupItem>
      ))}
    </ToggleGroup>
  );
}

/** JSON textarea. Calls onChange only with valid JSON; shows the parse error otherwise. */
export function JsonEditor({
  value,
  onChange,
  rows = 8,
  onValidityChange,
}: {
  value: unknown;
  onChange: (v: unknown) => void;
  rows?: number;
  onValidityChange?: (valid: boolean) => void;
}) {
  const [text, setText] = useState(() => fmtJson(value ?? {}));
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    // Follow external changes unless they are just the echo of what the user typed.
    const parsed = parseJson(text);
    if (!parsed.ok || JSON.stringify(parsed.value) !== JSON.stringify(value ?? {})) {
      setText(fmtJson(value ?? {}));
      setError(null);
    }
  }, [JSON.stringify(value ?? {})]);
  return (
    <div className="grid gap-1">
      <Textarea
        className="font-mono text-xs"
        rows={rows}
        value={text}
        spellCheck={false}
        onChange={(e) => {
          setText(e.target.value);
          const parsed = parseJson(e.target.value);
          setError(parsed.ok ? null : parsed.error);
          onValidityChange?.(parsed.ok);
          if (parsed.ok) onChange(parsed.value);
        }}
      />
      {error && <p className="text-xs text-destructive">JSON 无效：{error}</p>}
    </div>
  );
}

const NONE = "__none__";

/** Select over string options with an optional "none" entry mapped to null. */
export function OptionSelect({
  value,
  onChange,
  options,
  noneLabel,
  placeholder = "请选择",
  className,
}: {
  value: string | null;
  onChange: (v: string | null) => void;
  options: { value: string; label: ReactNode }[];
  noneLabel?: string;
  placeholder?: string;
  className?: string;
}) {
  return (
    <Select value={value ?? (noneLabel ? NONE : "")} onValueChange={(v) => onChange(v === NONE ? null : v)}>
      <SelectTrigger className={cn("w-full", className)}>
        <SelectValue placeholder={placeholder} />
      </SelectTrigger>
      <SelectContent>
        {noneLabel && <SelectItem value={NONE}>{noneLabel}</SelectItem>}
        {options.map((o) => (
          <SelectItem key={o.value} value={o.value}>
            {o.label}
          </SelectItem>
        ))}
      </SelectContent>
    </Select>
  );
}

export function GroupSelect({
  value,
  onChange,
  allLabel = "全部群",
  className,
}: {
  value: number | null;
  onChange: (v: number | null) => void;
  allLabel?: string | null;
  className?: string;
}) {
  const groups = useQuery(listGroupsOptions());
  return (
    <OptionSelect
      className={className}
      value={value === null ? null : String(value)}
      onChange={(v) => onChange(v === null ? null : Number(v))}
      noneLabel={allLabel ?? undefined}
      placeholder="选择群"
      options={(groups.data ?? []).map((g) => ({
        value: String(g.group_id),
        label: g.name ? `${g.name}（${g.group_id}）` : String(g.group_id),
      }))}
    />
  );
}

export function Pager({
  total,
  limit,
  offset,
  onChange,
}: {
  total: number;
  limit: number;
  offset: number;
  onChange: (offset: number) => void;
}) {
  if (total <= limit && offset === 0) return null;
  const page = Math.floor(offset / limit) + 1;
  const pages = Math.max(1, Math.ceil(total / limit));
  return (
    <div className="mt-4 flex items-center justify-end gap-2 text-sm text-muted-foreground">
      <span>
        共 {total} 条 · 第 {page}/{pages} 页
      </span>
      <Button size="sm" variant="outline" disabled={offset === 0} onClick={() => onChange(Math.max(0, offset - limit))}>
        上一页
      </Button>
      <Button size="sm" variant="outline" disabled={offset + limit >= total} onClick={() => onChange(offset + limit)}>
        下一页
      </Button>
    </div>
  );
}

export function ConfirmButton({
  children,
  title,
  description,
  onConfirm,
  variant = "destructive",
  size = "sm",
}: {
  children: ReactNode;
  title: string;
  description?: ReactNode;
  onConfirm: () => void;
  variant?: "destructive" | "outline" | "default" | "ghost";
  size?: "sm" | "default" | "xs";
}) {
  return (
    <AlertDialog>
      <AlertDialogTrigger asChild>
        <Button variant={variant} size={size}>
          {children}
        </Button>
      </AlertDialogTrigger>
      <AlertDialogContent>
        <AlertDialogHeader>
          <AlertDialogTitle>{title}</AlertDialogTitle>
          {description && <AlertDialogDescription>{description}</AlertDialogDescription>}
        </AlertDialogHeader>
        <AlertDialogFooter>
          <AlertDialogCancel>取消</AlertDialogCancel>
          <AlertDialogAction onClick={onConfirm}>确定</AlertDialogAction>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  );
}

export function NumberInput({
  value,
  onChange,
  step,
  placeholder,
  className,
}: {
  value: number | null | undefined;
  onChange: (v: number | null) => void;
  step?: number | "any";
  placeholder?: string;
  className?: string;
}) {
  return (
    <Input
      type="number"
      className={className}
      step={step}
      placeholder={placeholder}
      value={value ?? ""}
      onChange={(e) => onChange(e.target.value === "" ? null : Number(e.target.value))}
    />
  );
}

export function Pre({ value, className }: { value: unknown; className?: string }) {
  return (
    <pre className={cn("max-h-80 overflow-auto rounded-lg bg-muted p-3 text-xs", className)}>
      {typeof value === "string" ? value : fmtJson(value)}
    </pre>
  );
}

/** Hover hint for a single element (instead of the native title attribute). */
export function Hint({ content, children }: { content: ReactNode; children: ReactNode }) {
  if (!content) return children;
  return (
    <Tooltip>
      <TooltipTrigger asChild>{children}</TooltipTrigger>
      <TooltipContent className="max-w-md whitespace-pre-wrap">{content}</TooltipContent>
    </Tooltip>
  );
}

/** Collapsed-by-default section with a clickable label (instead of <details>). */
export function Disclosure({ label, children }: { label: ReactNode; children: ReactNode }) {
  const [open, setOpen] = useState(false);
  return (
    <Collapsible open={open} onOpenChange={setOpen}>
      <CollapsibleTrigger asChild>
        <Button variant="ghost" size="sm" className="-ml-2 text-muted-foreground">
          <ChevronRightIcon className={cn("transition-transform", open && "rotate-90")} />
          {label}
        </Button>
      </CollapsibleTrigger>
      <CollapsibleContent className="mt-2">{children}</CollapsibleContent>
    </Collapsible>
  );
}

/** Day range; `from`/`to` are whole days (callers widen `to` to its end of day). */
export function DateRangePicker({
  value,
  onChange,
  placeholder = "选择日期范围",
  className,
}: {
  value: DateRange | undefined;
  onChange: (v: DateRange | undefined) => void;
  placeholder?: string;
  className?: string;
}) {
  const label = value?.from
    ? `${format(value.from, "yyyy-MM-dd")} — ${value.to ? format(value.to, "yyyy-MM-dd") : "…"}`
    : placeholder;
  return (
    <div className={cn("flex items-center gap-1", className)}>
      <Popover>
        <PopoverTrigger asChild>
          <Button variant="outline" className={cn("w-64 justify-start font-normal", !value?.from && "text-muted-foreground")}>
            <CalendarIcon />
            {label}
          </Button>
        </PopoverTrigger>
        <PopoverContent className="w-auto p-0" align="start">
          <Calendar mode="range" numberOfMonths={2} locale={zhCN} selected={value} onSelect={onChange} />
        </PopoverContent>
      </Popover>
      {value?.from && (
        <Button variant="ghost" size="icon" aria-label="清除日期" onClick={() => onChange(undefined)}>
          <XIcon />
        </Button>
      )}
    </div>
  );
}
