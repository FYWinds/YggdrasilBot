import { useQuery } from "@tanstack/react-query";
import {
  ArrowUpRightIcon,
  BotIcon,
  BrainIcon,
  CircleXIcon,
  GlobeIcon,
  ImageIcon,
  InfoIcon,
  MessagesSquareIcon,
  ScrollTextIcon,
  SearchIcon,
  TerminalIcon,
  TriangleAlertIcon,
  WrenchIcon,
} from "lucide-react";
import { type ReactNode, createContext, useContext, useMemo, useState } from "react";
import { listProvidersOptions } from "@/client/@tanstack/react-query.gen";
import type {
  BotReply,
  Entry,
  HitsDisplay,
  OpsReply,
  PageDisplay,
  QqContextDisplay,
  QqMessage,
  ToolCallPart,
  TranscriptOut,
} from "@/client/types.gen";
import { Disclosure, Empty, ExternalLink, Hint } from "@/components/common";
import { type Names, QQForwardCard, QQMessageList, QQMessageView, ReplyView, type Segment } from "@/components/qq";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Bubble, BubbleContent } from "@/components/ui/bubble";
import { Button } from "@/components/ui/button";
import {
  CodeBlock,
  CodeBlockActions,
  CodeBlockContent,
  CodeBlockCopyButton,
  CodeBlockHeader,
  CodeBlockLine,
  CodeBlockTitle,
} from "@/components/ui/code-block";
import { HoverCard, HoverCardContent, HoverCardTrigger } from "@/components/ui/hover-card";
import { Message, MessageContent, MessageHeader } from "@/components/ui/message";
import { Progress } from "@/components/ui/progress";
import { ScrollArea } from "@/components/ui/scroll-area";
import { Spinner } from "@/components/ui/spinner";
import {
  Trace,
  TraceCollapsible,
  TraceCollapsibleContent,
  TraceCollapsibleTrigger,
  TraceDescription,
  TraceIcon,
  TraceLine,
  TraceTitle,
} from "@/components/ui/trace";
import { useHighlightedCode } from "@/hooks/use-highlighted-code";
import { fmtJson, fmtTime } from "@/lib/format";
import { cn } from "@/lib/utils";

type Part = NonNullable<Entry["parts"]>[number];
type ImagePart = Extract<Part, { type?: "image" }>;

/** Tools whose call is a script and whose result is its output (shown as a terminal). */
const TERMINAL: Record<string, true> = { bash: true, python: true, sh: true, py: true, background: true, eval: true };
/** Transcript images (prompt, tool results, renders) by sha256. */
const BLOB_URL = "/api/transcripts/blobs/";
/** How many recent chat messages the user turn shows before "展开". */
const RECENT_SHOWN = 8;

export function fmtTokens(n: number): string {
  return n >= 1000 ? `${(n / 1000).toFixed(1)}k` : String(n);
}

/** `12.3s`, `2m05s`, `1h04m`. */
export function fmtMs(ms: number): string {
  if (ms < 60_000) return `${(ms / 1000).toFixed(1)}s`;
  const s = Math.round(ms / 1000);
  if (s < 3600) return `${Math.floor(s / 60)}m${String(s % 60).padStart(2, "0")}s`;
  return `${Math.floor(s / 3600)}h${String(Math.floor((s % 3600) / 60)).padStart(2, "0")}m`;
}

type Ctx = {
  start: number;
  live: boolean;
  names: Names;
  messages: Record<string, QqMessage>;
  lookup: (qqMessageId: number) => QqMessage | undefined;
  bot?: { user_id: number; name: string } | null;
};

const TranscriptContext = createContext<Ctx | null>(null);

function useTranscript(): Ctx {
  const ctx = useContext(TranscriptContext);
  if (!ctx) throw new Error("outside <Transcript>");
  return ctx;
}

/** The text parts joined; `missing` when content capture was off (texts are null, only sizes kept). */
function joinText(parts: Part[]): { text: string; chars: number; missing: boolean } {
  let text = "";
  let chars = 0;
  let missing = false;
  for (const p of parts) {
    if (p.type !== "text") continue;
    chars += p.chars ?? 0;
    if (p.text == null) missing = true;
    else text += (text ? "\n\n" : "") + p.text;
  }
  return { text, chars, missing };
}

