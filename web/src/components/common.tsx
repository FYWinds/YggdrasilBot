import { useQuery } from "@tanstack/react-query";
import { format } from "date-fns";
import { CalendarIcon, ChevronRightIcon, ExternalLinkIcon, InboxIcon, XIcon } from "lucide-react";
import {
  type ComponentProps,
  type FormEvent,
  type ReactNode,
  cloneElement,
  isValidElement,
  useEffect,
  useId,
  useState,
} from "react";
import type { DateRange } from "react-day-picker";
import { zhCN } from "react-day-picker/locale";
import { listGroupsOptions } from "@/client/@tanstack/react-query.gen";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
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
import { Checkbox } from "@/components/ui/checkbox";
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from "@/components/ui/collapsible";
import {
  Dialog,
  DialogClose,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import {
  Empty as EmptyRoot,
  EmptyContent,
  EmptyDescription,
  EmptyHeader,
  EmptyMedia,
  EmptyTitle,
} from "@/components/ui/empty";
import { FieldContent, FieldDescription, FieldError, FieldLabel, Field as UIField } from "@/components/ui/field";
import { Input } from "@/components/ui/input";
import { InputGroup, InputGroupAddon, InputGroupButton, InputGroupInput } from "@/components/ui/input-group";
import { ScrollArea } from "@/components/ui/scroll-area";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Skeleton } from "@/components/ui/skeleton";
import { Spinner } from "@/components/ui/spinner";
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

/** The row of filters under a page header (every list page puts its filters here, each with an aria-label). */
export function FilterBar({ children }: { children: ReactNode }) {
  return <div className="mb-4 flex flex-wrap items-center gap-2">{children}</div>;
}

/** A titled block inside a page or dialog (h2 on pages, h3 in dialogs), with optional actions on the right. */
export function SectionHeader({
  title,
  description,
  actions,
  level = 2,
  className,
}: {
  title: ReactNode;
  description?: ReactNode;
  actions?: ReactNode;
  level?: 2 | 3;
  className?: string;
}) {
  const Heading = level === 2 ? "h2" : "h3";
  return (
    <div className={cn("flex flex-wrap items-end justify-between gap-2", className)}>
      <div>
        <Heading className={cn("font-medium", level === 2 ? "text-lg" : "text-sm")}>{title}</Heading>
        {description && <p className="mt-0.5 text-sm text-muted-foreground">{description}</p>}
      </div>
      {actions && <div className="flex items-center gap-2">{actions}</div>}
    </div>
  );
}

/**
 * A labelled control. The label is linked to the control: a single element child gets the generated `id` unless it
 * sets one (the input wrappers below forward it to their real input). `horizontal` puts a Switch or Checkbox before
 * its label.
 */
export function Field({
  label,
  hint,
  error,
  children,
  className,
  orientation = "vertical",
}: {
  label: ReactNode;
  hint?: ReactNode;
  error?: ReactNode;
  children: ReactNode;
  className?: string;
  orientation?: "vertical" | "horizontal";
}) {
  const generated = useId();
  const own = isValidElement<{ id?: string }>(children) ? children.props.id : undefined;
  const id = own ?? generated;
  const control = isValidElement<{ id?: string }>(children) && own === undefined ? cloneElement(children, { id }) : children;
  const text = (
    <>
      <FieldLabel htmlFor={id} id={`${id}-label`}>
        {label}
      </FieldLabel>
      {hint && <FieldDescription>{hint}</FieldDescription>}
    </>
  );
  return (
    <UIField orientation={orientation} className={className} data-invalid={error ? true : undefined}>
      {orientation === "horizontal" ? (
        <>
          {control}
          <FieldContent>{text}</FieldContent>
        </>
      ) : (
        <>
          <FieldLabel htmlFor={id} id={`${id}-label`}>
            {label}
          </FieldLabel>
          {control}
          {hint && <FieldDescription>{hint}</FieldDescription>}
        </>
      )}
      {error && <FieldError>{error}</FieldError>}
    </UIField>
  );
}

