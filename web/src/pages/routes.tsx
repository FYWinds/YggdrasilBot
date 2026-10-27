import { useMutation, useQuery } from "@tanstack/react-query";
import { PencilIcon, PlusIcon } from "lucide-react";
import { type FormEvent, useState } from "react";
import { toast } from "sonner";
import {
  createEmbedSpaceMutation,
  deleteRouteMutation,
  listEmbedSpacesOptions,
  listProvidersOptions,
  listRoutesOptions,
  reembedEmbedSpaceMutation,
  upsertRouteMutation,
} from "@/client/@tanstack/react-query.gen";
import type { ProviderOut, RouteIn, TaskRouteRow } from "@/client/types.gen";
import {
  ConfirmButton,
  Empty,
  Field,
  FormDialog,
  Hint,
  JsonEditor,
  NumberInput,
  OptionSelect,
  PageHeader,
  PendingButton,
  QueryState,
} from "@/components/common";
import { ModelSelector } from "@/components/model-caps";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { fmtTime, isPlainObject } from "@/lib/format";

const TASK_LABELS: Record<string, string> = {
  chat: "对话",
  tldr: "TL;DR",
  quick_ack: "快速响应",
  ops: "管理操作",
  agent: "后台 agent",
  vision: "看图",
  sticker: "表情包打标",
  moderation: "审核",
  embedding: "向量",
  rerank: "重排",
  memory_topic: "话题分段",
  memory_glossary: "黑话词典",
  memory_profile: "用户画像",
  memory_digest: "每日摘要",
};

export function RoutesPage() {
  const routes = useQuery(listRoutesOptions());
  const providers = useQuery(listProvidersOptions());
  const [editing, setEditing] = useState<TaskRouteRow | null>(null);
  const remove = useMutation({ ...deleteRouteMutation(), onSuccess: () => toast.success("已删除路由") });
  const providerName = (id: number | null | undefined) =>
    providers.data?.find((p) => p.id === id)?.name ?? (id ? `#${id}` : "—");

  return (
    <div>
      <PageHeader title="模型路由" description="每个任务使用的模型、参数、兜底模型和 UsageLimits。" />
      <QueryState isLoading={routes.isLoading} error={routes.error} />
      {routes.data && (
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>任务</TableHead>
              <TableHead>模型</TableHead>
              <TableHead>兜底</TableHead>
              <TableHead>参数</TableHead>
              <TableHead>限制</TableHead>
              <TableHead className="text-right">操作</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {routes.data.map((row) => {
              const params =
                row.route && Object.keys(row.route.params ?? {}).length ? JSON.stringify(row.route.params) : null;
              const thinking = thinkingOf(row.route?.params);
              return (
                <TableRow key={row.task}>
                  <TableCell>
                    <div className="font-medium">{TASK_LABELS[row.task] ?? row.task}</div>
                  </TableCell>
                  <TableCell>
                    {row.route ? (
                      <>
                        <div className="font-mono text-xs">{row.route.model}</div>
                        <div className="text-xs text-muted-foreground">{providerName(row.route.provider_id)}</div>
                        {thinking && (
                          <Badge variant="outline" className="mt-1">
                            思考 {thinking}
                          </Badge>
                        )}
                      </>
                    ) : (
                      <Badge variant="outline">未配置</Badge>
                    )}
                  </TableCell>
                  <TableCell className="font-mono text-xs">
                    {row.route?.fallback_model
                      ? `${providerName(row.route.fallback_provider_id)} / ${row.route.fallback_model}`
                      : "—"}
                  </TableCell>
                  <TableCell className="max-w-56 font-mono text-xs">
                    {params ? (
                      <Hint content={params}>
                        <span tabIndex={0} className="block truncate rounded-sm outline-none focus-visible:ring-3 focus-visible:ring-ring/50">
                          {params}
                        </span>
                      </Hint>
                    ) : (
                      "—"
                    )}
                  </TableCell>
                  <TableCell className="font-mono text-xs">
                    {row.route?.limits && Object.values(row.route.limits).some((v) => v != null)
                      ? JSON.stringify(Object.fromEntries(Object.entries(row.route.limits).filter(([, v]) => v != null)))
                      : "—"}
                  </TableCell>
                  <TableCell>
                    <div className="flex justify-end gap-1.5">
                      <Button size="sm" variant="outline" onClick={() => setEditing(row)}>
                        <PencilIcon data-icon="inline-start" /> {row.route ? "编辑" : "配置"}
                      </Button>
                      {row.route && (
                        <ConfirmButton
                          title={`删除 ${row.task} 的路由？`}
                          description="删除后该任务不可用，直到重新配置。"
                          onConfirm={() => remove.mutate({ path: { task: row.task } })}
                        >
                          删除
                        </ConfirmButton>
                      )}
                    </div>
                  </TableCell>
                </TableRow>
              );
            })}
          </TableBody>
        </Table>
      )}
      {editing && providers.data && (
        <RouteDialog row={editing} providers={providers.data} onClose={() => setEditing(null)} />
      )}
      <EmbedSpaces providers={providers.data ?? []} />
    </div>
  );
}