function imagesOf(parts: Part[]): ImagePart[] {
  return parts.filter((p): p is ImagePart => p.type === "image");
}

/** Time since the run started; the full time on hover. */
function At({ at }: { at?: string | null }) {
  const { start } = useTranscript();
  if (!at) return null;
  return (
    <Hint content={fmtTime(at)}>
      <time dateTime={at} className="shrink-0 text-xs leading-5 text-muted-foreground tabular-nums">
        {fmtMs(Math.max(0, Date.parse(at) - start))}
      </time>
    </Hint>
  );
}

function Missing({ chars, what = "内容" }: { chars?: number; what?: string }) {
  return (
    <span className="text-xs text-muted-foreground italic">
      {what}未记录（{chars ?? 0} 字）
    </span>
  );
}

/** Code or text with highlighting, a copy button and an optional title. `className` sets the height limit. */
export function Code({
  value,
  language = "text",
  title,
  variant = "default",
  className = "max-h-80",
}: {
  value: string;
  language?: string;
  title?: ReactNode;
  variant?: "default" | "destructive";
  className?: string;
}) {
  const lines = useHighlightedCode(value, language);
  const copy = (
    <CodeBlockActions>
      <CodeBlockCopyButton value={value} aria-label="复制" />
    </CodeBlockActions>
  );
  return (
    <CodeBlock size="sm" variant={variant}>
      {title ? (
        <CodeBlockHeader>
          <CodeBlockTitle>{title}</CodeBlockTitle>
          {copy}
        </CodeBlockHeader>
      ) : (
        copy
      )}
      <CodeBlockContent wrap className={className}>
        {lines.map((line, i) => (
          <CodeBlockLine key={i}>{line}</CodeBlockLine>
        ))}
      </CodeBlockContent>
    </CodeBlock>
  );
}

function Images({ items }: { items: { src?: string | null; caption?: string; bytes?: number }[] }) {
  return (
    <div className="grid grid-cols-[repeat(auto-fill,minmax(10rem,1fr))] gap-2">
      {items.map((img, i) => (
        <figure key={i} className="grid content-start gap-1">
          {img.src ? (
            <a href={img.src} target="_blank" rel="noreferrer noopener" className="block overflow-hidden rounded-lg border bg-muted">
              <img src={img.src} alt={img.caption || "图片"} loading="lazy" className="max-h-64 w-full object-contain" />
            </a>
          ) : (
            <div className="flex h-24 items-center justify-center gap-1 rounded-lg border border-dashed text-xs text-muted-foreground">
              <ImageIcon className="size-4" aria-hidden />
              图片未记录{img.bytes ? `（${fmtTokens(img.bytes)} B）` : ""}
            </div>
          )}
          {img.caption && <figcaption className="text-xs text-muted-foreground">{img.caption}</figcaption>}
        </figure>
      ))}
    </div>
  );
}

function partImages(parts: ImagePart[]) {
  return parts.map((p) => ({ src: p.blob ? BLOB_URL + p.blob : p.url, bytes: p.bytes }));
}

/** A line on the left that opens into `children`, its time on the right. */
function Row({ at, line, children, defaultOpen }: { at?: string | null; line: ReactNode; children?: ReactNode; defaultOpen?: boolean }) {
  if (!children) {
    return (
      <div className="flex items-start gap-2">
        <div className="min-w-0 flex-1">{line}</div>
        <At at={at} />
      </div>
    );
  }
  return (
    <TraceCollapsible defaultOpen={defaultOpen}>
      <div className="flex items-start gap-2">
        <div className="min-w-0 flex-1">
          <TraceCollapsibleTrigger>{line}</TraceCollapsibleTrigger>
        </div>
        <At at={at} />
      </div>
      <TraceCollapsibleContent className="grid gap-2 text-foreground">{children}</TraceCollapsibleContent>
    </TraceCollapsible>
  );
}