/**
 * A dialog holding a form: Enter submits, the body scrolls under a fixed header and footer, the submit button shows a
 * spinner while `pending`. Without a `description` the dialog opts out of `aria-describedby` explicitly.
 */
export function FormDialog({
  open,
  onOpenChange,
  title,
  description,
  onSubmit,
  pending = false,
  submitLabel = "保存",
  submitDisabled = false,
  children,
  className,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  title: ReactNode;
  description?: ReactNode;
  onSubmit: () => void;
  pending?: boolean;
  submitLabel?: ReactNode;
  submitDisabled?: boolean;
  children: ReactNode;
  className?: string;
}) {
  const submit = (e: FormEvent) => {
    e.preventDefault();
    if (!pending && !submitDisabled) onSubmit();
  };
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent
        className={cn("flex max-h-[90svh] flex-col gap-0 p-0 sm:max-w-lg", className)}
        {...(description ? {} : { "aria-describedby": undefined })}
      >
        <form onSubmit={submit} className="flex min-h-0 flex-1 flex-col">
          <DialogHeader className="p-4 pr-10">
            <DialogTitle>{title}</DialogTitle>
            {description && <DialogDescription>{description}</DialogDescription>}
          </DialogHeader>
          <ScrollArea className="min-h-0 flex-1" viewportClassName="max-h-[calc(90svh-9rem)]">
            <div className="grid gap-4 px-4 pb-4">{children}</div>
          </ScrollArea>
          <DialogFooter className="m-0">
            <DialogClose asChild>
              <Button type="button" variant="outline">
                取消
              </Button>
            </DialogClose>
            <PendingButton type="submit" pending={pending} disabled={submitDisabled}>
              {submitLabel}
            </PendingButton>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

/** A read-only dialog (details): fixed header, scrolling body. */
export function DetailDialog({
  open,
  onOpenChange,
  title,
  description,
  children,
  className,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  title: ReactNode;
  description?: ReactNode;
  children: ReactNode;
  className?: string;
}) {
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent
        className={cn("flex max-h-[90svh] flex-col gap-0 p-0 sm:max-w-2xl", className)}
        {...(description ? {} : { "aria-describedby": undefined })}
      >
        <DialogHeader className="p-4 pr-10">
          <DialogTitle>{title}</DialogTitle>
          {description && <DialogDescription>{description}</DialogDescription>}
        </DialogHeader>
        <ScrollArea className="min-h-0 flex-1" viewportClassName="max-h-[calc(90svh-5rem)]">
          <div className="grid gap-4 px-4 pb-4">{children}</div>
        </ScrollArea>
      </DialogContent>
    </Dialog>
  );
}

/** Button that shows a spinner and stays disabled while its request runs. */
export function PendingButton({
  pending,
  disabled,
  children,
  ...props
}: ComponentProps<typeof Button> & { pending: boolean }) {
  return (
    <Button disabled={pending || disabled} {...props}>
      {pending && <Spinner data-icon="inline-start" />}
      {children}
    </Button>
  );
}

/** Link leaving the console: new tab, fixed `rel`, trailing icon. */
export function ExternalLink({ href, children, className }: { href: string; children?: ReactNode; className?: string }) {
  return (
    <a
      href={href}
      target="_blank"
      rel="noreferrer noopener"
      className={cn("inline-flex items-center gap-1 text-primary underline-offset-4 hover:underline", className)}
    >
      {children ?? href}
      <ExternalLinkIcon className="size-3 shrink-0" aria-hidden />
    </a>
  );
}

/** `value` after it stopped changing for `ms` (search boxes). */
export function useDebounced<T>(value: T, ms = 300): T {
  const [settled, setSettled] = useState(value);
  useEffect(() => {
    const t = setTimeout(() => setSettled(value), ms);
    return () => clearTimeout(t);
  }, [value, ms]);
  return settled;
}

