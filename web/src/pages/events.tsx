import { useMutation, useQuery } from "@tanstack/react-query";
import { CheckIcon, XIcon } from "lucide-react";
import { Fragment, useState } from "react";
import { toast } from "sonner";
import { decideEventMutation, listEventsOptions } from "@/client/@tanstack/react-query.gen";
import type { EventOut } from "@/client/types.gen";
import {
  ConfirmButton,
  Empty,
  FilterBar,
  Hint,
  OptionSelect,
  PageHeader,
  Pager,
  Pre,
  QueryState,
  useDebounced,
  useGroupName,
} from "@/components/common";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { fmtTime } from "@/lib/format";

const LIMIT = 50;

/** Kind prefixes offered as filters (the API matches by prefix). */
const KIND_FILTERS: Record<string, string> = {
  "request.": "全部申请",
  "request.group.invite": "加群邀请",
  "request.group.add": "入群申请",
  "request.friend": "好友申请",
  "notice.group_decrease.kick_me": "bot 被踢",
  "notice.group_ban.self": "bot 被禁言",
  "notice.group_admin.self": "bot 管理员变动",
  "notice.bot_offline": "掉线",
  "notice.group_increase": "进群",
  "notice.group_decrease": "退群 / 被踢",
  "notice.group_ban": "禁言",
};

const KIND_LABELS: Record<string, string> = {
  "request.group.invite": "加群邀请",
  "request.group.add": "入群申请",
  "request.friend": "好友申请",
  "notice.group_decrease.kick_me": "bot 被踢出群",
  "notice.group_decrease.leave": "成员退群",
  "notice.group_decrease.kick": "成员被踢",
  "notice.group_increase.self": "bot 进群",
  "notice.group_increase.approve": "成员进群（审批）",
  "notice.group_increase.invite": "成员进群（邀请）",
  "notice.group_ban.self.ban": "bot 被禁言",
  "notice.group_ban.self.lift_ban": "bot 解除禁言",
  "notice.group_ban.all.ban": "全员禁言",
  "notice.group_ban.all.lift_ban": "解除全员禁言",
  "notice.group_ban.member.ban": "成员被禁言",
  "notice.group_ban.member.lift_ban": "成员解除禁言",
  "notice.group_admin.self.set": "bot 成为管理员",
  "notice.group_admin.self.unset": "bot 被取消管理员",
  "notice.group_admin.member.set": "成员成为管理员",
  "notice.group_admin.member.unset": "成员被取消管理员",
  "notice.bot_offline": "QQ 掉线",
};

type Status = "pending" | "approved" | "rejected" | "expired" | "failed";
const STATUS: Record<Status, { label: string; variant: "default" | "secondary" | "outline" | "destructive" }> = {
  pending: { label: "待处理", variant: "default" },
  approved: { label: "已同意", variant: "secondary" },
  rejected: { label: "已拒绝", variant: "outline" },
  expired: { label: "已过期", variant: "outline" },
  failed: { label: "处理失败", variant: "destructive" },
};

function detail(e: EventOut): string {
  const d = e.data as Record<string, unknown>;
  const parts: string[] = [];
  if (e.operator_id) parts.push(`操作人 ${e.operator_id}`);
  if (typeof d.duration === "number" && d.duration > 0) parts.push(`${Math.round(d.duration / 60)} 分钟`);
  if (typeof d.comment === "string" && d.comment) parts.push(`附言：${d.comment}`);
  return parts.join(" · ");
}