function SystemEntry({ entry }: { entry: Entry }) {
  const { text, chars, missing } = joinText(entry.parts ?? []);
  return (
    <Row
      at={entry.at}
      line={
        <TraceLine>
          <TraceIcon>
            <ScrollTextIcon />
          </TraceIcon>
          <TraceTitle>系统提示词</TraceTitle>
          <TraceDescription>{chars} 字</TraceDescription>
        </TraceLine>
      }
    >
      {missing ? <Missing chars={chars} /> : <Code value={text} language="markdown" className="max-h-[32rem]" />}
    </Row>
  );
}

/** The exact prompt parts the model received (text and images). */
function PromptParts({ parts }: { parts: Part[] }) {
  const { text, chars, missing } = joinText(parts);
  const images = imagesOf(parts);
  return (
    <div className="grid gap-2">
      {missing ? <Missing chars={chars} /> : text && <Code value={text} language="markdown" className="max-h-[32rem]" />}
      {images.length > 0 && <Images items={partImages(images)} />}
    </div>
  );
}

function SubHeading({ children, actions }: { children: ReactNode; actions?: ReactNode }) {
  return (
    <div className="flex items-center justify-between gap-2">
      <h3 className="text-xs font-medium text-muted-foreground">{children}</h3>
      {actions}
    </div>
  );
}

/** The user turn of an invocation as the group saw it: recent chat, what the quoted message replies to, the quoted
 * message and the trigger. */
function QQContext({ entry, display }: { entry: Entry; display: QqContextDisplay }) {
  const { messages, names, lookup } = useTranscript();
  const [all, setAll] = useState(false);
  const chain = display.chain ?? [];
  const own = new Set([display.trigger?.id, display.quoted?.id, ...chain.map((m) => m.id)].filter((id) => id != null));
  const recent = (display.recent ?? [])
    .map((id) => messages[String(id)])
    .filter((m): m is QqMessage => !!m && !(m.id != null && own.has(m.id)));
  const hidden = all ? 0 : Math.max(0, recent.length - RECENT_SHOWN);
  return (
    <section aria-label="群聊上下文" className="grid gap-3 rounded-xl border bg-muted/30 p-3">
      <div className="flex items-center gap-2 text-xs text-muted-foreground">
        <MessagesSquareIcon className="size-3.5" aria-hidden />
        <span className="font-medium text-foreground">群聊上下文</span>
        {!!display.blocks?.length && <span className="truncate">提示词包含：{display.blocks.join("、")}</span>}
        <span className="ml-auto">
          <At at={entry.at} />
        </span>
      </div>
      {recent.length > 0 && (
        <div className="grid gap-2">
          <SubHeading
            actions={
              hidden > 0 && (
                <Button variant="ghost" size="xs" onClick={() => setAll(true)}>
                  展开更早的 {hidden} 条
                </Button>
              )
            }
          >
            最近群聊（{recent.length}）
          </SubHeading>
          <QQMessageList messages={recent.slice(hidden)} names={names} lookup={lookup} />
        </div>
      )}
      {chain.length > 0 && (
        <div className="grid gap-2">
          <SubHeading>引用的消息回复的</SubHeading>
          <QQMessageList messages={chain} names={names} lookup={lookup} />
        </div>
      )}
      {display.quoted && (
        <div className="grid gap-2">
          <SubHeading>引用的消息</SubHeading>
          <QQMessageView message={display.quoted} names={names} lookup={lookup} />
        </div>
      )}
      {display.trigger && (
        <div className="grid gap-2">
          <SubHeading>触发消息</SubHeading>
          <QQMessageView message={display.trigger} names={names} lookup={lookup} highlight />
        </div>
      )}
      <Disclosure label="查看原始提示词">
        <PromptParts parts={entry.parts ?? []} />
      </Disclosure>
    </section>
  );
}