/** Loading skeleton or error alert for a query; renders nothing once data is there. */
export function QueryState({
  isLoading,
  error,
  variant = "rows",
  rows = 5,
}: {
  isLoading?: boolean;
  error?: unknown;
  variant?: "rows" | "card";
  rows?: number;
}) {
  if (isLoading) {
    return variant === "card" ? (
      <Skeleton className="h-48 w-full rounded-xl" aria-label="加载中" />
    ) : (
      <div className="grid gap-2 py-2" aria-label="加载中">
        {Array.from({ length: rows }, (_, i) => (
          <Skeleton key={i} className="h-9 w-full" />
        ))}
      </div>
    );
  }
  if (error) {
    return (
      <Alert variant="destructive" className="my-2">
        <AlertTitle>加载失败</AlertTitle>
        <AlertDescription className="whitespace-pre-wrap">{errorMessage(error)}</AlertDescription>
      </Alert>
    );
  }
  return null;
}

export function Empty({
  children = "暂无数据",
  description,
  icon,
  action,
}: {
  children?: ReactNode;
  description?: ReactNode;
  icon?: ReactNode;
  action?: ReactNode;
}) {
  return (
    <EmptyRoot className="py-8">
      <EmptyHeader>
        <EmptyMedia variant="icon">{icon ?? <InboxIcon />}</EmptyMedia>
        <EmptyTitle>{children}</EmptyTitle>
        {description && <EmptyDescription>{description}</EmptyDescription>}
      </EmptyHeader>
      {action && <EmptyContent>{action}</EmptyContent>}
    </EmptyRoot>
  );
}

/** Free-form list of strings; Enter or comma adds, Backspace on empty removes the last. */
export function TagInput({
  id,
  value,
  onChange,
  placeholder = "输入后回车添加",
}: {
  id?: string;
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
    <InputGroup className="h-auto min-h-8 flex-wrap py-1">
      {value.length > 0 && (
        <InputGroupAddon className="flex-wrap gap-1.5">
          {value.map((tag) => (
            <Badge key={tag} variant="secondary" className="gap-0.5 pr-0.5">
              {tag}
              <InputGroupButton
                size="icon-xs"
                className="size-4"
                aria-label={`移除 ${tag}`}
                onClick={() => onChange(value.filter((t) => t !== tag))}
              >
                <XIcon />
              </InputGroupButton>
            </Badge>
          ))}
        </InputGroupAddon>
      )}
      <InputGroupInput
        id={id}
        className="min-w-24"
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
    </InputGroup>
  );
}

/** Pick any subset of a fixed option list. Inside a `Field`, the group is named by the field's label. */
export function MultiToggle({
  id,
  options,
  value,
  onChange,
}: {
  id?: string;
  options: readonly string[];
  value: string[];
  onChange: (v: string[]) => void;
}) {
  return (
    <ToggleGroup
      type="multiple"
      variant="outline"
      size="sm"
      spacing={1.5}
      className="flex-wrap"
      aria-labelledby={id ? `${id}-label` : undefined}
      value={value}
      onValueChange={onChange}
    >
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
  id,
  value,
  onChange,
  rows = 8,
  onValidityChange,
}: {
  id?: string;
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
        id={id}
        aria-invalid={error ? true : undefined}
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
      {error && <FieldError>JSON 无效：{error}</FieldError>}
    </div>
  );
}

const NONE = "__none__";

