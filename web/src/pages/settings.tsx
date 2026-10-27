import { useMutation, useQuery } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { toast } from "sonner";
import {
  getGlobalSettingsOptions,
  getSettingsSchemaOptions,
  putGlobalSettingsMutation,
} from "@/client/@tanstack/react-query.gen";
import { Disclosure, Field, JsonEditor, PageHeader, PendingButton, Pre, QueryState } from "@/components/common";
import { type JsonSchema, resolveSchema, SECTION_LABELS, SchemaObject, sparseDiff } from "@/components/schema-form";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { isPlainObject } from "@/lib/format";

export function SettingsPage() {
  const schemaQ = useQuery(getSettingsSchemaOptions());
  const globalQ = useQuery(getGlobalSettingsOptions());
  const save = useMutation({ ...putGlobalSettingsMutation(), onSuccess: () => toast.success("已保存，正在通知各进程热更新") });
  const [draft, setDraft] = useState<Record<string, unknown>>({});
  const [raw, setRaw] = useState<unknown>({});
  const [rawValid, setRawValid] = useState(true);

  useEffect(() => {
    if (globalQ.data) {
      setDraft(globalQ.data.effective);
      setRaw(globalQ.data.value);
    }
  }, [globalQ.data]);

  const root = schemaQ.data as JsonSchema | undefined;
  if (!root || !globalQ.data) {
    return (
      <QueryState
        isLoading={schemaQ.isLoading || globalQ.isLoading}
        error={schemaQ.error ?? globalQ.error}
        variant="card"
      />
    );
  }

  const sparse = sparseDiff(draft, globalQ.data.defaults);
  const doc = isPlainObject(sparse) ? sparse : {};
  const dirty = JSON.stringify(doc) !== JSON.stringify(globalQ.data.value);

  return (
    <div className="max-w-4xl">
      <PageHeader
        title="设置"
        description="全局设置；群级覆盖在「群管理」里编辑。只保存与默认值不同的项。"
        actions={
          <PendingButton pending={save.isPending} disabled={!dirty} onClick={() => save.mutate({ body: doc })}>
            {dirty ? "保存" : "已保存"}
          </PendingButton>
        }
      />
      <Tabs defaultValue="form">
        <TabsList>
          <TabsTrigger value="form">表单</TabsTrigger>
          <TabsTrigger value="json">JSON</TabsTrigger>
        </TabsList>
        <TabsContent value="form" className="grid gap-4">
          {Object.entries(resolveSchema(root, root).properties ?? {}).map(([key, child]) => (
            <Card key={key}>
              <CardHeader>
                <CardTitle>{SECTION_LABELS[key] ?? key}</CardTitle>
              </CardHeader>
              <CardContent>
                <SchemaObject
                  schema={child}
                  root={root}
                  value={draft[key]}
                  onChange={(v) => setDraft((d) => ({ ...d, [key]: v }))}
                />
              </CardContent>
            </Card>
          ))}
        </TabsContent>
        <TabsContent value="json">
          <Card>
            <CardHeader>
              <CardTitle>存储的文档</CardTitle>
              <CardDescription>直接编辑覆盖默认值的稀疏 JSON；保存时会按 Settings 校验。</CardDescription>
            </CardHeader>
            <CardContent className="grid gap-3">
              <Field label="覆盖默认值的 JSON">
                <JsonEditor value={raw} onChange={setRaw} rows={20} onValidityChange={setRawValid} />
              </Field>
              <div className="flex gap-2">
                <PendingButton
                  pending={save.isPending}
                  disabled={!rawValid || !isPlainObject(raw)}
                  onClick={() => isPlainObject(raw) && save.mutate({ body: raw })}
                >
                  保存 JSON
                </PendingButton>
                <Button variant="outline" onClick={() => setRaw(globalQ.data.value)}>
                  还原
                </Button>
              </div>
              <Disclosure label="生效值（合并默认值后）">
                <Pre value={globalQ.data.effective} viewport="max-h-96" />
              </Disclosure>
            </CardContent>
          </Card>
        </TabsContent>
      </Tabs>
    </div>
  );
}
