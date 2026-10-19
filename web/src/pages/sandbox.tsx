import { useMutation, useQuery } from "@tanstack/react-query";
import { Trash2Icon } from "lucide-react";
import { toast } from "sonner";
import {
  cancelJobMutation,
  destroyAllWorkspacesMutation,
  destroyWorkspaceMutation,
  listGroupsOptions,
  listJobsOptions,
  listWorkspacesOptions,
} from "@/client/@tanstack/react-query.gen";
import type { JobOut, WorkspaceOut } from "@/client/types.gen";
import { ConfirmButton, Disclosure, Empty, PageHeader, QueryState } from "@/components/common";
import { Badge } from "@/components/ui/badge";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { fmtTime } from "@/lib/format";

export function SandboxPage() {
  const workspaces = useQuery({ ...listWorkspacesOptions(), refetchInterval: 30_000 });
  const groups = useQuery(listGroupsOptions());
  const destroy = useMutation({
    ...destroyWorkspaceMutation(),
    onSuccess: () => {
      toast.success("已销毁");
      void workspaces.refetch();
    },
  });
  const destroyAll = useMutation({
    ...destroyAllWorkspacesMutation(),
    onSuccess: (r) => {
      toast.success(`已销毁 ${r.destroyed} 个工作区`);
      void workspaces.refetch();
    },
  });
  const groupName = (id: number) => groups.data?.find((g) => g.group_id === id)?.name;
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
        description="每个群可以有几个沙箱工作区（一个话题一个，带名字和说明），闲置或到期后自动回收。销毁会删掉里面的全部文件和公开链接，正在进行的调用和后台任务也会被打断。"
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
              <TableHead>Claim</TableHead>
              <TableHead>创建</TableHead>
              <TableHead>最近使用</TableHead>
              <TableHead>到期</TableHead>
              <TableHead>进行中</TableHead>
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
                      <div>{groupName(groupId) || "—"}</div>
                      <div className="font-mono text-xs text-muted-foreground">{groupId}</div>
                      <div className="mt-1 text-xs text-muted-foreground">{items.length} 个工作区</div>
                    </TableCell>
                  )}
                  <TableCell className="max-w-72">
                    <div className="font-mono font-medium">{w.name}</div>
                    <div className="text-xs whitespace-normal text-muted-foreground">{w.description || "—"}</div>
                  </TableCell>
                  <TableCell className="font-mono text-xs">
                    {w.pending ? (
                      <Badge variant="outline">未启动</Badge>
                    ) : (
                      <>
                        <div>{w.claim}</div>
                        <div className="text-muted-foreground">{w.pod_ip || "—"}</div>
                      </>
                    )}
                  </TableCell>
                  <TableCell className="text-xs">{fmtTime(w.created_at)}</TableCell>
                  <TableCell className="text-xs">{fmtTime(w.last_used)}</TableCell>
                  <TableCell className="text-xs">
                    <div>{w.expires_at ? fmtTime(w.expires_at) : "—"}</div>
                    {w.max_until && <div className="text-muted-foreground">最晚 {fmtTime(w.max_until)}</div>}
                  </TableCell>
                  <TableCell>
                    {w.active}
                    {w.recreate && (
                      <Badge variant="outline" className="ml-2">
                        待重建
                      </Badge>
                    )}
                  </TableCell>
                  <TableCell className="text-xs">
                    {w.links.length === 0
                      ? "—"
                      : w.links.map((l) => (
                          <div key={l.port}>
                            <span className="font-mono">:{l.port}</span>{" "}
                            <a className="underline" href={l.url} target="_blank" rel="noreferrer noopener">
                              {l.url}
                            </a>
                            {(l.pid != null || l.command) && (
                              <div className="font-mono text-muted-foreground truncate max-w-80" title={l.command}>
                                {l.pid != null && `pid ${l.pid} `}
                                {l.command}
                              </div>
                            )}
                          </div>
                        ))}
                  </TableCell>
                  <TableCell className="font-mono text-xs">
                    {w.jobs.length === 0 ? "—" : w.jobs.map((id) => <div key={id}>{id}</div>)}
                  </TableCell>
                  <TableCell>
                    <ConfirmButton
                      title={`销毁群 ${groupId} 的工作区 ${w.name}？`}
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

