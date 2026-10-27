import { useMutation, useQuery } from "@tanstack/react-query";
import { Link, useNavigate } from "@tanstack/react-router";
import { Trash2Icon } from "lucide-react";
import { useState } from "react";
import { toast } from "sonner";
import {
  destroyAllWorkspacesMutation,
  destroyWorkspaceMutation,
  listJobsOptions,
  listWorkspacesOptions,
} from "@/client/@tanstack/react-query.gen";
import type { JobOut, WorkspaceOut } from "@/client/types.gen";
import {
  ConfirmButton,
  Empty,
  ExternalLink,
  FilterBar,
  GroupSelect,
  Hint,
  OptionSelect,
  PageHeader,
  Pager,
  QueryState,
  SectionHeader,
  useDebounced,
  useGroupName,
} from "@/components/common";
import { fmtTokens } from "@/components/transcript";
import { Badge } from "@/components/ui/badge";
import { Input } from "@/components/ui/input";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { fmtTime } from "@/lib/format";

const JOBS_LIMIT = 50;

export function SandboxPage() {
  const workspaces = useQuery({ ...listWorkspacesOptions(), refetchInterval: 30_000, retry: false });
  const destroy = useMutation({
    ...destroyWorkspaceMutation(),
    onSuccess: () => toast.success("已销毁"),
  });
  const destroyAll = useMutation({
    ...destroyAllWorkspacesMutation(),
    onSuccess: (r) => toast.success(`已销毁 ${r.destroyed} 个工作区`),
  });
  const groupName = useGroupName();
  // The API lists each group's workspaces together; one row per workspace, the group cell spanning them.
  const byGroup: [number, WorkspaceOut[]][] = [];
  for (const w of workspaces.data?.items ?? []) {
    const last = byGroup[byGroup.length - 1];
    if (last && last[0] === w.group_id) last[1].push(w);
    else byGroup.push([w.group_id, [w]]);
  }
  return (
    <div>
      <PageHeader
        title="工作区"
        description="每个群可以有几个沙箱工作区，闲置或到期后自动回收，文件随之删除。"
        actions={
          !!workspaces.data?.items.length && (
            <ConfirmButton
              variant="destructive"
              size="default"
              title="销毁全部工作区？"
              description="所有群的工作区、文件和公开链接都会被删除，正在进行的调用会失败。"
              onConfirm={() => destroyAll.mutate({})}
            >
              <Trash2Icon data-icon="inline-start" /> 销毁全部
            </ConfirmButton>
          )
        }
      />
      <QueryState isLoading={workspaces.isLoading} error={workspaces.error} />
      {workspaces.data?.items.length === 0 && <Empty>当前没有工作区</Empty>}
      {byGroup.length > 0 && (
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>群</TableHead>
              <TableHead>工作区</TableHead>
              <TableHead>最近使用</TableHead>
              <TableHead>到期</TableHead>
              <TableHead>使用中</TableHead>
              <TableHead>公开链接</TableHead>
              <TableHead>后台任务</TableHead>
              <TableHead>
                <span className="sr-only">操作</span>
              </TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {byGroup.map(([groupId, items]) =>
              items.map((w, i) => (
                <TableRow key={`${groupId}/${w.name}`} className="align-top">
                  {i === 0 && (
                    <TableCell rowSpan={items.length} className="border-r">
                      {groupName(groupId)}
                    </TableCell>
                  )}
                  <TableCell className="max-w-80 whitespace-normal">
                    <div className="flex items-center gap-2">
                      <span className="font-mono font-medium">{w.name}</span>
                      {w.pending && <Badge variant="outline">未启动</Badge>}
                      {w.recreate && <Badge variant="outline">待重建</Badge>}
                    </div>
                    {w.description && <div className="text-xs text-muted-foreground">{w.description}</div>}
                  </TableCell>
                  <TableCell className="text-xs">{fmtTime(w.last_used)}</TableCell>
                  <TableCell className="text-xs">
                    {w.expires_at ? (
                      <Hint content={w.max_until ? `最晚 ${fmtTime(w.max_until)}` : null}>
                        <span>{fmtTime(w.expires_at)}</span>
                      </Hint>
                    ) : (
                      "—"
                    )}
                  </TableCell>
                  <TableCell>{w.active || "—"}</TableCell>
                  <TableCell className="text-xs">
                    {w.links.length === 0
                      ? "—"
                      : w.links.map((l) => (
                          <div key={l.port}>
                            <ExternalLink href={l.url} />
                          </div>
                        ))}
                  </TableCell>
                  <TableCell>{w.jobs.length || "—"}</TableCell>
                  <TableCell>
                    <ConfirmButton
                      title={`销毁工作区 ${w.name}？`}
                      description="文件和公开链接都会被删除；正在使用它的调用和后台任务会失败。"
                      onConfirm={() => destroy.mutate({ path: { group_id: groupId, name: w.name } })}
                    >
                      销毁
                    </ConfirmButton>
                  </TableCell>
                </TableRow>
              )),
            )}
          </TableBody>
        </Table>
      )}
      <JobsSection groupName={groupName} />
    </div>
  );
}

export const JOB_STATUS: Record<string, string> = {
  starting: "启动中",
  running: "运行中",
  succeeded: "已完成",
  failed: "失败",
  timeout: "超时",
  cancelled: "已取消",
};
export const JOB_KIND: Record<string, string> = { command: "后台命令", agent: "后台大任务" };
export const REPORT: Record<string, string> = {
  "": "—",
  pending: "待回报",
  running: "回报中",
  done: "已回报",
  skipped: "不回报",
  failed: "回报失败",
};

