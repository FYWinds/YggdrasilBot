import { useQuery } from "@tanstack/react-query";
import { Link, useNavigate } from "@tanstack/react-router";
import { useState } from "react";
import { listInvocationsOptions } from "@/client/@tanstack/react-query.gen";
import {
  Empty,
  ExternalLink,
  FilterBar,
  GroupSelect,
  Hint,
  OptionSelect,
  PageHeader,
  Pager,
  QueryState,
  useDebounced,
  useGroupName,
} from "@/components/common";
import { fmtMs } from "@/components/transcript";
import { Badge } from "@/components/ui/badge";
import { Input } from "@/components/ui/input";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { fmtTime } from "@/lib/format";

const STATUSES = [
  "running",
  "ok",
  "silent",
  "done",
  "reply",
  "handoff",
  "blocked_input",
  "review",
  "rejected",
  "failed",
  "ratelimited",
  "cancelled",
];
const HANDLERS = ["chat", "tldr", "ops", "report", "privacy.optout", "privacy.optin", "privacy.forget", "admin.review", "admin.status"];
const LIMIT = 50;

export function statusVariant(status: string): "default" | "destructive" | "outline" | "secondary" {
  if (status === "ok") return "default";
  if (status === "failed" || status === "rejected") return "destructive";
  if (status === "running") return "secondary";
  return "outline";
}

export function InvocationsPage() {
  const [q, setQ] = useState("");
  const query = useDebounced(q);
  const [groupId, setGroupId] = useState<number | null>(null);
  const [handler, setHandler] = useState<string | null>(null);
  const [status, setStatus] = useState<string | null>(null);
  const [offset, setOffset] = useState(0);
  const navigate = useNavigate();
  const list = useQuery(
    listInvocationsOptions({ query: { q: query || null, group_id: groupId, handler, status, limit: LIMIT, offset } }),
  );
  const groupName = useGroupName();
  // Every filter change starts from the first page.
  const filter =
    <T,>(set: (v: T) => void) =>
    (v: T) => {
      set(v);
      setOffset(0);
    };
  return (
    <div>
      <PageHeader title="调用记录" description="每次触发的完整记录：点开看完整对话，trace 链接打开 Grafana Tempo。" />
      <FilterBar>
        <Input
          className="w-64"
          aria-label="搜索调用"
          placeholder="搜索请求内容"
          value={q}
          onChange={(e) => filter(setQ)(e.target.value)}
        />
        <GroupSelect className="w-56" value={groupId} onChange={filter(setGroupId)} />
        <OptionSelect
          className="w-44"
          aria-label="handler"
          value={handler}
          onChange={filter(setHandler)}
          noneLabel="全部 handler"
          options={HANDLERS.map((h) => ({ value: h, label: h }))}
        />
        <OptionSelect
          className="w-40"
          aria-label="状态"
          value={status}
          onChange={filter(setStatus)}
          noneLabel="全部状态"
          options={STATUSES.map((s) => ({ value: s, label: s }))}
        />
      </FilterBar>
      <QueryState isLoading={list.isLoading} error={list.error} />
      {list.data?.items.length === 0 && <Empty>没有符合条件的调用</Empty>}
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
              <TableRow
                key={i.id}
                className="cursor-pointer"
                onClick={(e) => {
                  // Links in the row (the detail link, trace) handle their own clicks.
                  if (!(e.target as HTMLElement).closest("a")) {
                    void navigate({ to: "/invocations/$id", params: { id: i.id } });
                  }
                }}
              >
                <TableCell className="text-xs whitespace-nowrap">
                  <Link to="/invocations/$id" params={{ id: i.id }} className="underline-offset-4 hover:underline">
                    {fmtTime(i.created_at)}
                  </Link>
                </TableCell>
                <TableCell className="max-w-36 truncate text-xs">{i.group_id != null ? groupName(i.group_id) : "私聊"}</TableCell>
                <TableCell className="max-w-32 text-xs">
                  <Hint content={i.user_name ? `${i.user_name}（${i.user_id}）` : null}>
                    <div className="truncate">{i.user_name || <span className="font-mono">{i.user_id}</span>}</div>
                  </Hint>
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
                <TableCell className="text-xs">{i.duration_ms != null ? fmtMs(i.duration_ms) : "—"}</TableCell>
                <TableCell className="text-xs">
                  {i.trace_url ? <ExternalLink href={i.trace_url}>打开</ExternalLink> : <span className="text-muted-foreground">—</span>}
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      )}
      {list.data && <Pager total={list.data.total} limit={LIMIT} offset={offset} onChange={setOffset} />}
    </div>
  );
}