function UserEntry({ entry, source }: { entry: Entry; source: string }) {
  const parts = entry.parts ?? [];
  if (entry.display?.kind === "qq_context") return <QQContext entry={entry} display={entry.display} />;
  const { text, chars, missing } = joinText(parts);
  const images = imagesOf(parts);
  return (
    <Message align="end">
      <MessageContent>
        <MessageHeader className="justify-end gap-2 px-0">
          {source === "command" ? "脚本" : "用户"}
          <At at={entry.at} />
        </MessageHeader>
        {source === "command" && !missing ? (
          <Code value={text} language="bash" />
        ) : (
          (text || missing) && (
            <Bubble align="end" variant="muted">
              <BubbleContent className="whitespace-pre-wrap">{missing ? <Missing chars={chars} /> : text}</BubbleContent>
            </Bubble>
          )
        )}
        {images.length > 0 && <Images items={partImages(images)} />}
      </MessageContent>
    </Message>
  );
}

function Thinking({ part }: { part: Extract<Part, { type?: "thinking" }> }) {
  const line = (
    <TraceLine>
      <TraceIcon>
        <BrainIcon />
      </TraceIcon>
      <TraceTitle>{part.redacted ? "思考（已加密）" : "思考"}</TraceTitle>
      {!!part.chars && <TraceDescription>{part.chars} 字</TraceDescription>}
    </TraceLine>
  );
  if (part.redacted) return line;
  return (
    <TraceCollapsible>
      <TraceCollapsibleTrigger>{line}</TraceCollapsibleTrigger>
      <TraceCollapsibleContent className="whitespace-pre-wrap">
        {part.text == null ? <Missing chars={part.chars} /> : part.text}
      </TraceCollapsibleContent>
    </TraceCollapsible>
  );
}

function commandOf(args: ToolCallPart["args"]): string | null {
  for (const key of ["command", "script", "code", "cmd"]) {
    const v = args?.[key];
    if (typeof v === "string") return v;
  }
  return null;
}

function argSummary(call: ToolCallPart | undefined): string {
  const v = call?.args && Object.values(call.args).find((x) => typeof x === "string" || typeof x === "number");
  if (v == null) return "";
  const s = String(v).replace(/\s+/g, " ").trim();
  return s.length > 80 ? `${s.slice(0, 80)}…` : s;
}

function Hits({ display }: { display: HitsDisplay }) {
  const hits = display.hits ?? [];
  return (
    <div className="grid gap-2">
      <div className="text-xs text-muted-foreground">
        搜索「{display.query}」· {hits.length} 条结果
      </div>
      {hits.map((h, i) => (
        <div key={i} className="grid gap-0.5">
          <a href={h.url} target="_blank" rel="noreferrer noopener" className="w-fit max-w-full">
            <TraceLine>
              <TraceIcon>
                <GlobeIcon />
              </TraceIcon>
              <TraceTitle>
                {h.title || h.url}
                <span className="sr-only">（在新标签页打开）</span>
              </TraceTitle>
              <TraceIcon>
                <ArrowUpRightIcon />
              </TraceIcon>
            </TraceLine>
          </a>
          {h.snippet && <p className="line-clamp-3 pl-6 text-xs text-muted-foreground">{h.snippet}</p>}
        </div>
      ))}
    </div>
  );
}

function PageCard({ display: d }: { display: PageDisplay }) {
  const href = d.final_url || d.url;
  return (
    <div className="grid gap-1 rounded-lg border p-3">
      <div className="flex min-w-0 items-center gap-2">
        <GlobeIcon className="size-4 shrink-0 text-muted-foreground" aria-hidden />
        <span className="truncate font-medium">{d.title || href}</span>
      </div>
      {/^https?:\/\//.test(href) ? (
        <ExternalLink href={href} className="text-xs break-all">
          {href}
        </ExternalLink>
      ) : (
        <span className="font-mono text-xs">{href}</span>
      )}
      <div className="flex flex-wrap items-center gap-1.5 text-xs text-muted-foreground">
        {d.via && <Badge variant="outline">{d.via}</Badge>}
        {(d.pages ?? 1) > 1 && (
          <Badge variant="outline">
            第 {d.page ?? 1}/{d.pages} 页
          </Badge>
        )}
        {d.final_url && d.final_url !== d.url && <span className="break-all">请求：{d.url}</span>}
      </div>
    </div>
  );
}

