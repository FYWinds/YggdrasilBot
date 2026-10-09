import { useMutation, useQuery } from "@tanstack/react-query";
import { diffLines } from "diff";
import { useEffect, useState } from "react";
import { toast } from "sonner";
import {
  activatePromptVersionMutation,
  createPromptVersionMutation,
  getPromptOptions,
  listPromptsOptions,
  previewPromptMutation,
  resetPromptMutation,
} from "@/client/@tanstack/react-query.gen";
import type { PromptDetail } from "@/client/types.gen";
import { ConfirmButton, Empty, Field, JsonEditor, PageHeader, Pre, QueryState } from "@/components/common";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Textarea } from "@/components/ui/textarea";
import { fmtTime, isPlainObject } from "@/lib/format";
import { cn } from "@/lib/utils";

export function PromptsPage() {
  const list = useQuery(listPromptsOptions());
  const [key, setKey] = useState<string | null>(null);
  const current = key ?? list.data?.[0]?.key ?? null;
  return (
    <div>
      <PageHeader title="提示词" description="所有模板都可以覆盖；每次保存一个版本，可随时切换或恢复默认。模板语法为 Jinja2。" />
      <QueryState isLoading={list.isLoading} error={list.error} />
      {list.data?.length === 0 && <Empty>没有可编辑的模板</Empty>}
      {!!list.data?.length && (
        <div className="flex gap-6">
          <nav className="flex w-56 shrink-0 flex-col gap-0.5">
            {list.data.map((p) => (
              <Button
                key={p.key}
                variant="ghost"
                onClick={() => setKey(p.key)}
                className={cn("justify-between", current === p.key && "bg-muted font-medium")}
              >
                <span className="font-mono">{p.key}</span>
                {p.active_version_id ? <Badge>v{p.active_version_id}</Badge> : <Badge variant="outline">默认</Badge>}
              </Button>
            ))}
          </nav>
          <div className="min-w-0 flex-1">{current && <PromptEditor key={current} promptKey={current} />}</div>
        </div>
      )}
    </div>
  );
}

function activeBody(d: PromptDetail): string {
  return d.versions.find((v) => v.id === d.active_version_id)?.body ?? d.default_body;
}

function DiffView({ from, to }: { from: string; to: string }) {
  const parts = diffLines(from, to);
  if (parts.every((p) => !p.added && !p.removed)) return <Empty>与默认模板相同</Empty>;
  return (
    <pre className="max-h-[32rem] overflow-auto rounded-lg border text-xs">
      {parts.map((p, i) => (
        <div
          key={i}
          className={cn(
            "px-3 whitespace-pre-wrap",
            p.added && "bg-green-500/15 text-green-900",
            p.removed && "bg-red-500/15 text-red-900 line-through decoration-red-900/40",
          )}
        >
          {p.value
            .replace(/\n$/, "")
            .split("\n")
            .map((line) => `${p.added ? "+" : p.removed ? "-" : " "} ${line}`)
            .join("\n")}
        </div>
      ))}
    </pre>
  );
}

