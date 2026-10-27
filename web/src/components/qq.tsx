import { format, isThisYear, isToday } from "date-fns";
import {
  BookIcon,
  ChevronDownIcon,
  ChevronRightIcon,
  FileIcon,
  FilmIcon,
  ImageOffIcon,
  type LucideIcon,
  MessagesSquareIcon,
  MicIcon,
} from "lucide-react";
import { type ReactNode, useState } from "react";
import type { BotReply, OpsReply, QqMessage, SourceRef } from "@/client";
import { DetailDialog, ExternalLink, Hint } from "@/components/common";
import { Avatar, AvatarFallback, AvatarImage } from "@/components/ui/avatar";
import { Badge } from "@/components/ui/badge";
import { Bubble, BubbleContent } from "@/components/ui/bubble";
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from "@/components/ui/collapsible";
import { Message, MessageAvatar, MessageContent, MessageGroup, MessageHeader } from "@/components/ui/message";
import { fmtTime, isPlainObject } from "@/lib/format";
import { cn } from "@/lib/utils";

/** QQ number -> group card. */
export type Names = Record<string, string>;
/** A raw OneBot segment. */
export type Segment = { type: string; data: Record<string, unknown> };

type Lookup = (qqMessageId: number) => QqMessage | undefined;
type ForwardNode = { userId: number; name: string; sentAt?: string | null; segments: Segment[] };

const RPS: Record<string, string> = { "1": "石头", "2": "剪刀", "3": "布" };
/** A message alone in its row without a bubble, like QQ shows cards, pictures and stickers. */
const BARE: Record<string, true> = { forward: true, json: true, mface: true, image: true };
const DIVIDER_GAP_MS = 5 * 60_000;

function str(v: unknown): string {
  return v == null ? "" : String(v);
}

/** Raw segments (or a CQ string) as well-formed segments; malformed entries are dropped. */
function toSegments(raw: unknown): Segment[] {
  if (typeof raw === "string") return raw ? [{ type: "text", data: { text: raw } }] : [];
  if (!Array.isArray(raw)) return [];
  return raw
    .filter(isPlainObject)
    .map((s) => ({ type: str(s.type) || "unknown", data: isPlainObject(s.data) ? s.data : {} }));
}

function senderName(names: Names, userId: number, fallback?: string | null): string {
  return names[String(userId)] || fallback || String(userId);
}

function stripBrackets(v: unknown): string {
  return str(v).replace(/^\[|\]$/g, "");
}

function atName(data: Record<string, unknown>, names: Names): string {
  const qq = str(data.qq);
  if (qq === "all") return "全体成员";
  return names[qq] || str(data.name).replace(/^@/, "") || qq;
}

/** Custom stickers arrive as image segments with `sub_type` 1 or a summary such as `[动画表情]`. */
function isSticker(data: Record<string, unknown>): boolean {
  const summary = stripBrackets(data.summary);
  return str(data.sub_type) === "1" || (summary !== "" && summary !== "图片");
}

function clock(at: string | null | undefined): string | null {
  if (!at) return null;
  const d = new Date(at);
  return Number.isNaN(d.getTime()) ? null : format(d, "HH:mm");
}

