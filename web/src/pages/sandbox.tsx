import { useMutation, useQuery } from "@tanstack/react-query";
import { Trash2Icon } from "lucide-react";
import { toast } from "sonner";
import {
  destroyAllWorkspacesMutation,
  destroyWorkspaceMutation,
  listGroupsOptions,
  listWorkspacesOptions,
} from "@/client/@tanstack/react-query.gen";
import { ConfirmButton, Empty, PageHeader, QueryState } from "@/components/common";
import { Badge } from "@/components/ui/badge";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { fmtTime } from "@/lib/format";

export function SandboxPage() {
  const workspaces = useQuery({ ...listWorkspacesOptions(), refetchInterval: 30_000 });
  const groups = useQuery(listGroupsOptions());
  const destroy = useMutation({ ...destroyWorkspaceMutation(), onSuccess: () => toast.success("已销毁") });
  const destroyAll = useMutation({
    ...destroyAllWorkspacesMutation(),
    onSuccess: (r) => toast.success(`已销毁 ${r.destroyed} 个工作区`),
  });
  return (
    <div>
      <PageHeader
        title="工作区"
        description="每个群一个沙箱工作区，闲置或到期后自动回收。销毁会删掉里面的全部文件和公开链接，正在进行的调用也会被打断。"
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
      {!!workspaces.data?.items.length && (
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>群</TableHead>
              <TableHead>Claim</TableHead>
              <TableHead>创建</TableHead>
              <TableHead>最近使用</TableHead>
              <TableHead>到期</TableHead>
              <TableHead>进行中</TableHead>
              <TableHead>公开链接</TableHead>
              <TableHead />
            </TableRow>
          </TableHeader>
          <TableBody>
            {workspaces.data.items.map((w) => (
              <TableRow key={w.group_id}>
                <TableCell>
                  <div>{groups.data?.find((g) => g.group_id === w.group_id)?.name || "—"}</div>
                  <div className="font-mono text-xs text-muted-foreground">{w.group_id}</div>
                </TableCell>
                <TableCell className="font-mono text-xs">
                  <div>{w.claim}</div>
                  <div className="text-muted-foreground">{w.pod_ip || "—"}</div>
                </TableCell>
                <TableCell className="text-xs">{fmtTime(w.created_at)}</TableCell>
                <TableCell className="text-xs">{fmtTime(w.last_used)}</TableCell>
                <TableCell className="text-xs">{fmtTime(w.expires_at)}</TableCell>
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
                        <div key={l.port} title={l.command}>
                          <span className="font-mono">:{l.port}</span>{" "}
                          <a className="underline" href={l.url} target="_blank" rel="noreferrer noopener">
                            {l.url}
                          </a>
                        </div>
                      ))}
                </TableCell>
                <TableCell>
                  <ConfirmButton
                    title={`销毁群 ${w.group_id} 的工作区？`}
                    description="文件和公开链接都会被删除；正在使用它的调用会失败。"
                    onConfirm={() => destroy.mutate({ path: { group_id: w.group_id } })}
                  >
                    销毁
                  </ConfirmButton>
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      )}
    </div>
  );
}