function PromptEditor({ promptKey }: { promptKey: string }) {
  const detail = useQuery(getPromptOptions({ path: { key: promptKey } }));
  const [body, setBody] = useState("");
  const [note, setNote] = useState("");
  const [activate, setActivate] = useState(true);
  const [vars, setVars] = useState<Record<string, unknown>>({});
  const create = useMutation({ ...createPromptVersionMutation(),
      onSuccess: (v) => {
        toast.success(`已保存版本 v${v.id}`);
        setNote("");
      },
    });
  const activateM = useMutation({ ...activatePromptVersionMutation(), onSuccess: () => toast.success("已切换版本") });
  const reset = useMutation({ ...resetPromptMutation(), onSuccess: () => toast.success("已恢复默认模板") });
  const preview = useMutation(previewPromptMutation());

  useEffect(() => {
    if (!detail.data) return;
    setBody(activeBody(detail.data));
    setVars((old) => Object.fromEntries(detail.data.variables.map((v) => [v, old[v] ?? ""])));
  }, [detail.data]);

  if (!detail.data) return <QueryState isLoading={detail.isLoading} error={detail.error} />;
  const d = detail.data;
  const changed = body !== activeBody(d);

  return (
    <div className="grid gap-6">
      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <span className="font-mono">{promptKey}</span>
            {d.active_version_id ? <Badge>生效 v{d.active_version_id}</Badge> : <Badge variant="outline">使用默认模板</Badge>}
          </CardTitle>
        </CardHeader>
        <CardContent>
          <Tabs defaultValue="edit">
            <TabsList>
              <TabsTrigger value="edit">编辑</TabsTrigger>
              <TabsTrigger value="diff">与默认对比</TabsTrigger>
              <TabsTrigger value="preview">预览</TabsTrigger>
            </TabsList>
            <TabsContent value="edit" className="grid gap-3">
              <Textarea
                className="min-h-96 font-mono text-xs"
                spellCheck={false}
                value={body}
                onChange={(e) => setBody(e.target.value)}
              />
              <div className="flex flex-wrap items-center gap-3">
                <Input className="w-80" placeholder="版本备注" value={note} onChange={(e) => setNote(e.target.value)} />
                <label className="flex items-center gap-2 text-sm">
                  <Checkbox checked={activate} onCheckedChange={(c) => setActivate(c === true)} />
                  保存后立即生效
                </label>
                <Button
                  disabled={!changed || !body.trim() || create.isPending}
                  onClick={() => create.mutate({ path: { key: promptKey }, body: { body, note, activate } })}
                >
                  保存为新版本
                </Button>
                <Button variant="outline" disabled={!changed} onClick={() => setBody(activeBody(d))}>
                  撤销修改
                </Button>
                <Button variant="outline" onClick={() => setBody(d.default_body)}>
                  载入默认模板
                </Button>
              </div>
            </TabsContent>
            <TabsContent value="diff">
              <DiffView from={d.default_body} to={body} />
            </TabsContent>
            <TabsContent value="preview" className="grid gap-3">
              <Field label="示例变量" hint={d.variables.length ? `模板引用：${d.variables.join("、")}` : "模板没有引用变量"}>
                <JsonEditor value={vars} rows={6} onChange={(v) => isPlainObject(v) && setVars(v)} />
              </Field>
              <div>
                <Button
                  disabled={preview.isPending}
                  onClick={() => preview.mutate({ path: { key: promptKey }, body: { body, variables: vars } })}
                >
                  渲染编辑器中的模板
                </Button>
              </div>
              {preview.data && <Pre value={preview.data.rendered} className="max-h-[32rem] whitespace-pre-wrap" />}
            </TabsContent>
          </Tabs>
        </CardContent>
      </Card>
      <Card>
        <CardHeader>
          <CardTitle className="flex items-center justify-between">
            版本历史
            <ConfirmButton
              variant="outline"
              title="恢复默认模板？"
              description="历史版本会保留，可以随时再切换回来。"
              onConfirm={() => reset.mutate({ path: { key: promptKey } })}
            >
              恢复默认
            </ConfirmButton>
          </CardTitle>
        </CardHeader>
        <CardContent className="grid gap-2">
          {d.versions.length === 0 && <Empty>还没有保存过版本</Empty>}
          {d.versions.map((v) => (
            <div key={v.id} className="flex items-center justify-between gap-3 rounded-lg border px-3 py-2 text-sm">
              <div className="min-w-0">
                <span className="font-mono">v{v.id}</span>
                {v.id === d.active_version_id && <Badge className="ml-2">生效中</Badge>}
                <span className="ml-3 text-muted-foreground">
                  {fmtTime(v.created_at)} · {v.created_by}
                </span>
                {v.note && <div className="truncate text-muted-foreground">{v.note}</div>}
              </div>
              <div className="flex shrink-0 gap-1.5">
                <Button size="sm" variant="outline" onClick={() => setBody(v.body)}>
                  载入编辑器
                </Button>
                <Button
                  size="sm"
                  disabled={v.id === d.active_version_id || activateM.isPending}
                  onClick={() => activateM.mutate({ path: { key: promptKey }, body: { version_id: v.id } })}
                >
                  设为生效
                </Button>
              </div>
            </div>
          ))}
        </CardContent>
      </Card>
    </div>
  );
}
