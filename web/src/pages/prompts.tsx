import { useMutation, useQuery } from "@tanstack/react-query";
import { diffLines } from "diff";
import { type FormEvent, type ReactNode, useEffect, useState } from "react";
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
import {
  ConfirmButton,
  Disclosure,
  Empty,
  Field,
  Hint,
  JsonEditor,
  PageHeader,
  PendingButton,
  QueryState,
} from "@/components/common";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Checkbox } from "@/components/ui/checkbox";
import {
  CodeBlock,
  CodeBlockActions,
  CodeBlockContent,
  CodeBlockCopyButton,
  CodeBlockHeader,
  CodeBlockLine,
  CodeBlockTitle,
} from "@/components/ui/code-block";
import { Input } from "@/components/ui/input";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Textarea } from "@/components/ui/textarea";
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group";
import { fmtTime, isPlainObject } from "@/lib/format";

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
          <ToggleGroup
            type="single"
            orientation="vertical"
            spacing={0.5}
            aria-label="模板"
            className="w-56 shrink-0"
            value={current ?? ""}
            onValueChange={(v) => v && setKey(v)}
          >
            {list.data.map((p) => (
              <ToggleGroupItem key={p.key} value={p.key} className="justify-between font-normal data-[state=on]:font-medium">
                <span className="truncate font-mono">{p.key}</span>
                {p.active_version_id ? <Badge>v{p.active_version_id}</Badge> : <Badge variant="outline">默认</Badge>}
              </ToggleGroupItem>
            ))}
          </ToggleGroup>
          <div className="min-w-0 flex-1">{current && <PromptEditor key={current} promptKey={current} />}</div>
        </div>
      )}
    </div>
  );
}

function activeBody(d: PromptDetail): string {
  return d.versions.find((v) => v.id === d.active_version_id)?.body ?? d.default_body;
}

/** A template or rendered prompt: line numbers, wrapping, copy button. */
function TemplateBlock({ title, value }: { title: ReactNode; value: string }) {
  return (
    <CodeBlock>
      <CodeBlockHeader>
        <CodeBlockTitle>{title}</CodeBlockTitle>
        <CodeBlockActions>
          <CodeBlockCopyButton value={value} aria-label="复制" />
        </CodeBlockActions>
      </CodeBlockHeader>
      <CodeBlockContent wrap lineNumbers className="max-h-[32rem]">
        {value}
      </CodeBlockContent>
    </CodeBlock>
  );
}

function DiffView({ from, to }: { from: string; to: string }) {
  const parts = diffLines(from, to);
  if (parts.every((p) => !p.added && !p.removed)) return <Empty>与默认模板相同</Empty>;
  const rows = parts.flatMap((p) =>
    p.value
      .replace(/\n$/, "")
      .split("\n")
      .map((line) => ({ line, variant: p.added ? ("added" as const) : p.removed ? ("removed" as const) : ("default" as const) })),
  );
  const added = rows.filter((r) => r.variant === "added").length;
  const removed = rows.filter((r) => r.variant === "removed").length;
  return (
    <CodeBlock>
      <CodeBlockHeader>
        <CodeBlockTitle>默认模板 → 编辑器</CodeBlockTitle>
        <span className="ms-auto font-mono text-xs">
          <span className="text-success">+{added}</span> <span className="text-destructive">−{removed}</span>
        </span>
      </CodeBlockHeader>
      <CodeBlockContent wrap className="max-h-[32rem]">
        {rows.map((r, i) => (
          <CodeBlockLine
            key={i}
            variant={r.variant}
            className={r.variant === "added" ? "bg-success/10 after:text-success" : undefined}
          >
            {r.line}
          </CodeBlockLine>
        ))}
      </CodeBlockContent>
    </CodeBlock>
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

  if (!detail.data) return <QueryState isLoading={detail.isLoading} error={detail.error} variant="card" />;
  const d = detail.data;
  const changed = body !== activeBody(d);
  const canSave = changed && !!body.trim() && !create.isPending;
  const save = (e: FormEvent) => {
    e.preventDefault();
    if (canSave) create.mutate({ path: { key: promptKey }, body: { body, note, activate } });
  };

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
            <TabsContent value="edit" asChild>
              <form onSubmit={save} className="grid gap-3">
                <Textarea
                  aria-label="模板内容"
                  className="min-h-96 font-mono text-xs"
                  spellCheck={false}
                  value={body}
                  onChange={(e) => setBody(e.target.value)}
                />
                <div className="flex flex-wrap items-center gap-3">
                  <Input
                    aria-label="版本备注"
                    className="w-80"
                    placeholder="版本备注"
                    value={note}
                    onChange={(e) => setNote(e.target.value)}
                  />
                  <Field label="保存后立即生效" orientation="horizontal" className="w-auto">
                    <Checkbox checked={activate} onCheckedChange={(c) => setActivate(c === true)} />
                  </Field>
                  <PendingButton type="submit" pending={create.isPending} disabled={!changed || !body.trim()}>
                    保存为新版本
                  </PendingButton>
                  <Button type="button" variant="outline" disabled={!changed} onClick={() => setBody(activeBody(d))}>
                    撤销修改
                  </Button>
                  <Button type="button" variant="outline" onClick={() => setBody(d.default_body)}>
                    载入默认模板
                  </Button>
                </div>
              </form>
            </TabsContent>
            <TabsContent value="diff">
              <DiffView from={d.default_body} to={body} />
            </TabsContent>
            <TabsContent value="preview" className="grid gap-3">
              <Field label="示例变量" hint={d.variables.length ? `模板引用：${d.variables.join("、")}` : "模板没有引用变量"}>
                <JsonEditor value={vars} rows={6} onChange={(v) => isPlainObject(v) && setVars(v)} />
              </Field>
              <div>
                <PendingButton
                  pending={preview.isPending}
                  onClick={() => preview.mutate({ path: { key: promptKey }, body: { body, variables: vars } })}
                >
                  渲染编辑器中的模板
                </PendingButton>
              </div>
              {preview.data && <TemplateBlock title="渲染结果" value={preview.data.rendered} />}
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
            <div key={v.id} className="grid gap-1 rounded-lg border px-3 py-2 text-sm">
              <div className="flex items-center justify-between gap-3">
                <div className="min-w-0">
                  <span className="font-mono">v{v.id}</span>
                  {v.id === d.active_version_id && <Badge className="ml-2">生效中</Badge>}
                  <span className="ml-3 text-muted-foreground">
                    {fmtTime(v.created_at)} · {v.created_by}
                  </span>
                  {v.note && (
                    <Hint content={v.note}>
                      <div tabIndex={0} className="truncate rounded-sm text-muted-foreground outline-none focus-visible:ring-3 focus-visible:ring-ring/50">
                        {v.note}
                      </div>
                    </Hint>
                  )}
                </div>
                <div className="flex shrink-0 gap-1.5">
                  <Button size="sm" variant="outline" onClick={() => setBody(v.body)}>
                    载入编辑器
                  </Button>
                  <PendingButton
                    size="sm"
                    pending={activateM.isPending && activateM.variables?.body.version_id === v.id}
                    disabled={v.id === d.active_version_id || activateM.isPending}
                    onClick={() => activateM.mutate({ path: { key: promptKey }, body: { version_id: v.id } })}
                  >
                    设为生效
                  </PendingButton>
                </div>
              </div>
              <Disclosure label="查看内容">
                <TemplateBlock title={`v${v.id}`} value={v.body} />
              </Disclosure>
            </div>
          ))}
        </CardContent>
      </Card>
    </div>
  );
}