export function EventsPage() {
  const [kind, setKind] = useState<string | null>("request.");
  const [status, setStatus] = useState<Status | null>(null);
  const [groupId, setGroupId] = useState("");
  const debouncedGroupId = useDebounced(groupId);
  const [offset, setOffset] = useState(0);
  const [open, setOpen] = useState<number | null>(null);
  const groupName = useGroupName();
  const events = useQuery(
    listEventsOptions({
      query: { kind, status, group_id: debouncedGroupId ? Number(debouncedGroupId) : null, limit: LIMIT, offset },
    }),
  );
  const decide = useMutation({
    ...decideEventMutation(),
    onSuccess: (e) => toast.success(`E${e.id} ${STATUS[e.status as Status]?.label ?? e.status}`),
  });
  const reset = <T,>(set: (v: T) => void) => (v: T) => {
    set(v);
    setOffset(0);
  };

  return (
    <div>
      <PageHeader
        title="事件"
        description="加群和好友申请、bot 自身状态和已启用群的成员变动。保留天数和自动处理策略在设置的 events 一节。"
      />
      <FilterBar>
        <Hint content="填群号；事件也会来自未加入白名单的群，所以这里不是群下拉框">
          <Input
            className="w-36"
            inputMode="numeric"
            placeholder="群号"
            aria-label="群号（可填未启用的群）"
            value={groupId}
            onChange={(e) => reset(setGroupId)(e.target.value.replace(/\D/g, ""))}
          />
        </Hint>
        <OptionSelect
          className="w-40"
          aria-label="类型"
          value={kind}
          onChange={reset(setKind)}
          noneLabel="全部类型"
          options={Object.entries(KIND_FILTERS).map(([value, label]) => ({ value, label }))}
        />
        <OptionSelect
          className="w-32"
          aria-label="状态"
          value={status}
          onChange={(v) => reset(setStatus)(v as Status | null)}
          noneLabel="全部状态"
          options={Object.entries(STATUS).map(([value, s]) => ({ value, label: s.label }))}
        />
      </FilterBar>
      <QueryState isLoading={events.isLoading} error={events.error} />
      {events.data?.items.length === 0 && <Empty>没有符合条件的事件</Empty>}
      {!!events.data?.items.length && (
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead className="w-20">编号</TableHead>
              <TableHead className="w-36">时间</TableHead>
              <TableHead>类型</TableHead>
              <TableHead>群</TableHead>
              <TableHead>对象</TableHead>
              <TableHead>详情</TableHead>
              <TableHead>状态</TableHead>
              <TableHead className="text-right">操作</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {events.data.items.map((e) => (
              <Fragment key={e.id}>
                <TableRow className="cursor-pointer" onClick={() => setOpen(open === e.id ? null : e.id)}>
                  <TableCell className="font-mono text-xs">
                    <Button
                      variant="ghost"
                      size="xs"
                      className="font-mono"
                      aria-expanded={open === e.id}
                      aria-label={`E${e.id} 原始数据`}
                      onClick={(ev) => {
                        ev.stopPropagation();
                        setOpen(open === e.id ? null : e.id);
                      }}
                    >
                      E{e.id}
                    </Button>
                  </TableCell>
                  <TableCell className="text-xs">{fmtTime(e.at)}</TableCell>
                  <TableCell>
                    {KIND_LABELS[e.kind] ?? e.kind}
                  </TableCell>
                  <TableCell className="text-xs">{e.group_id != null ? groupName(e.group_id) : "—"}</TableCell>
                  <TableCell className="font-mono text-xs">{e.user_id ?? "—"}</TableCell>
                  <TableCell className="max-w-72 text-xs">
                    <Hint content={detail(e)}>
                      <div className="truncate">{detail(e) || "—"}</div>
                    </Hint>
                  </TableCell>
                  <TableCell>
                    {e.status ? (
                      <Hint content={e.handled_by === "auto" ? "自动处理" : e.handled_by}>
                        <Badge variant={STATUS[e.status as Status]?.variant ?? "outline"}>
                          {STATUS[e.status as Status]?.label ?? e.status}
                        </Badge>
                      </Hint>
                    ) : (
                      "—"
                    )}
                  </TableCell>
                  <TableCell onClick={(ev) => ev.stopPropagation()}>
                    {e.status === "pending" && (
                      <div className="flex justify-end gap-1.5">
                        <Button
                          size="sm"
                          disabled={decide.isPending}
                          onClick={() => decide.mutate({ path: { event_id: e.id }, body: { approve: true } })}
                        >
                          <CheckIcon data-icon="inline-start" /> 同意
                        </Button>
                        <ConfirmButton
                          title={`拒绝 E${e.id}？`}
                          description="拒绝后无法撤销。"
                          onConfirm={() => decide.mutate({ path: { event_id: e.id }, body: { approve: false } })}
                        >
                          <XIcon data-icon="inline-start" /> 拒绝
                        </ConfirmButton>
                      </div>
                    )}
                  </TableCell>
                </TableRow>
                {open === e.id && (
                  <TableRow>
                    <TableCell colSpan={8}>
                      <Pre value={e.data} />
                    </TableCell>
                  </TableRow>
                )}
              </Fragment>
            ))}
          </TableBody>
        </Table>
      )}
      {events.data && <Pager total={events.data.total} limit={LIMIT} offset={offset} onChange={setOffset} />}
    </div>
  );
}