/** Select over string options with an optional "none" entry mapped to null. */
export function OptionSelect({
  id,
  value,
  onChange,
  options,
  noneLabel,
  placeholder = "请选择",
  className,
  "aria-label": ariaLabel,
}: {
  id?: string;
  value: string | null;
  onChange: (v: string | null) => void;
  options: { value: string; label: ReactNode }[];
  noneLabel?: string;
  placeholder?: string;
  className?: string;
  "aria-label"?: string;
}) {
  return (
    <Select value={value ?? (noneLabel ? NONE : "")} onValueChange={(v) => onChange(v === NONE ? null : v)}>
      <SelectTrigger id={id} aria-label={ariaLabel} className={cn("w-full", className)}>
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

/** Display name of a group by id: its configured name, else the number. */
export function useGroupName(): (id: number) => string {
  const groups = useQuery(listGroupsOptions());
  return (id) => groups.data?.find((g) => g.group_id === id)?.name || String(id);
}

export function GroupSelect({
  id,
  value,
  onChange,
  allLabel = "全部群",
  className,
}: {
  id?: string;
  value: number | null;
  onChange: (v: number | null) => void;
  allLabel?: string | null;
  className?: string;
}) {
  const groups = useQuery(listGroupsOptions());
  return (
    <OptionSelect
      id={id}
      aria-label={id ? undefined : "群"}
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
  disabled,
  "aria-label": ariaLabel,
}: {
  children: ReactNode;
  title: string;
  description?: ReactNode;
  onConfirm: () => void;
  variant?: "destructive" | "outline" | "default" | "ghost";
  size?: "sm" | "default" | "xs" | "icon-xs" | "icon-sm";
  disabled?: boolean;
  "aria-label"?: string;
}) {
  return (
    <AlertDialog>
      <AlertDialogTrigger asChild>
        <Button variant={variant} size={size} disabled={disabled} aria-label={ariaLabel}>
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
  id,
  value,
  onChange,
  step,
  placeholder,
  className,
  "aria-label": ariaLabel,
}: {
  id?: string;
  value: number | null | undefined;
  onChange: (v: number | null) => void;
  step?: number | "any";
  placeholder?: string;
  className?: string;
  "aria-label"?: string;
}) {
  return (
    <Input
      id={id}
      type="number"
      aria-label={ariaLabel}
      className={className}
      step={step}
      placeholder={placeholder}
      value={value ?? ""}
      onChange={(e) => onChange(e.target.value === "" ? null : Number(e.target.value))}
    />
  );
}

/** Preformatted text or JSON in a scroll box; `viewport` sets its height limit (a static class, e.g. `max-h-60`). */
export function Pre({
  value,
  className,
  viewport = "max-h-80",
}: {
  value: unknown;
  className?: string;
  viewport?: string;
}) {
  return (
    <ScrollArea className={cn("rounded-lg bg-muted", className)} viewportClassName={viewport}>
      <pre className="p-3 font-mono text-xs break-all whitespace-pre-wrap">
        {typeof value === "string" ? value : fmtJson(value)}
      </pre>
    </ScrollArea>
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

/**
 * Write-only API key input for provider forms: shows the stored key's last four characters and, when one is stored,
 * a checkbox to clear it. `onChange` gets the changed form keys; forms with a second key map them.
 */
export function ApiKeyField({
  label = "API key",
  clearLabel = "清除已存储的 key",
  masked,
  value,
  clear,
  emptyHint,
  note,
  onChange,
}: {
  label?: string;
  clearLabel?: string;
  masked?: string | null;
  value: string;
  clear: boolean;
  emptyHint: string;
  /** Shown after the stored-key / empty hint either way. */
  note?: string;
  onChange: (patch: { api_key?: string; clear_api_key?: boolean }) => void;
}) {
  const hint = masked ? `当前 ${masked}；留空保持不变` : emptyHint;
  return (
    <>
      <Field label={label} hint={note ? `${hint}。${note}` : hint}>
        <Input
          type="password"
          autoComplete="off"
          value={value}
          disabled={clear}
          onChange={(e) => onChange({ api_key: e.target.value })}
        />
      </Field>
      {masked && (
        <Field label={clearLabel} orientation="horizontal">
          <Checkbox checked={clear} onCheckedChange={(c) => onChange({ clear_api_key: c === true })} />
        </Field>
      )}
    </>
  );
}
