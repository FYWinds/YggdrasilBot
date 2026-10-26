import { useQuery } from "@tanstack/react-query";
import { ExternalLinkIcon } from "lucide-react";
import { useState } from "react";
import { getInvocationOptions, listInvocationsOptions } from "@/client/@tanstack/react-query.gen";
import {
  Empty,
  Field,
  GroupSelect,
  Hint,
  OptionSelect,
  PageHeader,
  Pager,
  Pre,
  QueryState,
  useGroupName,
} from "@/components/common";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { fmtTime } from "@/lib/format";

const STATUSES = ["running", "ok", "silent", "blocked_input", "review", "rejected", "failed", "ratelimited", "cancelled"];
const HANDLERS = ["chat", "tldr", "report", "privacy.optout", "privacy.optin", "privacy.forget", "admin.review", "admin.status"];
const LIMIT = 50;

function statusVariant(status: string): "default" | "destructive" | "outline" | "secondary" {
  if (status === "ok") return "default";
  if (status === "failed" || status === "rejected") return "destructive";
  if (status === "running") return "secondary";
  return "outline";
}

function TraceLink({ url }: { url: string | null | undefined }) {
  if (!url) return <span className="text-muted-foreground">—</span>;
  return (
    <a href={url} target="_blank" rel="noreferrer" className="inline-flex items-center gap-1 text-xs underline">
      打开 <ExternalLinkIcon className="size-3" />
    </a>
  );
}

