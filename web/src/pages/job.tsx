import { useMutation, useQuery } from "@tanstack/react-query";
import { Link, useParams } from "@tanstack/react-router";
import { ArrowLeftIcon } from "lucide-react";
import { toast } from "sonner";
import { cancelJobMutation, getJobOptions, getJobTranscriptOptions } from "@/client/@tanstack/react-query.gen";
import { ConfirmButton, ExternalLink, PageHeader, QueryState, SectionHeader, useGroupName } from "@/components/common";
import { Code, ContextUsage, Facts, Transcript } from "@/components/transcript";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { ScrollArea } from "@/components/ui/scroll-area";
import { Separator } from "@/components/ui/separator";
import { fmtTime } from "@/lib/format";
import { JOB_KIND, JOB_STATUS, REPORT, fmtDuration, jobVariant } from "@/pages/sandbox";

export function JobPage() {
  const { id } = useParams({ from: "/sandbox/jobs/$id" });
  const detail = useQuery({
    ...getJobOptions({ path: { job_id: id } }),
    refetchInterval: (q) => (["running", "starting"].includes(q.state.data?.status ?? "") ? 3000 : false),
  });
  const d = detail.data;
  const running = d?.status === "running" || d?.status === "starting";
  const transcript = useQuery({
    ...getJobTranscriptOptions({ path: { job_id: id } }),
    refetchInterval: (q) => (q.state.data?.live || running ? 3000 : false),
  });
  const cancel = useMutation({
    ...cancelJobMutation(),
    onSuccess: () => toast.success("已请求取消，几秒内停止"),
  });
  const groupName = useGroupName();
  const t = transcript.data;
  return (
    <div>
      <Button variant="link" size="sm" className="mb-2 px-0" asChild>
        <Link to="/sandbox">
          <ArrowLeftIcon data-icon="inline-start" />
          工作区
        </Link>
      </Button>
      <PageHeader
        title={d?.title ?? "后台任务"}
        description={<span className="font-mono">{id}</span>}
        actions={
          d &&
          running &&
          !d.cancel && (
            <ConfirmButton
              size="default"
              title="取消这个后台任务？"
              description="任务会被停止；结束后群里仍会收到回报。"
              onConfirm={() => cancel.mutate({ path: { job_id: d.id } })}
            >
              取消任务
            </ConfirmButton>
          )
        }
      />
      <QueryState isLoading={detail.isLoading} error={detail.error} variant="card" />
      {d && (
        <div className="grid items-start gap-6 xl:grid-cols-[minmax(0,1fr)_22rem]">
          <aside aria-label="任务信息" className="xl:sticky xl:top-6 xl:col-start-2 xl:row-start-1">
            <ScrollArea viewportClassName="xl:max-h-[calc(100svh-3rem)]">
              <div className="grid gap-4 rounded-xl border p-4">
                <div className="flex flex-wrap items-center gap-2">
                  <Badge variant={jobVariant(d.status)}>{JOB_STATUS[d.status] ?? d.status}</Badge>
                  <Badge variant="outline">{JOB_KIND[d.kind] ?? d.kind}</Badge>
                  {d.cancel && running && <Badge variant="outline">取消中</Badge>}
                </div>
                {running && d.progress && <p className="text-sm text-muted-foreground">进度：{d.progress}</p>}
                <Facts
                  items={[
                    ["群", groupName(d.group_id)],
                    ["请求者", <span className="font-mono">{d.user_id}</span>],
                    ["工作区", <span className="font-mono">{d.workspace}</span>],
                    ["模型", d.model ? <span className="font-mono text-xs">{d.model}</span> : "—"],
                    ["开始", fmtTime(d.created_at)],
                    [d.ended_at ? "结束" : "截止", fmtTime(d.ended_at ?? d.deadline)],
                    ["耗时", fmtDuration(d)],
                    [
                      "用量",
                      d.usage ? (
                        <ContextUsage
                          model={d.model}
                          input={d.usage.input_tokens}
                          cached={d.usage.cached_tokens}
                          output={d.usage.output_tokens}
                          requests={d.usage.requests}
                          transcript={t}
                        />
                      ) : (
                        "—"
                      ),
                    ],
                    [
                      "回报",
                      <div className="flex flex-wrap items-center gap-2">
                        {REPORT[d.report] ?? d.report}
                        {d.report_invocation && (
                          <Button variant="link" size="xs" className="h-auto px-0" asChild>
                            <Link to="/invocations/$id" params={{ id: d.report_invocation }}>
                              查看回报调用
                            </Link>
                          </Button>
                        )}
                      </div>,
                    ],
                  ]}
                />
                {d.error && <Code title="错误" value={d.error} variant="destructive" className="max-h-60" />}
                {d.links.length > 0 && (
                  <>
                    <Separator />
                    <div className="grid gap-1">
                      <SectionHeader level={3} title="公开链接" />
                      {d.links.map((l) => (
                        <ExternalLink key={l.port} href={l.url} className="text-xs break-all" />
                      ))}
                    </div>
                  </>
                )}
                {d.images.length > 0 && (
                  <>
                    <Separator />
                    <div className="grid gap-1">
                      <SectionHeader level={3} title="发到群里的图片" />
                      {d.images.map((p) => (
                        <span key={p} className="font-mono text-xs break-all">
                          {p}
                        </span>
                      ))}
                    </div>
                  </>
                )}
              </div>
            </ScrollArea>
          </aside>
          <section aria-label="对话" className="grid min-w-0 gap-5 xl:col-start-1 xl:row-start-1">
            <QueryState isLoading={transcript.isLoading} error={transcript.error} variant="card" />
            {t && <Transcript transcript={t} startedAt={d.created_at} />}
            {t && !t.available && d.result && <Code title="结果" value={d.result} />}
          </section>
        </div>
      )}
    </div>
  );
}