function thinkingOf(params: Record<string, unknown> | undefined): string | null {
  const t = params?.thinking;
  return typeof t === "string" ? t : null;
}

function RouteDialog({
  row,
  providers,
  onClose,
}: {
  row: TaskRouteRow;
  providers: ProviderOut[];
  onClose: () => void;
}) {
  const [form, setForm] = useState<RouteIn>({
    provider_id: row.route?.provider_id ?? providers[0]?.id ?? 0,
    model: row.route?.model ?? "",
    params: row.route?.params ?? {},
    fallback_provider_id: row.route?.fallback_provider_id ?? null,
    fallback_model: row.route?.fallback_model ?? null,
    limits: row.route?.limits ?? {},
  });
  const [paramsValid, setParamsValid] = useState(true);
  const save = useMutation({ ...upsertRouteMutation(), onSuccess: onClose });
  const providerOptions = providers.map((p) => ({ value: String(p.id), label: p.name }));
  const limits = form.limits ?? {};
  // The image task is one POST /images: no thinking, fallback or UsageLimits; params go into the request body.
  const image = row.task === "image";
  return (
    <FormDialog
      open
      onOpenChange={(o) => !o && onClose()}
      title={TASK_LABELS[row.task] ?? row.task}
      description={
        image
          ? "画图用哪个 provider 的哪个模型（OpenRouter 的 /images 接口），参数原样放进请求体。"
          : `任务 ${row.task} 用哪个 provider 的哪个模型、调用参数和 UsageLimits。`
      }
      onSubmit={() => save.mutate({ path: { task: row.task }, body: form })}
      pending={save.isPending}
      submitDisabled={!form.model || !form.provider_id || !paramsValid}
      className="sm:max-w-2xl"
    >
      <div className="grid grid-cols-[12rem_1fr] gap-3">
        <Field label="Provider">
          <OptionSelect
            value={String(form.provider_id)}
            onChange={(v) => setForm({ ...form, provider_id: Number(v) })}
            options={providerOptions}
          />
        </Field>
        <Field label="模型" hint={image ? undefined : "「思考」默认不传，由模型决定；开启后更慢、更贵。兜底模型用同一个设置。"}>
          <ModelSelector
            task={row.task}
            provider={providers.find((p) => p.id === form.provider_id)}
            value={form.model}
            onChange={(model) => setForm({ ...form, model })}
            effort={
              image
                ? undefined
                : {
                    value: thinkingOf(form.params),
                    onChange: (v) => {
                      const { thinking: _, ...rest } = form.params ?? {};
                      setForm({ ...form, params: v === null ? rest : { ...rest, thinking: v } });
                    },
                  }
            }
          />
        </Field>
        {!image && (
          <>
            <Field label="兜底 Provider">
              <OptionSelect
                value={form.fallback_provider_id == null ? null : String(form.fallback_provider_id)}
                onChange={(v) => setForm({ ...form, fallback_provider_id: v === null ? null : Number(v) })}
                noneLabel="无"
                options={providerOptions}
              />
            </Field>
            <Field label="兜底模型">
              <ModelSelector
                task={row.task}
                provider={providers.find((p) => p.id === form.fallback_provider_id)}
                value={form.fallback_model ?? ""}
                onChange={(m) => setForm({ ...form, fallback_model: m || null })}
              />
            </Field>
          </>
        )}
      </div>
      <Field
        label="参数"
        hint={
          image
            ? "quality（auto、low、medium、high…）、resolution、output_format、background…"
            : "temperature、max_tokens、top_p、timeout、seed、extra_body…"
        }
      >
        <JsonEditor
          rows={5}
          value={form.params}
          onValidityChange={setParamsValid}
          onChange={(v) => isPlainObject(v) && setForm({ ...form, params: v })}
        />
      </Field>
      <div className={image ? "hidden" : "grid grid-cols-3 gap-3"}>
        <Field label="request_limit">
          <NumberInput
            value={limits.request_limit}
            onChange={(v) => setForm({ ...form, limits: { ...limits, request_limit: v } })}
            placeholder="默认"
          />
        </Field>
        <Field label="tool_calls_limit">
          <NumberInput
            value={limits.tool_calls_limit}
            onChange={(v) => setForm({ ...form, limits: { ...limits, tool_calls_limit: v } })}
            placeholder="默认"
          />
        </Field>
        <Field label="total_tokens_limit">
          <NumberInput
            value={limits.total_tokens_limit}
            onChange={(v) => setForm({ ...form, limits: { ...limits, total_tokens_limit: v } })}
            placeholder="默认"
          />
        </Field>
      </div>
    </FormDialog>
  );
}