function JobsSection({ groupName }: { groupName: (id: number) => string | undefined }) {
  const jobs = useQuery({ ...listJobsOptions(), refetchInterval: 10_000 });
  const cancel = useMutation({
    ...cancelJobMutation(),
    onSuccess: () => {
      toast.success("已请求取消，几秒内停止");
      void jobs.refetch();
    },
  });
  return (
    <div className="mt-10">
      <h2 className="text-lg font-semibold tracking-tight">后台任务</h2>
      <p className="mt-1 text-sm text-muted-foreground">
        群里发起的后台命令和后台大任务（工作区里的 omp）。结束后 bot 会自动在群里回报结果；记录保留 7 天。
      </p>
      <QueryState isLoading={jobs.isLoading} error={jobs.error} />
      {jobs.data?.items.length === 0 && <Empty>最近没有后台任务</Empty>}
      {!!jobs.data?.items.length && (
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>群</TableHead>
              <TableHead>任务</TableHead>
              <TableHead>状态</TableHead>
              <TableHead>时间</TableHead>
              <TableHead>用量</TableHead>
              <TableHead>详情</TableHead>
              <TableHead />
            </TableRow>
          </TableHeader>
          <TableBody>
            {jobs.data.items.map((j) => (
              <JobRow
                key={j.id}
                job={j}
                groupName={groupName(j.group_id)}
                onCancel={() => cancel.mutate({ path: { job_id: j.id } })}
              />
            ))}
          </TableBody>
        </Table>
      )}
    </div>
  );
}

function JobRow({ job: j, groupName, onCancel }: { job: JobOut; groupName?: string; onCancel: () => void }) {
  const running = j.status === "running" || j.status === "starting";
  return (
    <TableRow className="align-top">
      <TableCell>
        <div>{groupName || "—"}</div>
        <div className="font-mono text-xs text-muted-foreground">{j.group_id}</div>
      </TableCell>
      <TableCell className="max-w-72">
        <div className="font-medium">{j.title}</div>
        <div className="text-xs text-muted-foreground">
          <span className="font-mono">{j.id}</span> · {JOB_KIND[j.kind] ?? j.kind} · 工作区{" "}
          <span className="font-mono">{j.workspace}</span>
          {j.model && ` · ${j.model}`} · 请求者 <span className="font-mono">{j.user_id}</span>
        </div>
        {j.links.map((l) => (
          <div key={l.port} className="text-xs">
            <span className="font-mono">:{l.port}</span>{" "}
            <a className="underline" href={l.url} target="_blank" rel="noreferrer noopener">
              {l.url}
            </a>
          </div>
        ))}
      </TableCell>
      <TableCell className="max-w-64">
        <Badge variant={jobVariant(j.status)}>{JOB_STATUS[j.status] ?? j.status}</Badge>
        {j.cancel && running && (
          <Badge variant="outline" className="ml-2">
            取消中
          </Badge>
        )}
        {j.progress && <div className="mt-1 text-xs text-muted-foreground">{j.progress}</div>}
        {j.error && <div className="mt-1 text-xs text-destructive">{j.error}</div>}
        <div className="mt-1 text-xs text-muted-foreground">
          {REPORT[j.report] ?? j.report}
          {j.report_invocation && <span className="font-mono"> {j.report_invocation.slice(0, 8)}</span>}
        </div>
      </TableCell>
      <TableCell className="text-xs">
        <div>开始 {fmtTime(j.created_at)}</div>
        <div className="text-muted-foreground">
          {j.ended_at ? `结束 ${fmtTime(j.ended_at)}` : `截止 ${fmtTime(j.deadline)}`}
        </div>
      </TableCell>
      <TableCell className="text-xs">
        {j.usage ? (
          <>
            <div>{j.usage.requests} 次请求</div>
            <div className="text-muted-foreground">
              I {fmtTokens(j.usage.input_tokens)} / C {fmtTokens(j.usage.cached_tokens)} / O{" "}
              {fmtTokens(j.usage.output_tokens)}
            </div>
          </>
        ) : (
          "—"
        )}
      </TableCell>
      <TableCell className="max-w-md text-xs">
        <Disclosure label={`事件 ${j.events.length}`}>
          <div className="space-y-0.5">
            {j.events.map((e, i) => (
              <div key={i}>
                <span className="text-muted-foreground">{fmtTime(e.at)}</span> {e.text}
              </div>
            ))}
          </div>
        </Disclosure>
        <Disclosure label={j.kind === "agent" ? "任务" : "脚本"}>
          <pre className="max-h-64 overflow-auto whitespace-pre-wrap font-mono">{j.spec}</pre>
        </Disclosure>
        {j.result && (
          <Disclosure label="结果">
            <pre className="max-h-64 overflow-auto whitespace-pre-wrap font-mono">{j.result}</pre>
          </Disclosure>
        )}
      </TableCell>
      <TableCell>
        {running && !j.cancel && (
          <ConfirmButton
            title={`取消后台任务 ${j.id}？`}
            description="任务会被停止；结束后群里仍会收到回报。"
            onConfirm={onCancel}
          >
            取消
          </ConfirmButton>
        )}
      </TableCell>
    </TableRow>
  );
}