export function InvocationsPage() {
  const [groupId, setGroupId] = useState<number | null>(null);
  const [handler, setHandler] = useState<string | null>(null);
  const [status, setStatus] = useState<string | null>(null);
  const [offset, setOffset] = useState(0);
  const [selected, setSelected] = useState<string | null>(null);
  const list = useQuery(
    listInvocationsOptions({ query: { group_id: groupId, handler, status, limit: LIMIT, offset } }),
  );
  const groupName = useGroupName();
  return (
    <div>
      <PageHeader title="调用记录" description="每次触发的完整记录，trace 链接打开 Grafana Tempo。" />
      <div className="mb-4 flex flex-wrap items-end gap-3">
        <Field label="群" className="w-60">
          <GroupSelect
            value={groupId}
            onChange={(v) => {
              setGroupId(v);
              setOffset(0);
            }}
          />
        </Field>
        <Field label="handler" className="w-44">
          <OptionSelect
            value={handler}
            onChange={(v) => {
              setHandler(v);
              setOffset(0);
            }}
            noneLabel="全部"
            options={HANDLERS.map((h) => ({ value: h, label: h }))}
          />
        </Field>
        <Field label="状态" className="w-40">
          <OptionSelect
            value={status}
            onChange={(v) => {
              setStatus(v);
              setOffset(0);
            }}
            noneLabel="全部"
            options={STATUSES.map((s) => ({ value: s, label: s }))}
          />
        </Field>
      </div>
      <QueryState isLoading={list.isLoading} error={list.error} />
      {list.data?.items.length === 0 && <Empty />}
      {!!list.data?.items.length && (
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>时间</TableHead>
              <TableHead>群</TableHead>
              <TableHead>用户</TableHead>
              <TableHead>handler</TableHead>
              <TableHead>状态</TableHead>
              <TableHead>请求</TableHead>
              <TableHead>tokens</TableHead>
              <TableHead>耗时</TableHead>
              <TableHead>trace</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {list.data.items.map((i) => (
              <TableRow key={i.id} className="cursor-pointer" onClick={() => setSelected(i.id)}>
                <TableCell className="text-xs whitespace-nowrap">{fmtTime(i.created_at)}</TableCell>
                <TableCell className="max-w-36 truncate text-xs">{i.group_id != null ? groupName(i.group_id) : "私聊"}</TableCell>
                <TableCell className="max-w-32 truncate text-xs" title={i.user_name ? `${i.user_name}（${i.user_id}）` : undefined}>
                  {i.user_name || <span className="font-mono">{i.user_id}</span>}
                </TableCell>
                <TableCell className="font-mono text-xs">{i.handler}</TableCell>
                <TableCell>
                  <Badge variant={statusVariant(i.status)}>{i.status}</Badge>
                </TableCell>
                <TableCell className="max-w-sm text-sm">
                  <Hint content={i.request_text}>
                    <div className="truncate">{i.request_text}</div>
                  </Hint>
                </TableCell>
                <TableCell className="text-xs whitespace-nowrap">
                  {i.tokens_in} / {i.tokens_out}
                </TableCell>
                <TableCell className="text-xs">{i.duration_ms != null ? `${(i.duration_ms / 1000).toFixed(1)}s` : "—"}</TableCell>
                <TableCell onClick={(e) => e.stopPropagation()}>
                  <TraceLink url={i.trace_url} />
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      )}
      {list.data && <Pager total={list.data.total} limit={LIMIT} offset={offset} onChange={setOffset} />}
      {selected && <InvocationDialog id={selected} groupName={groupName} onClose={() => setSelected(null)} />}
    </div>
  );
}

type ReplyDoc = {
  mode?: string;
  title?: string | null;
  sections?: { content?: string }[];
  sources?: { url?: string | null; label?: string }[];
  silent_reason?: string | null;
  messages?: unknown[];
  text?: string | null; // ops replies
};

/** A stored chat / tldr / ops reply as readable text; anything unrecognised falls back to the raw JSON. */
function Reply({ reply }: { reply: Record<string, unknown> }) {
  const r = reply as ReplyDoc;
  const sections = Array.isArray(r.sections) ? r.sections.filter((s) => s.content?.trim()) : [];
  const messages = [...(Array.isArray(r.messages) ? r.messages : []), r.text].filter(
    (m): m is string => typeof m === "string" && !!m,
  );
  const sources = Array.isArray(r.sources) ? r.sources : [];
  if (!r.silent_reason && !r.title && !sections.length && !messages.length && !sources.length) {
    return <Pre value={reply} />;
  }
  return (
    <div className="grid gap-2 rounded-lg bg-muted p-3">
      {r.silent_reason && (
        <p className="whitespace-pre-wrap">
          <span className="text-muted-foreground">{r.mode === "silent" ? "不回复：" : "原因："}</span>
          {r.silent_reason}
        </p>
      )}
      {r.title && <div className="font-medium">{r.title}</div>}
      {[...messages, ...sections.map((s) => s.content)].map((text, i) => (
        <p key={i} className="whitespace-pre-wrap">
          {text}
        </p>
      ))}
      {!!sources.length && (
        <div className="grid gap-0.5 text-xs">
          {sources.map((s, i) =>
            s.url ? (
              <a key={i} href={s.url} target="_blank" rel="noreferrer" className="truncate underline">
                {s.label || s.url}
              </a>
            ) : (
              <span key={i} className="truncate">
                {s.label}
              </span>
            ),
          )}
        </div>
      )}
    </div>
  );
}

function InvocationDialog({
  id,
  groupName,
  onClose,
}: {
  id: string;
  groupName: (id: number) => string;
  onClose: () => void;
}) {
  const detail = useQuery(getInvocationOptions({ path: { invocation_id: id } }));
  const d = detail.data;
  return (
    <Dialog open onOpenChange={(o) => !o && onClose()}>
      <DialogContent className="max-h-[90svh] overflow-y-auto sm:max-w-3xl">
        <DialogHeader>
          <DialogTitle className="flex flex-wrap items-baseline gap-2 pr-6">
            调用详情 <span className="font-mono text-xs font-normal text-muted-foreground">{id}</span>
          </DialogTitle>
        </DialogHeader>
        <QueryState isLoading={detail.isLoading} error={detail.error} />
        {d && (
          <div className="grid gap-3 text-sm">
            <div className="flex flex-wrap items-center gap-2">
              <Badge variant={statusVariant(d.status)}>{d.status}</Badge>
              <span className="font-mono">{d.handler}</span>
              <span className="text-muted-foreground">{fmtTime(d.created_at)}</span>
              <span>
                {d.group_id != null ? groupName(d.group_id) : "私聊"} · {d.user_name || d.user_id}
              </span>
              {d.trace_url && (
                <Button size="sm" variant="outline" asChild>
                  <a href={d.trace_url} target="_blank" rel="noreferrer">
                    在 Grafana 打开 trace <ExternalLinkIcon />
                  </a>
                </Button>
              )}
            </div>
            <div className="whitespace-pre-wrap break-words">
              <span className="text-muted-foreground">请求：</span>
              {d.request_text || "—"}
            </div>
            <div className="grid grid-cols-2 gap-2 text-xs md:grid-cols-4">
              <div>tokens in {d.tokens_in}</div>
              <div>out {d.tokens_out}</div>
              <div>cached {d.tokens_cached}</div>
              <div>耗时 {d.duration_ms ?? "—"} ms</div>
              <div>快速响应 {d.ack_kind ?? "—"}</div>
              <div>首响应 {d.ack_latency_ms ?? "—"} ms</div>
              <div className="col-span-2">模型 {d.models.map((m) => (typeof m === "string" ? m : JSON.stringify(m))).join("、") || "—"}</div>
            </div>
            {Object.keys(d.tool_calls).length > 0 && (
              <div className="text-xs">
                工具 {Object.entries(d.tool_calls).map(([k, v]) => `${k}×${String(v)}`).join(" · ")}
              </div>
            )}
            {d.error && <Pre value={d.error} className="text-destructive" />}
            {d.reply && (
              <>
                <div className="font-medium">回复</div>
                <Reply reply={d.reply} />
              </>
            )}
            {d.moderation.length > 0 && (
              <>
                <div className="font-medium">审核</div>
                <Table>
                  <TableHeader>
                    <TableRow>
                      <TableHead>阶段</TableHead>
                      <TableHead>结论</TableHead>
                      <TableHead>分数</TableHead>
                      <TableHead>类别</TableHead>
                      <TableHead>动作</TableHead>
                      <TableHead>耗时</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {d.moderation.map((m) => (
                      <TableRow key={m.id}>
                        <TableCell>{m.stage}</TableCell>
                        <TableCell>{m.verdict}</TableCell>
                        <TableCell>{m.score.toFixed(2)}</TableCell>
                        <TableCell className="text-xs">{m.categories.join("、") || "—"}</TableCell>
                        <TableCell>{m.action}</TableCell>
                        <TableCell className="text-xs">{m.latency_ms} ms</TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
              </>
            )}
          </div>
        )}
      </DialogContent>
    </Dialog>
  );
}
