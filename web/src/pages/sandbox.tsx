import { useMutation, useQuery } from "@tanstack/react-query";
import { Trash2Icon } from "lucide-react";
import { type ReactNode, useState } from "react";
import { toast } from "sonner";
import {
  cancelJobMutation,
  destroyAllWorkspacesMutation,
  destroyWorkspaceMutation,
  getJobOptions,
  listJobsOptions,
  listWorkspacesOptions,
} from "@/client/@tanstack/react-query.gen";
import type { JobOut, WorkspaceOut } from "@/client/types.gen";
import {
  ConfirmButton,
  Empty,
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
import { Dialog, DialogContent, DialogHeader, DialogTitle } from "@/components/ui/dialog";
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
              <Trash2Icon /> 销毁全部
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
              <TableHead />
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
                            <a className="underline" href={l.url} target="_blank" rel="noreferrer noopener">
                              {l.url}
                            </a>
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

const JOB_STATUS: Record<string, string> = {
  starting: "启动中",
  running: "运行中",
  succeeded: "已完成",
  failed: "失败",
  timeout: "超时",
  cancelled: "已取消",
};
const JOB_KIND: Record<string, string> = { command: "后台命令", agent: "后台大任务" };
const REPORT: Record<string, string> = {
  "": "—",
  pending: "待回报",
  running: "回报中",
  done: "已回报",
  skipped: "不回报",
  failed: "回报失败",
};

function jobVariant(status: string): "default" | "destructive" | "outline" | "secondary" {
  if (status === "running" || status === "starting") return "default";
  if (status === "succeeded") return "secondary";
  if (status === "failed" || status === "timeout") return "destructive";
  return "outline";
}

function fmtTokens(n: number): string {
  return n >= 1000 ? `${(n / 1000).toFixed(1)}k` : String(n);
}

function fmtDuration(j: JobOut): string {
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
  const [selected, setSelected] = useState<string | null>(null);
  const jobs = useQuery({
    ...listJobsOptions({
      query: {
        group_id: groupId,
        status: status as "running" | "succeeded" | "failed" | "timeout" | "cancelled" | null,
        kind: kind as "command" | "agent" | null,
        q: q || null,
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
    <div className="mt-10">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h2 className="text-lg font-semibold tracking-tight">后台任务</h2>
        <div className="flex flex-wrap items-center gap-2">
          <Input
            className="w-56"
            placeholder="搜索标题 / 任务 / 结果"
            value={q}
            onChange={(e) => filter(setQ)(e.target.value)}
          />
          <GroupSelect className="w-56" value={groupId} onChange={filter(setGroupId)} />
          <OptionSelect
            className="w-28"
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
            value={kind}
            onChange={filter(setKind)}
            noneLabel="全部类型"
            options={Object.entries(JOB_KIND).map(([value, label]) => ({ value, label }))}
          />
        </div>
      </div>
      <QueryState isLoading={jobs.isLoading} error={jobs.error} />
      {jobs.data?.items.length === 0 && <Empty>没有符合条件的后台任务</Empty>}
      {!!jobs.data?.items.length && (
        <Table className="mt-2">
          <TableHeader>
            <TableRow>
              <TableHead>群</TableHead>
              <TableHead>任务</TableHead>
              <TableHead>类型</TableHead>
              <TableHead>状态</TableHead>
              <TableHead>开始</TableHead>
              <TableHead>耗时</TableHead>
              <TableHead>用量</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {jobs.data.items.map((j) => (
              <TableRow key={j.id} className="cursor-pointer" onClick={() => setSelected(j.id)}>
                <TableCell>{groupName(j.group_id)}</TableCell>
                <TableCell className="max-w-96 truncate font-medium">{j.title}</TableCell>
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
      {selected && <JobDialog id={selected} groupName={groupName} onClose={() => setSelected(null)} />}
    </div>
  );
}

function JobDialog({ id, groupName, onClose }: { id: string; groupName: (id: number) => string; onClose: () => void }) {
  const detail = useQuery({ ...getJobOptions({ path: { job_id: id } }), refetchInterval: 5_000 });
  const cancel = useMutation({
    ...cancelJobMutation(),
    onSuccess: () => toast.success("已请求取消，几秒内停止"),
  });
  const d = detail.data;
  const running = d?.status === "running" || d?.status === "starting";
  const facts: [string, ReactNode][] = d
    ? [
        ["群", groupName(d.group_id)],
        ["请求者", <span className="font-mono">{d.user_id}</span>],
        ["工作区", <span className="font-mono">{d.workspace}</span>],
        ...(d.model ? ([["模型", <span className="font-mono">{d.model}</span>]] as [string, ReactNode][]) : []),
        ["开始", fmtTime(d.created_at)],
        [d.ended_at ? "结束" : "截止", fmtTime(d.ended_at ?? d.deadline)],
        ["耗时", fmtDuration(d)],
        [
          "用量",
          d.usage
            ? `${d.usage.requests} 次请求 · 输入 ${fmtTokens(d.usage.input_tokens)}（缓存 ${fmtTokens(d.usage.cached_tokens)}）· 输出 ${fmtTokens(d.usage.output_tokens)}`
            : "—",
        ],
        [
          "回报",
          <>
            {REPORT[d.report] ?? d.report}
            {d.report_invocation && <span className="ml-2 font-mono text-muted-foreground">{d.report_invocation}</span>}
          </>,
        ],
      ]
    : [];
  return (
    <Dialog open onOpenChange={(o) => !o && onClose()}>
      <DialogContent className="max-h-[90svh] overflow-y-auto sm:max-w-4xl">
        <DialogHeader>
          <DialogTitle className="flex flex-wrap items-center gap-2 pr-6">
            {d?.title ?? id}
            {d && <Badge variant={jobVariant(d.status)}>{JOB_STATUS[d.status] ?? d.status}</Badge>}
            {d && <Badge variant="outline">{JOB_KIND[d.kind] ?? d.kind}</Badge>}
            <span className="font-mono text-xs font-normal text-muted-foreground">{id}</span>
          </DialogTitle>
        </DialogHeader>
        <QueryState isLoading={detail.isLoading} error={detail.error} />
        {d && (
          <div className="grid gap-4 text-sm">
            <dl className="grid grid-cols-[auto_minmax(0,1fr)] gap-x-4 gap-y-1 text-xs">
              {facts.map(([label, value]) => (
                <div key={label} className="contents">
                  <dt className="text-muted-foreground">{label}</dt>
                  <dd className="break-words">{value}</dd>
                </div>
              ))}
            </dl>
            {running && !d.cancel && (
              <div>
                <ConfirmButton
                  title="取消这个后台任务？"
                  description="任务会被停止；结束后群里仍会收到回报。"
                  onConfirm={() => cancel.mutate({ path: { job_id: d.id } })}
                >
                  取消任务
                </ConfirmButton>
              </div>
            )}
            {d.progress && running && <p className="text-muted-foreground">进度：{d.progress}</p>}
            {d.error && <Pre value={d.error} className="text-destructive" />}
            {d.links.length > 0 && (
              <div>
                <div className="mb-1 font-medium">公开链接</div>
                {d.links.map((l) => (
                  <div key={l.port} className="text-xs">
                    <a className="underline" href={l.url} target="_blank" rel="noreferrer noopener">
                      {l.url}
                    </a>
                  </div>
                ))}
              </div>
            )}
            {d.images.length > 0 && (
              <div>
                <div className="mb-1 font-medium">发到群里的图片</div>
                {d.images.map((p) => (
                  <div key={p} className="font-mono text-xs">
                    {p}
                  </div>
                ))}
              </div>
            )}
            <div>
              <div className="mb-1 font-medium">{d.kind === "agent" ? "任务" : "脚本"}</div>
              <Pre value={d.spec} className="whitespace-pre-wrap" />
            </div>
            {d.result && (
              <div>
                <div className="mb-1 font-medium">结果</div>
                <Pre value={d.result} className="whitespace-pre-wrap" />
              </div>
            )}
            <div>
              <div className="mb-1 font-medium">事件（{d.events.length}）</div>
              <div className="max-h-96 overflow-y-auto rounded-lg border">
                <table className="w-full text-xs">
                  <tbody>
                    {d.events.map((e, i) => (
                      <tr key={i} className="border-b align-top last:border-0">
                        <td className="w-40 px-2 py-1 whitespace-nowrap text-muted-foreground">{fmtTime(e.at)}</td>
                        <td className="px-2 py-1 break-words">{e.text}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          </div>
        )}
      </DialogContent>
    </Dialog>
  );
}