/** A tool result by what it holds: QQ chat, a forward, search hits, a page, images, terminal output or text. */
function ToolResult({ entry, terminal }: { entry: Entry; terminal: boolean }) {
  const { messages, names, lookup } = useTranscript();
  const parts = entry.parts ?? [];
  const { text, chars, missing } = joinText(parts);
  const images = imagesOf(parts);
  const d = entry.display;
  const failed = entry.outcome === "error" || entry.outcome === "retry";
  let rich: ReactNode = null;
  if (d?.kind === "qq_messages") {
    const list = (d.message_ids ?? []).map((id) => messages[String(id)]).filter((m): m is QqMessage => !!m);
    rich = list.length ? (
      <ScrollArea className="rounded-lg border" viewportClassName="max-h-[32rem]">
        <div className="p-2">
          <QQMessageList messages={list} names={names} lookup={lookup} />
        </div>
      </ScrollArea>
    ) : (
      <p className="text-xs text-muted-foreground">消息没有保存</p>
    );
  } else if (d?.kind === "qq_forward") {
    rich = (
      <QQForwardCard
        names={names}
        nodes={(d.nodes ?? []).map((n) => ({
          userId: n.user_id ?? 0,
          name: n.sender_name ?? "",
          sentAt: n.sent_at,
          segments: (n.segments ?? []) as Segment[],
        }))}
      />
    );
  } else if (d?.kind === "hits") {
    rich = <Hits display={d} />;
  } else if (d?.kind === "page") {
    rich = <PageCard display={d} />;
  } else if (d?.kind === "images") {
    rich = <Images items={(d.images ?? []).map((i) => ({ src: BLOB_URL + i.blob, caption: i.caption }))} />;
  }
  const title =
    entry.outcome === "retry" ? "要求重试" : entry.outcome === "error" ? "错误" : terminal ? "输出" : d?.kind === "page" ? "正文" : "结果";
  const raw = missing ? (
    <Missing chars={chars} what="结果" />
  ) : text ? (
    <Code
      title={title}
      value={text}
      language={terminal ? "ansi" : /^\s*[[{]/.test(text) ? "json" : "text"}
      variant={entry.outcome === "error" ? "destructive" : "default"}
    />
  ) : null;
  return (
    <>
      {rich}
      {images.length > 0 && <Images items={partImages(images)} />}
      {rich && raw && !failed && d?.kind !== "page" ? <Disclosure label="模型收到的文本">{raw}</Disclosure> : raw}
      {!rich && !raw && !images.length && <p className="text-xs text-muted-foreground">没有输出</p>}
    </>
  );
}

function toolIcon(entry: Entry | undefined, terminal: boolean): ReactNode {
  if (terminal) return <TerminalIcon />;
  const kind = entry?.display?.kind;
  if (kind === "hits") return <SearchIcon />;
  if (kind === "page") return <GlobeIcon />;
  if (kind === "qq_messages" || kind === "qq_forward") return <MessagesSquareIcon />;
  if (kind === "images" || (entry && imagesOf(entry.parts ?? []).length)) return <ImageIcon />;
  return <WrenchIcon />;
}

/** One tool call from arguments to result (AI Elements "Tool"; shell tools as "Terminal"). */
function ToolCard({ call, entry }: { call?: ToolCallPart; entry?: Entry }) {
  const { live } = useTranscript();
  const name = call?.name ?? entry?.name ?? "工具";
  const terminal = !!TERMINAL[name];
  const command = terminal ? commandOf(call?.args) : null;
  const outcome = entry?.outcome;
  const status = !entry
    ? live
      ? "running"
      : "pending"
    : outcome === "error"
      ? "error"
      : outcome === "cancelled"
        ? "pending"
        : undefined;
  const badge = !entry
    ? live
      ? "运行中"
      : "没有结果"
    : outcome === "retry"
      ? "要求重试"
      : outcome === "cancelled"
        ? "已取消"
        : outcome === "error"
          ? "出错"
          : null;
  const summary = command ? command.trim().split("\n")[0] : argSummary(call);
  // Tool cards start collapsed (the line shows name, status and duration); only a result without its call — a
  // command job's output, the whole point of that transcript — starts open.
  const open = !call;
  return (
    <Row
      at={entry?.at}
      defaultOpen={open}
      line={
        <TraceLine status={status}>
          <TraceIcon>{status === "running" ? <Spinner /> : toolIcon(entry, terminal)}</TraceIcon>
          <TraceTitle>
            <span className="font-medium">{name}</span>
            {summary && <span className={cn("ml-1.5", command && "font-mono text-xs")}>{summary}</span>}
          </TraceTitle>
          {badge && <Badge variant={outcome === "error" ? "destructive" : "outline"}>{badge}</Badge>}
          {call && !call.args && !!call.chars && <TraceDescription>参数 {call.chars} 字</TraceDescription>}
          {entry?.duration_ms != null && <TraceDescription>{fmtMs(entry.duration_ms)}</TraceDescription>}
        </TraceLine>
      }
    >
      {command ? (
        <Code title={name === "python" || name === "py" ? "代码" : "命令"} value={command} language={name === "python" || name === "py" ? "python" : "bash"} />
      ) : call?.args ? (
        <Code title="参数" value={fmtJson(call.args)} language="json" className="max-h-60" />
      ) : (
        call && <Missing chars={call.chars} what="参数" />
      )}
      {entry && <ToolResult entry={entry} terminal={terminal} />}
    </Row>
  );
}

/** The run's structured answer: `final_result` arguments, or the text the model wrote it as (an `output` part). */
function Answer({ value, chars }: { value?: Record<string, unknown> | null; chars?: number }) {
  const { bot } = useTranscript();
  return value ? (
    <ReplyView reply={value as BotReply | OpsReply} botName={bot?.name} botId={bot?.user_id} defaultOpen />
  ) : (
    <Missing chars={chars} what="回复" />
  );
}

function FinalResult({ call, entry }: { call: ToolCallPart; entry?: Entry }) {
  return (
    <>
      <Answer value={call.args} chars={call.chars} />
      {entry && entry.outcome !== "ok" && (
        <Trace>
          <ToolCard call={call} entry={entry} />
        </Trace>
      )}
    </>
  );
}

function UsageText({ usage }: { usage: NonNullable<Entry["usage"]> }) {
  return (
    <Hint content={`输入 ${usage.input ?? 0}（缓存 ${usage.cached ?? 0}）· 输出 ${usage.output ?? 0} tokens`}>
      <span className="tabular-nums">
        ↑{fmtTokens(usage.input ?? 0)}
        {!!usage.cached && ` (${fmtTokens(usage.cached)} 缓存)`} ↓{fmtTokens(usage.output ?? 0)}
      </span>
    </Hint>
  );
}

function AssistantEntry({ entry, results }: { entry: Entry; results: Map<string, Entry> }) {
  // Consecutive tool calls share one trace block; the final answer renders as the reply itself.
  const blocks: (Part | ToolCallPart[])[] = [];
  for (const p of entry.parts ?? []) {
    const last = blocks[blocks.length - 1];
    if (p.type === "tool_call" && p.name !== "final_result") {
      if (Array.isArray(last)) last.push(p);
      else blocks.push([p]);
    } else blocks.push(p);
  }
  return (
    <Message>
      <MessageContent className="gap-3">
        <MessageHeader className="gap-2 px-0">
          <BotIcon className="size-3.5" aria-hidden />
          <span className="font-mono">{entry.model || "模型"}</span>
          {entry.duration_ms != null && <span className="tabular-nums">{fmtMs(entry.duration_ms)}</span>}
          {entry.usage && <UsageText usage={entry.usage} />}
          {entry.finish && !["stop", "tool_call", "end_turn", "toolUse", "tool_use"].includes(entry.finish) && (
            <Badge variant="outline">{entry.finish}</Badge>
          )}
          <span className="ml-auto">
            <At at={entry.at} />
          </span>
        </MessageHeader>
        {blocks.map((b, i) => {
          if (Array.isArray(b)) {
            return (
              <Trace key={i}>
                {b.map((call) => (
                  <ToolCard key={call.id} call={call} entry={results.get(call.id)} />
                ))}
              </Trace>
            );
          }
          if (b.type === "thinking") return <Thinking key={i} part={b} />;
          if (b.type === "tool_call") return <FinalResult key={i} call={b} entry={results.get(b.id)} />;
          if (b.type === "output") return <Answer key={i} value={b.value} chars={b.chars} />;
          if (b.type === "image") return <Images key={i} items={partImages([b])} />;
          if (b.type !== "text") return null;
          return (
            <div key={i} className="whitespace-pre-wrap">
              {b.text == null ? <Missing chars={b.chars} /> : b.text}
            </div>
          );
        })}
        {entry.error && (
          <Alert variant="destructive">
            <CircleXIcon />
            <AlertTitle>模型请求失败</AlertTitle>
            <AlertDescription className="whitespace-pre-wrap">{entry.error}</AlertDescription>
          </Alert>
        )}
      </MessageContent>
    </Message>
  );
}

function EventEntry({ entry }: { entry: Entry }) {
  const parts = entry.parts ?? [];
  const body = parts.length > 0 && <PromptParts parts={parts} />;
  if (entry.level === "error") {
    return (
      <Alert variant="destructive">
        <CircleXIcon />
        <AlertTitle className="flex items-start gap-2">
          <span className="min-w-0 flex-1 whitespace-pre-wrap">{entry.text}</span>
          <At at={entry.at} />
        </AlertTitle>
        {body && <AlertDescription>{body}</AlertDescription>}
      </Alert>
    );
  }
  const warn = entry.level === "warn";
  return (
    <Row
      at={entry.at}
      line={
        <TraceLine className={cn(warn && "text-foreground")}>
          <TraceIcon>{warn ? <TriangleAlertIcon /> : <InfoIcon />}</TraceIcon>
          <TraceTitle className="whitespace-normal">{entry.text}</TraceTitle>
        </TraceLine>
      }
    >
      {body || undefined}
    </Row>
  );
}

/**
 * An agent run as a read-only conversation (like the Codex / Claude Code apps): system prompt, the user turn
 * (QQ-rendered context for invocations), assistant turns with thinking, text and tool cards, pipeline events.
 * Times are relative to `startedAt`.
 */
export function Transcript({ transcript: t, startedAt }: { transcript: TranscriptOut; startedAt: string }) {
  const ctx = useMemo<Ctx>(() => {
    const messages = t.messages ?? {};
    const byQQ = new Map<number, QqMessage>();
    const add = (m: QqMessage | null | undefined) => {
      if (m?.qq_message_id != null) byQQ.set(m.qq_message_id, m);
    };
    Object.values(messages).forEach(add);
    for (const e of t.entries) {
      if (e.display?.kind === "qq_context") {
        add(e.display.quoted);
        add(e.display.trigger);
        e.display.chain?.forEach(add);
      }
    }
    return {
      start: Date.parse(startedAt),
      live: !!t.live,
      names: t.names ?? {},
      messages,
      lookup: (id) => byQQ.get(id),
      bot: t.bot,
    };
  }, [t, startedAt]);
  // Tool entries render inside the card of the call they answer; the rest stand alone.
  const results = useMemo(() => {
    const calls = new Set<string>();
    for (const e of t.entries) {
      if (e.kind !== "assistant") continue;
      for (const p of e.parts ?? []) if (p.type === "tool_call") calls.add(p.id);
    }
    const map = new Map<string, Entry>();
    for (const e of t.entries) if (e.kind === "tool" && e.call_id && calls.has(e.call_id)) map.set(e.call_id, e);
    return map;
  }, [t.entries]);
  return (
    <TranscriptContext value={ctx}>
      <div className="flex flex-col gap-5">
        {!t.available && (
          <Alert>
            <InfoIcon />
            <AlertTitle>这次运行早于完整记录功能，只能显示请求和结果</AlertTitle>
          </Alert>
        )}
        {t.content === "metadata" && (
          <Alert>
            <InfoIcon />
            <AlertTitle>内容未记录</AlertTitle>
            <AlertDescription>
              这次运行时没有开启内容记录（privacy.capture_llm_content），只保存了结构：角色、工具名、长度和耗时。
            </AlertDescription>
          </Alert>
        )}
        {t.entries.length === 0 && !t.live && <Empty>没有记录</Empty>}
        {t.entries.map((e, i) => {
          switch (e.kind) {
            case "system":
              return <SystemEntry key={i} entry={e} />;
            case "user":
              return <UserEntry key={i} entry={e} source={t.source} />;
            case "assistant":
              return <AssistantEntry key={i} entry={e} results={results} />;
            case "tool":
              return e.call_id && results.get(e.call_id) === e ? null : (
                <Trace key={i}>
                  <ToolCard entry={e} />
                </Trace>
              );
            default:
              return <EventEntry key={i} entry={e} />;
          }
        })}
        {t.live && (
          <Trace>
            <TraceLine status="running">
              <TraceIcon>
                <Spinner />
              </TraceIcon>
              <TraceTitle>运行中…</TraceTitle>
            </TraceLine>
          </Trace>
        )}
      </div>
    </TranscriptContext>
  );
}

/** Label/value facts for a detail page's side column. */
export function Facts({ items }: { items: [string, ReactNode][] }) {
  return (
    <dl className="grid grid-cols-[auto_minmax(0,1fr)] gap-x-4 gap-y-1.5 text-sm">
      {items.map(([label, value]) => (
        <div key={label} className="contents">
          <dt className="text-muted-foreground">{label}</dt>
          <dd className="break-words">{value}</dd>
        </div>
      ))}
    </dl>
  );
}

/** The model's context window from the providers' models.dev data; `model` is a `provider/model` route label. */
function useContextWindow(model: string | null | undefined): number | undefined {
  const providers = useQuery(listProvidersOptions());
  if (!model) return undefined;
  const slash = model.indexOf("/");
  const name = slash > 0 ? model.slice(slash + 1) : model;
  const list = providers.data ?? [];
  const caps =
    (slash > 0 ? list.find((p) => p.name === model.slice(0, slash))?.model_caps[name] : undefined) ??
    list.map((p) => p.model_caps[model] ?? p.model_caps[name]).find(Boolean);
  return caps?.context ?? undefined;
}

/**
 * Token usage (AI Elements "Context"): a hover card with input / cached / output and, when the model's context
 * window is known, how full the largest single request got.
 */
export function ContextUsage({
  model,
  input,
  cached,
  output,
  requests,
  transcript,
}: {
  model?: string | null;
  input: number;
  cached: number;
  output: number;
  requests?: number;
  transcript?: TranscriptOut;
}) {
  const limit = useContextWindow(model);
  const peak = Math.max(
    0,
    ...(transcript?.entries ?? []).map((e) => (e.usage ? (e.usage.input ?? 0) + (e.usage.output ?? 0) : 0)),
  );
  const used = peak || input + output;
  const percent = limit ? Math.min(100, Math.round((used / limit) * 100)) : null;
  const rows: [string, number][] = [
    ["输入", input],
    ["其中缓存", cached],
    ["输出", output],
    ...(requests != null ? ([["请求次数", requests]] as [string, number][]) : []),
  ];
  return (
    <HoverCard openDelay={10} closeDelay={100}>
      <HoverCardTrigger asChild>
        <Button variant="outline" size="xs" className="tabular-nums">
          {percent != null ? `${percent}% 上下文` : `${fmtTokens(input + output)} tokens`}
        </Button>
      </HoverCardTrigger>
      <HoverCardContent className="flex w-64 flex-col gap-3">
        {limit ? (
          <>
            <div className="flex items-center justify-between gap-3 text-sm">
              <span className="font-medium">上下文</span>
              <span className="text-muted-foreground tabular-nums">
                {fmtTokens(used)} / {fmtTokens(limit)}
              </span>
            </div>
            <Progress value={percent} aria-label="上下文占用" />
            {peak > 0 && <p className="text-xs text-muted-foreground">按用量最大的一次请求计算</p>}
          </>
        ) : (
          <span className="text-sm font-medium">用量</span>
        )}
        <dl className="grid grid-cols-[1fr_auto] gap-x-3 gap-y-1 text-sm">
          {rows.map(([label, value]) => (
            <div key={label} className="contents">
              <dt className="text-muted-foreground">{label}</dt>
              <dd className="tabular-nums">{value.toLocaleString("zh-CN")}</dd>
            </div>
          ))}
        </dl>
      </HoverCardContent>
    </HoverCard>
  );
}