function EmbedSpaces({ providers }: { providers: ProviderOut[] }) {
  const spaces = useQuery(listEmbedSpacesOptions());
  const [providerId, setProviderId] = useState<number | null>(null);
  const [model, setModel] = useState("");
  const create = useMutation({ ...createEmbedSpaceMutation(),
      onSuccess: () => {
        toast.success("已创建，重嵌入任务已入队");
        setModel("");
      },
    });
  const reembed = useMutation({ ...reembedEmbedSpaceMutation(), onSuccess: () => toast.success("已重新入队") });
  const submit = (e: FormEvent) => {
    e.preventDefault();
    if (providerId && model && !create.isPending) create.mutate({ body: { provider_id: providerId, model } });
  };
  return (
    <Card className="mt-8">
      <CardHeader>
        <CardTitle>向量空间</CardTitle>
        <CardDescription>
          更换 embedding 模型时新建一个空间，worker 会把全部记忆重新嵌入，完成后切换为生效。维度固定为 1024。
        </CardDescription>
      </CardHeader>
      <CardContent className="grid gap-4">
        <QueryState isLoading={spaces.isLoading} error={spaces.error} />
        {spaces.data?.length === 0 && <Empty description="用下面的表单新建第一个向量空间。">还没有向量空间</Empty>}
        {!!spaces.data?.length && (
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>ID</TableHead>
                <TableHead>模型</TableHead>
                <TableHead>维度</TableHead>
                <TableHead>状态</TableHead>
                <TableHead>创建时间</TableHead>
                <TableHead className="text-right">
                  <span className="sr-only">操作</span>
                </TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {spaces.data.map((s) => (
                <TableRow key={s.id}>
                  <TableCell>{s.id}</TableCell>
                  <TableCell className="font-mono text-xs">
                    {s.provider_name ?? `#${s.provider_id}`} / {s.model}
                  </TableCell>
                  <TableCell>{s.dim}</TableCell>
                  <TableCell>
                    <div className="flex gap-1">
                      {s.active && <Badge>生效中</Badge>}
                      <Badge variant="outline">{s.status}</Badge>
                    </div>
                  </TableCell>
                  <TableCell className="text-xs">{fmtTime(s.created_at)}</TableCell>
                  <TableCell className="text-right">
                    {!s.active && (
                      <PendingButton
                        size="sm"
                        variant="outline"
                        pending={reembed.isPending && reembed.variables?.path.space_id === s.id}
                        disabled={reembed.isPending}
                        onClick={() => reembed.mutate({ path: { space_id: s.id } })}
                      >
                        重新入队
                      </PendingButton>
                    )}
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        )}
        <form onSubmit={submit} className="flex flex-wrap items-start gap-3">
          <Field label="Provider" className="w-48">
            <OptionSelect
              value={providerId === null ? null : String(providerId)}
              onChange={(v) => setProviderId(v === null ? null : Number(v))}
              options={providers.map((p) => ({ value: String(p.id), label: p.name }))}
            />
          </Field>
          <Field label="模型" className="w-80">
            <ModelSelector
              task="embedding"
              provider={providers.find((p) => p.id === providerId)}
              value={model}
              onChange={setModel}
            />
          </Field>
          <PendingButton type="submit" className="mt-7" pending={create.isPending} disabled={!providerId || !model}>
            {!create.isPending && <PlusIcon data-icon="inline-start" />}
            新建并重嵌入
          </PendingButton>
        </form>
      </CardContent>
    </Card>
  );
}