/** Only http(s) links leave the console (cards and sources come from outside). */
function webUrl(v: unknown): string | null {
  const s = str(v).trim();
  if (/^https?:\/\//i.test(s)) return s;
  if (s.startsWith("//")) return `https:${s}`;
  if (/^[\w-]+(\.[\w-]+)+\//.test(s)) return `https://${s}`; // card previews often omit the scheme
  return null;
}

function humanSize(v: unknown): string {
  let n = Number(v);
  if (!Number.isFinite(n) || n <= 0) return "";
  const units = ["B", "KB", "MB", "GB"];
  let i = 0;
  while (n >= 1024 && i < units.length - 1) {
    n /= 1024;
    i++;
  }
  return `${i === 0 ? n : +n.toFixed(1)} ${units[i]}`;
}

/** NapCat / OneBot time: unix seconds (or ms), or an ISO string. */
function nodeTime(v: unknown): string | null {
  const n = typeof v === "number" ? v : /^\d+$/.test(str(v)) ? Number(v) : Number.NaN;
  if (Number.isNaN(n)) return typeof v === "string" && v ? v : null;
  const d = new Date(n < 1e12 ? n * 1000 : n);
  return Number.isNaN(d.getTime()) ? null : d.toISOString();
}

type Card = { title: string; desc: string; url: string | null; preview: string | null; tag: string; forward: boolean };

/** A json card: title from `meta.*.title|desc` or `prompt`, link from `meta.*.qqdocurl|jumpUrl|url`. */
function parseCard(raw: unknown): Card {
  const card: Card = { title: "", desc: "", url: null, preview: null, tag: "", forward: false };
  let doc: unknown = null;
  try {
    doc = JSON.parse(str(raw));
  } catch {
    doc = null;
  }
  if (isPlainObject(doc)) {
    // Merged forwards the bot itself sent come back as a multimsg card without their content.
    card.forward = doc.app === "com.tencent.multimsg";
    card.title = str(doc.prompt);
    for (const item of isPlainObject(doc.meta) ? Object.values(doc.meta) : []) {
      if (!isPlainObject(item)) continue;
      card.title = str(item.title) || str(item.desc) || card.title;
      card.desc = item.title && item.desc !== item.title ? str(item.desc) : "";
      card.preview = webUrl(item.preview) ?? card.preview;
      card.tag = str(item.tag) || card.tag;
      card.url = webUrl(item.qqdocurl) ?? webUrl(item.jumpUrl) ?? webUrl(item.url);
      if (card.url) break;
    }
  }
  card.title = card.title.trim() || "卡片";
  return card;
}

/** Forward content in NapCat (`{sender, time, message}`) or OneBot node (`{type: "node", data}`) form. */
function forwardNodes(content: unknown): ForwardNode[] | null {
  if (!Array.isArray(content)) return null;
  return content.filter(isPlainObject).map((node) => {
    const d = node.type === "node" && isPlainObject(node.data) ? node.data : node;
    const sender = isPlainObject(d.sender) ? d.sender : d;
    const userId = Number(sender.user_id) || 0;
    return {
      userId,
      name: str(sender.card) || str(sender.nickname) || String(userId),
      sentAt: nodeTime(d.time),
      segments: toSegments(d.message ?? d.content),
    };
  });
}

/** One-line text of a segment, QQ-preview style (forward previews, reply quotes, chip labels). */
function segmentText({ type, data }: Segment, names: Names): string {
  switch (type) {
    case "text":
      return str(data.text);
    case "at":
      return `@${atName(data, names)} `;
    case "reply":
      return "";
    case "face": {
      const text = isPlainObject(data.raw) ? str(data.raw.faceText).replace(/^\//, "") : "";
      return `[${text || `表情${str(data.id)}`}]`;
    }
    case "mface":
      return `[${stripBrackets(data.summary) || "表情包"}]`;
    case "image":
      return isSticker(data) ? `[${stripBrackets(data.summary) || "动画表情"}]` : "[图片]";
    case "record":
      return "[语音]";
    case "video":
      return "[视频]";
    case "file":
      return `[文件] ${str(data.file) || str(data.name)}`;
    case "forward":
      return "[聊天记录]";
    case "json": {
      const card = parseCard(data.data);
      return card.forward ? "[聊天记录]" : `[卡片] ${card.title}`;
    }
    case "xml":
      return "[卡片]";
    case "poke":
      return "[戳一戳]";
    case "dice":
      return `🎲 ${str(data.result)}`;
    case "rps":
      return `[猜拳] ${RPS[str(data.result)] ?? str(data.result)}`;
    case "markdown":
      return str(data.content);
    default:
      return `[${type}]`;
  }
}

function plainText(segments: unknown, names: Names): string {
  return toSegments(segments)
    .map((s) => segmentText(s, names))
    .join("")
    .replace(/\s+/g, " ")
    .trim();
}

function Chip({ icon: Icon, children }: { icon?: LucideIcon; children: ReactNode }) {
  return (
    <Badge variant="outline" className="mx-0.5 max-w-full align-middle font-normal text-muted-foreground">
      {Icon && <Icon aria-hidden />}
      <span className="truncate">{children}</span>
    </Badge>
  );
}

function MediaChip({ icon, parts }: { icon: LucideIcon; parts: string[] }) {
  return <Chip icon={icon}>{parts.filter(Boolean).join(" · ")}</Chip>;
}

function Note({ children }: { children: ReactNode }) {
  return <p className="text-sm text-muted-foreground">{children}</p>;
}

export function QQAvatar({ userId, name, className }: { userId: number; name?: string; className?: string }) {
  const label = (name || String(userId)).trim();
  return (
    <Avatar className={className}>
      {userId > 0 && (
        <AvatarImage src={`https://q1.qlogo.cn/g?b=qq&nk=${userId}&s=100`} alt="" referrerPolicy="no-referrer" />
      )}
      <AvatarFallback>{Array.from(label)[0]?.toUpperCase() ?? "?"}</AvatarFallback>
    </Avatar>
  );
}

function ReplyQuote({ id, names, lookup }: { id: unknown; names: Names; lookup?: Lookup }) {
  const qid = str(id) === "" ? Number.NaN : Number(id);
  const target = Number.isNaN(qid) ? undefined : lookup?.(qid);
  const time = clock(target?.sent_at);
  return (
    <span className="mb-1 block rounded-md border-l-2 border-primary/40 bg-background/50 px-2 py-1 text-xs text-muted-foreground">
      {target ? (
        <>
          <span className="font-medium">{senderName(names, target.user_id, target.sender_name)}</span>
          {time && <span className="ml-1.5 tabular-nums">{time}</span>}
          <span className="line-clamp-2 wrap-break-word">{plainText(target.segments, names) || "[空消息]"}</span>
        </>
      ) : (
        "回复了一条消息"
      )}
    </span>
  );
}

function ImageSegment({ data, sticker, alt }: { data: Record<string, unknown>; sticker: boolean; alt: string }) {
  const [broken, setBroken] = useState(false);
  const [open, setOpen] = useState(false);
  const src = str(data.src) || str(data.url);
  if (!src || broken) return <Chip icon={ImageOffIcon}>{alt}</Chip>;
  if (sticker) {
    return (
      <img
        src={src}
        alt={alt}
        loading="lazy"
        referrerPolicy="no-referrer"
        onError={() => setBroken(true)}
        className="inline-block max-h-30 max-w-30 align-bottom"
      />
    );
  }
  return (
    <>
      <button
        type="button"
        onClick={() => setOpen(true)}
        aria-label="查看原图"
        className="my-1 block cursor-zoom-in overflow-hidden rounded-md outline-none focus-visible:ring-3 focus-visible:ring-ring/50"
      >
        <img
          src={src}
          alt={alt}
          loading="lazy"
          referrerPolicy="no-referrer"
          onError={() => setBroken(true)}
          className="max-h-60 max-w-60 object-contain"
        />
      </button>
      <DetailDialog open={open} onOpenChange={setOpen} title="图片" className="sm:max-w-4xl">
        <img src={src} alt={alt} referrerPolicy="no-referrer" className="mx-auto max-h-[70svh] max-w-full object-contain" />
        <ExternalLink href={src}>在新标签页打开</ExternalLink>
      </DetailDialog>
    </>
  );
}

function CardSegment({ raw }: { raw: unknown }) {
  const card = parseCard(raw);
  if (card.forward) return <Chip icon={MessagesSquareIcon}>[合并转发]</Chip>;
  let host = "";
  try {
    host = card.url ? new URL(card.url).hostname : "";
  } catch {
    host = "";
  }
  return (
    <span className="my-1 flex w-64 max-w-full flex-col gap-2 rounded-lg border bg-card p-3 text-card-foreground">
      <span className="flex gap-2">
        <span className="min-w-0 flex-1">
          <span className="line-clamp-2 text-sm font-medium">{card.title}</span>
          {card.desc && <span className="mt-0.5 line-clamp-2 text-xs text-muted-foreground">{card.desc}</span>}
        </span>
        {card.preview && (
          <img
            src={card.preview}
            alt=""
            loading="lazy"
            referrerPolicy="no-referrer"
            onError={(e) => {
              e.currentTarget.hidden = true;
            }}
            className="size-12 shrink-0 rounded object-cover"
          />
        )}
      </span>
      <span className="flex items-center justify-between gap-2 border-t pt-2 text-xs text-muted-foreground">
        <span className="truncate">{card.tag || "卡片"}</span>
        {card.url && (
          <ExternalLink href={card.url} className="min-w-0 truncate">
            {host || "打开"}
          </ExternalLink>
        )}
      </span>
    </span>
  );
}

function SegmentView({ segment, names, lookup }: { segment: Segment; names: Names; lookup?: Lookup }) {
  const { type, data } = segment;
  switch (type) {
    case "text":
      return <span className="whitespace-pre-wrap">{str(data.text)}</span>;
    case "at":
      return (
        <Hint content={str(data.qq) === "all" ? null : str(data.qq)}>
          <span className="mr-0.5 font-medium text-primary">@{atName(data, names)}</span>
        </Hint>
      );
    case "reply":
      return <ReplyQuote id={data.id} names={names} lookup={lookup} />;
    case "image":
    case "mface":
      return (
        <ImageSegment
          data={data}
          sticker={type === "mface" || isSticker(data)}
          alt={segmentText(segment, names)}
        />
      );
    case "record":
      return <MediaChip icon={MicIcon} parts={["语音", humanSize(data.file_size)]} />;
    case "video":
      return <MediaChip icon={FilmIcon} parts={["视频", str(data.file), humanSize(data.file_size)]} />;
    case "file":
      return (
        <MediaChip
          icon={FileIcon}
          parts={[str(data.file) || str(data.name) || "未命名文件", humanSize(data.file_size)]}
        />
      );
    case "forward": {
      const nodes = forwardNodes(data.content);
      if (!nodes) return <Chip icon={MessagesSquareIcon}>[合并转发]</Chip>;
      return (
        <span className="my-1 block">
          <QQForwardCard nodes={nodes} names={names} title={str(data.title) || undefined} />
        </span>
      );
    }
    case "json":
      return <CardSegment raw={data.data} />;
    case "markdown":
      return <pre className="my-1 font-mono text-xs whitespace-pre-wrap">{str(data.content)}</pre>;
    default:
      return <Chip>{segmentText(segment, names)}</Chip>;
  }
}

/** The inline content of one QQ message. */
export function QQSegments({ segments, names, lookup }: { segments: Segment[]; names: Names; lookup?: Lookup }) {
  const list = toSegments(segments);
  if (list.length === 0) return <span className="text-muted-foreground">[空消息]</span>;
  return (
    <div className="min-w-0 wrap-break-word">
      {list.map((segment, i) => (
        <SegmentView key={i} segment={segment} names={names} lookup={lookup} />
      ))}
    </div>
  );
}

function ChatRow({
  userId,
  name,
  at,
  self = false,
  recalled = false,
  highlight = false,
  bare = false,
  children,
}: {
  userId: number;
  name: string;
  at?: string | null;
  self?: boolean;
  recalled?: boolean;
  highlight?: boolean;
  bare?: boolean;
  children: ReactNode;
}) {
  const time = clock(at);
  return (
    <Message
      align={self ? "end" : "start"}
      className={cn("rounded-lg p-1", highlight && "bg-primary/5 ring-1 ring-primary/40")}
    >
      <MessageAvatar className="self-start bg-transparent">
        <QQAvatar userId={userId} name={name} />
      </MessageAvatar>
      <MessageContent className="gap-1">
        <MessageHeader className="gap-2 px-0">
          <Hint content={userId > 0 ? String(userId) : null}>
            <span className="min-w-0 truncate">{name}</span>
          </Hint>
          {time && at && (
            <Hint content={fmtTime(at)}>
              <time dateTime={at} className="font-normal tabular-nums">
                {time}
              </time>
            </Hint>
          )}
          {recalled && (
            <Badge variant="secondary" className="font-normal text-muted-foreground">
              已撤回
            </Badge>
          )}
        </MessageHeader>
        <Bubble
          variant={bare ? "ghost" : self ? "tinted" : "secondary"}
          align={self ? "end" : "start"}
          className={cn(recalled && "opacity-60")}
        >
          <BubbleContent>{children}</BubbleContent>
        </Bubble>
      </MessageContent>
    </Message>
  );
}

/** One chat row: avatar, name, time, bubble. The bot's own messages (`is_self`) sit on the right. */
export function QQMessageView({
  message,
  names,
  lookup,
  highlight = false,
}: {
  message: QqMessage;
  names: Names;
  lookup?: Lookup;
  highlight?: boolean;
}) {
  const segments = toSegments(message.segments);
  return (
    <ChatRow
      userId={message.user_id}
      name={senderName(names, message.user_id, message.sender_name)}
      at={message.sent_at}
      self={!!message.is_self}
      recalled={!!message.recalled}
      highlight={highlight}
      bare={segments.length === 1 && BARE[segments[0].type] === true}
    >
      <QQSegments segments={segments} names={names} lookup={lookup} />
    </ChatRow>
  );
}

/** A chat log with QQ-style time dividers; `highlightIds` (messages.id) get a ring. */
export function QQMessageList({
  messages,
  names,
  lookup,
  highlightIds,
}: {
  messages: QqMessage[];
  names: Names;
  lookup?: Lookup;
  highlightIds?: number[];
}) {
  const highlighted = new Set(highlightIds);
  const dividers: (string | null)[] = [];
  let last = Number.NaN;
  for (const m of messages) {
    const t = m.sent_at ? new Date(m.sent_at).getTime() : Number.NaN;
    let label: string | null = null;
    if (!Number.isNaN(t)) {
      if (Number.isNaN(last) || t - last > DIVIDER_GAP_MS) {
        const d = new Date(t);
        label = format(d, isToday(d) ? "HH:mm" : isThisYear(d) ? "M月d日 HH:mm" : "yyyy年M月d日 HH:mm");
      }
      last = t;
    }
    dividers.push(label);
  }
  return (
    <MessageGroup className="gap-3">
      {messages.map((m, i) => (
        <div key={`${i}:${m.id ?? ""}`} className="grid gap-3">
          {dividers[i] && <div className="text-center text-xs text-muted-foreground tabular-nums">{dividers[i]}</div>}
          <QQMessageView
            message={m}
            names={names}
            lookup={lookup}
            highlight={m.id != null && highlighted.has(m.id)}
          />
        </div>
      ))}
    </MessageGroup>
  );
}

/** A QQ merged-forward card: title, four preview lines, expands inline into the forwarded chat. */
export function QQForwardCard({
  nodes,
  names,
  title = "群聊的聊天记录",
  defaultOpen = false,
}: {
  nodes: ForwardNode[];
  names: Names;
  title?: string;
  defaultOpen?: boolean;
}) {
  const [open, setOpen] = useState(defaultOpen);
  const messages: QqMessage[] = nodes.map((n) => ({
    user_id: n.userId,
    sender_name: n.name,
    sent_at: n.sentAt,
    segments: n.segments,
  }));
  return (
    <Collapsible
      open={open}
      onOpenChange={setOpen}
      className="w-72 max-w-full overflow-hidden rounded-lg border bg-card text-left text-card-foreground data-[state=open]:w-xl"
    >
      <CollapsibleTrigger className="block w-full p-3 text-left text-sm outline-none hover:bg-muted/50 focus-visible:ring-3 focus-visible:ring-ring/50">
        <span className="block truncate font-medium">{title}</span>
        <span className="mt-1.5 grid gap-0.5 text-xs text-muted-foreground">
          {nodes.slice(0, 4).map((n, i) => (
            <span key={i} className="truncate">
              {senderName(names, n.userId, n.name)}: {plainText(n.segments, names) || "[空消息]"}
            </span>
          ))}
        </span>
        <span className="mt-2 flex items-center justify-between border-t pt-2 text-xs text-muted-foreground">
          查看 {nodes.length} 条转发消息
          <ChevronRightIcon className={cn("size-3.5 transition-transform", open && "rotate-90")} aria-hidden />
        </span>
      </CollapsibleTrigger>
      <CollapsibleContent className="border-t bg-background/50 p-2">
        <QQMessageList messages={messages} names={names} />
      </CollapsibleContent>
    </Collapsible>
  );
}

/** AI Elements "sources": a collapsed count that opens into the list of links. */
function Sources({ sources }: { sources: SourceRef[] }) {
  return (
    <Collapsible className="pl-11 text-xs">
      <CollapsibleTrigger className="group flex items-center gap-1 font-medium text-primary">
        引用了 {sources.length} 个来源
        <ChevronDownIcon className="size-4 transition-transform group-data-[state=open]:rotate-180" aria-hidden />
      </CollapsibleTrigger>
      <CollapsibleContent className="mt-2 flex w-fit flex-col gap-2">
        {sources.map((s, i) => {
          const url = webUrl(s.url);
          return (
            <div key={i} className="flex items-center gap-2">
              <BookIcon className="size-4 shrink-0 text-muted-foreground" aria-hidden />
              {url ? <ExternalLink href={url}>{s.label}</ExternalLink> : <span>{s.label}</span>}
            </div>
          );
        })}
      </CollapsibleContent>
    </Collapsible>
  );
}

/** OpsReply carries only `mode` + `text`; anything else (or a chat-only mode) is a BotReply. */
function isBotReply(reply: BotReply | OpsReply): reply is BotReply {
  return (
    reply.mode === "forward" ||
    reply.mode === "silent" ||
    "sections" in reply ||
    "sources" in reply ||
    "title" in reply ||
    "silent_reason" in reply
  );
}

/**
 * The agent's final answer: a BotReply as the bot's forward card, an OpsReply as a text message or note. `botId`
 * (the bot's QQ number) gives it its real avatar; `defaultOpen` shows the forward expanded.
 */
export function ReplyView({
  reply,
  botName = "bot",
  botId = 0,
  defaultOpen = false,
}: {
  reply: BotReply | OpsReply;
  botName?: string;
  botId?: number;
  defaultOpen?: boolean;
}) {
  const names: Names = botId ? { [String(botId)]: botName } : {};
  if (isBotReply(reply)) {
    if (reply.mode === "silent") {
      return <Note>{reply.silent_reason ? `不回复：${reply.silent_reason}` : "不回复"}</Note>;
    }
    if (reply.mode === "done") return <Note>已完成操作，没有发消息</Note>;
    const nodes = (reply.sections ?? []).map((s) => ({
      userId: botId,
      name: botName,
      segments: [{ type: "text", data: { text: s.content } }],
    }));
    return (
      <div className="grid gap-2">
        <ChatRow userId={botId} name={botName} bare>
          <QQForwardCard nodes={nodes} names={names} title={reply.title || undefined} defaultOpen={defaultOpen} />
        </ChatRow>
        {reply.sources && reply.sources.length > 0 && <Sources sources={reply.sources} />}
      </div>
    );
  }
  if (reply.mode === "reply") {
    return (
      <ChatRow userId={botId} name={botName}>
        <span className="whitespace-pre-wrap">{reply.text || "[空消息]"}</span>
      </ChatRow>
    );
  }
  return <Note>{reply.mode === "handoff" ? "不是管理操作，交给普通答疑" : "已完成操作"}</Note>;
}