export function jobVariant(status: string): "default" | "destructive" | "outline" | "secondary" {
  if (status === "running" || status === "starting") return "default";
  if (status === "succeeded") return "secondary";
  if (status === "failed" || status === "timeout") return "destructive";
  return "outline";
}

export function fmtDuration(j: Pick<JobOut, "created_at" | "ended_at">): string {
  const end = j.ended_at ? new Date(j.ended_at).getTime() : Date.now();
  const s = Math.max(0, Math.round((end - new Date(j.created_at).getTime()) / 1000));
  if (s < 60) return `${s} 秒`;
  if (s < 3600) return `${Math.floor(s / 60)} 分 ${s % 60} 秒`;
  return `${Math.floor(s / 3600)} 小时 ${Math.floor((s % 3600) / 60)} 分`;
}

function JobsSection({ groupName }: { groupName: (id: number) => string }) {
  const [groupId, setGroupId] = useState<number | null>(null);
  const [status, setStatus] = useState<string | null>(null);
  const [kind, setKind] = useState<string | null>(null);
  const [q, setQ] = useState("");
  const [offset, setOffset] = useState(0);
  const navigate = useNavigate();
  const query = useDebounced(q);
  const jobs = useQuery({
    ...listJobsOptions({
      query: {
        group_id: groupId,
        status: status as "running" | "succeeded" | "failed" | "timeout" | "cancelled" | null,
        kind: kind as "command" | "agent" | null,
        q: query || null,
        limit: JOBS_LIMIT,
        offset,
      },
    }),
    refetchInterval: 10_000,
  });
  // Every filter change starts from the first page.
  const filter =
    <T,>(set: (v: T) => void) =>
    (v: T) => {
      set(v);
      setOffset(0);
    };
  return (
    <section className="mt-10">
      <SectionHeader title="后台任务" className="mb-3" />
      <FilterBar>
        <Input
          className="w-64"
          aria-label="搜索后台任务"
          placeholder="搜索标题 / 任务 / 结果"
          value={q}
          onChange={(e) => filter(setQ)(e.target.value)}
        />
        <GroupSelect className="w-56" value={groupId} onChange={filter(setGroupId)} />
        <OptionSelect
          className="w-28"
          aria-label="状态"
          value={status}
          onChange={filter(setStatus)}
          noneLabel="全部状态"
          options={[
            { value: "running", label: "运行中" },
            { value: "succeeded", label: "已完成" },
            { value: "failed", label: "失败" },
            { value: "timeout", label: "超时" },
            { value: "cancelled", label: "已取消" },
          ]}
        />
        <OptionSelect
          className="w-32"
          aria-label="类型"
          value={kind}
          onChange={filter(setKind)}
          noneLabel="全部类型"
          options={Object.entries(JOB_KIND).map(([value, label]) => ({ value, label }))}
        />
      </FilterBar>
      <QueryState isLoading={jobs.isLoading} error={jobs.error} />
      {jobs.data?.items.length === 0 && <Empty>没有符合条件的后台任务</Empty>}
      {!!jobs.data?.items.length && (
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>任务</TableHead>
              <TableHead>群</TableHead>
              <TableHead>类型</TableHead>
              <TableHead>状态</TableHead>
              <TableHead>开始</TableHead>
              <TableHead>耗时</TableHead>
              <TableHead>用量</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {jobs.data.items.map((j) => (
              <TableRow
                key={j.id}
                className="cursor-pointer"
                onClick={(e) => {
                  if (!(e.target as HTMLElement).closest("a")) {
                    void navigate({ to: "/sandbox/jobs/$id", params: { id: j.id } });
                  }
                }}
              >
                <TableCell className="max-w-96 font-medium">
                  <Hint content={j.title}>
                    <Link
                      to="/sandbox/jobs/$id"
                      params={{ id: j.id }}
                      className="block truncate underline-offset-4 hover:underline"
                    >
                      {j.title}
                    </Link>
                  </Hint>
                </TableCell>
                <TableCell>{groupName(j.group_id)}</TableCell>
                <TableCell className="text-xs">{JOB_KIND[j.kind] ?? j.kind}</TableCell>
                <TableCell className="max-w-72">
                  <div className="flex items-center gap-2">
                    <Badge variant={jobVariant(j.status)}>{JOB_STATUS[j.status] ?? j.status}</Badge>
                    {j.cancel && (j.status === "running" || j.status === "starting") && (
                      <Badge variant="outline">取消中</Badge>
                    )}
                    {(j.status === "running" || j.status === "starting") && j.progress && (
                      <span className="truncate text-xs text-muted-foreground">{j.progress}</span>
                    )}
                  </div>
                </TableCell>
                <TableCell className="text-xs">{fmtTime(j.created_at)}</TableCell>
                <TableCell className="text-xs">{fmtDuration(j)}</TableCell>
                <TableCell className="text-xs">
                  {j.usage
                    ? `${j.usage.requests} 次 · ${fmtTokens(j.usage.input_tokens + j.usage.output_tokens)} tokens`
                    : "—"}
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      )}
      {jobs.data && <Pager total={jobs.data.total} limit={JOBS_LIMIT} offset={offset} onChange={setOffset} />}
    </section>
  );
}
