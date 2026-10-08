import { useMutation, useQuery } from "@tanstack/react-query";
import { DownloadIcon, PlusIcon, SlidersHorizontalIcon, Trash2Icon } from "lucide-react";
import { useEffect, useState } from "react";
import { toast } from "sonner";
import {
  backfillGroupMutation,
  deleteGroupMutation,
  getGroupSettingsOptions,
  getSettingsSchemaOptions,
  listGroupsOptions,
  putGroupSettingsMutation,
  upsertGroupMutation,
} from "@/client/@tanstack/react-query.gen";
import type { GroupOut } from "@/client/types.gen";
import { ConfirmButton, Empty, Field, JsonEditor, NumberInput, OptionSelect, PageHeader, QueryState } from "@/components/common";
import {
  getAt,
  type JsonSchema,
  leafPaths,
  presentLeaves,
  SchemaInput,
  SECTION_LABELS,
  setAt,
  unsetAt,
} from "@/components/schema-form";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Switch } from "@/components/ui/switch";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { fmtTime, isPlainObject } from "@/lib/format";

export function GroupsPage() {
  const groups = useQuery(listGroupsOptions());
  const upsert = useMutation(upsertGroupMutation());
  const remove = useMutation({ ...deleteGroupMutation(), onSuccess: () => toast.success("已删除") });
  const backfill = useMutation({ ...backfillGroupMutation(), onSuccess: () => toast.success("已请求回填") });
  const [selected, setSelected] = useState<number | null>(null);
  const [newId, setNewId] = useState<number | null>(null);
  const [newName, setNewName] = useState("");
  const [names, setNames] = useState<Record<number, string>>({});

  const save = (g: GroupOut, patch: Partial<Pick<GroupOut, "name" | "enabled">>) =>
    upsert.mutate({ path: { group_id: g.group_id }, body: { name: g.name, enabled: g.enabled, ...patch } });

  return (
    <div>
      <PageHeader title="群管理" description="白名单、群名和群级设置覆盖。加入白名单后可以回填历史消息。" />
      <Card className="mb-6">
        <CardContent className="flex flex-wrap items-end gap-3">
          <Field label="群号" className="w-48">
            <NumberInput value={newId} onChange={setNewId} placeholder="QQ 群号" />
          </Field>
          <Field label="名称" className="w-64">
            <Input value={newName} onChange={(e) => setNewName(e.target.value)} />
          </Field>
          <Button
            disabled={!newId || upsert.isPending}
            onClick={() =>
              newId &&
              upsert.mutate(
                { path: { group_id: newId }, body: { name: newName, enabled: true } },
                {
                  onSuccess: () => {
                    toast.success("已加入白名单");
                    setNewId(null);
                    setNewName("");
                  },
                },
              )
            }
          >
            <PlusIcon /> 添加并启用
          </Button>
        </CardContent>
      </Card>
      <QueryState isLoading={groups.isLoading} error={groups.error} />
      {groups.data?.length === 0 && <Empty>还没有群</Empty>}
      {!!groups.data?.length && (
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>群号</TableHead>
              <TableHead>名称</TableHead>
              <TableHead>白名单</TableHead>
              <TableHead>设置覆盖</TableHead>
              <TableHead>回填</TableHead>
              <TableHead className="text-right">操作</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {groups.data.map((g) => (
              <TableRow key={g.group_id} data-state={selected === g.group_id ? "selected" : undefined}>
                <TableCell className="font-mono">{g.group_id}</TableCell>
                <TableCell>
                  <Input
                    className="h-8 w-56"
                    value={names[g.group_id] ?? g.name}
                    onChange={(e) => setNames({ ...names, [g.group_id]: e.target.value })}
                    onBlur={() => {
                      const name = names[g.group_id];
                      if (name !== undefined && name !== g.name) save(g, { name });
                    }}
                  />
                </TableCell>
                <TableCell>
                  <Switch checked={g.enabled} onCheckedChange={(enabled) => save(g, { enabled })} />
                </TableCell>
                <TableCell>
                  {Object.keys(g.settings).length ? (
                    <Badge variant="secondary">{Object.keys(g.settings).join("、")}</Badge>
                  ) : (
                    <span className="text-xs text-muted-foreground">无</span>
                  )}
                </TableCell>
                <TableCell className="text-xs">{fmtTime(g.backfilled_at)}</TableCell>
                <TableCell>
                  <div className="flex justify-end gap-1.5">
                    <Button
                      size="sm"
                      variant={selected === g.group_id ? "default" : "outline"}
                      onClick={() => setSelected(selected === g.group_id ? null : g.group_id)}
                    >
                      <SlidersHorizontalIcon /> 设置覆盖
                    </Button>
                    <Button
                      size="sm"
                      variant="outline"
                      disabled={backfill.isPending}
                      onClick={() => backfill.mutate({ path: { group_id: g.group_id } })}
                    >
                      <DownloadIcon /> 回填
                    </Button>
                    <ConfirmButton
                      title={`删除群 ${g.group_id}？`}
                      description="只删除配置（白名单和设置覆盖），不删除历史消息和记忆。"
                      onConfirm={() => remove.mutate({ path: { group_id: g.group_id } })}
                    >
                      删除
                    </ConfirmButton>
                  </div>
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      )}
      {selected !== null && <GroupOverrideEditor key={selected} groupId={selected} />}
    </div>
  );
}

const pathKey = (path: string[]) => path.join(".");

/** Edits only the keys a group overrides; everything else inherits from the global settings. */
function GroupOverrideEditor({ groupId }: { groupId: number }) {
  const schemaQ = useQuery(getSettingsSchemaOptions());
  const settingsQ = useQuery(getGroupSettingsOptions({ path: { group_id: groupId } }));
  const save = useMutation({ ...putGroupSettingsMutation(), onSuccess: () => toast.success("已保存群设置覆盖") });
  const [doc, setDoc] = useState<Record<string, unknown>>({});
  const [adding, setAdding] = useState<string | null>(null);

  useEffect(() => {
    if (settingsQ.data) setDoc(settingsQ.data.value);
  }, [settingsQ.data]);

  // The API returns pydantic's JSON Schema verbatim.
  const root = schemaQ.data as JsonSchema | undefined;
  if (!root || !settingsQ.data) return <QueryState isLoading error={schemaQ.error ?? settingsQ.error} />;

  const inherited = settingsQ.data.defaults;
  const present = presentLeaves(doc, root, root).filter((l) => l.path.length);
  const presentKeys = new Set(present.map((l) => pathKey(l.path)));
  const available = leafPaths(root, root).filter((l) => !presentKeys.has(pathKey(l.path)));
  const dirty = JSON.stringify(doc) !== JSON.stringify(settingsQ.data.value);

  return (
    <Card className="mt-6">
      <CardHeader>
        <CardTitle>群 {groupId} 的设置覆盖</CardTitle>
        <CardDescription>只列出被覆盖的项；未覆盖的项继承全局设置。</CardDescription>
      </CardHeader>
      <CardContent>
        <Tabs defaultValue="form">
          <TabsList>
            <TabsTrigger value="form">表单</TabsTrigger>
            <TabsTrigger value="json">JSON</TabsTrigger>
          </TabsList>
          <TabsContent value="form" className="grid gap-4">
            {present.length === 0 && <Empty>没有覆盖任何设置</Empty>}
            {present.map(({ path, schema }) => (
              <div key={pathKey(path)} className="grid gap-1.5 rounded-lg border p-3">
                <div className="flex items-center justify-between">
                  <div className="text-sm">
                    <span className="text-muted-foreground">{SECTION_LABELS[path[0]] ?? path[0]} · </span>
                    <span className="font-mono">{path.slice(1).join(".") || path[0]}</span>
                  </div>
                  <Button size="icon-sm" variant="ghost" aria-label="移除覆盖" onClick={() => setDoc(unsetAt(doc, path))}>
                    <Trash2Icon />
                  </Button>
                </div>
                {schema.description && <p className="text-xs text-muted-foreground">{schema.description}</p>}
                <SchemaInput
                  schema={schema}
                  root={root}
                  value={getAt(doc, path)}
                  onChange={(v) => setDoc(setAt(doc, path, v))}
                />
                <p className="text-xs text-muted-foreground">全局值：{JSON.stringify(getAt(inherited, path))}</p>
              </div>
            ))}
            <div className="flex items-end gap-2">
              <Field label="添加覆盖" className="w-96">
                <OptionSelect
                  value={adding}
                  onChange={setAdding}
                  placeholder="选择设置项"
                  options={available.map((l) => ({
                    value: pathKey(l.path),
                    label: `${SECTION_LABELS[l.path[0]] ?? l.path[0]} · ${l.path.slice(1).join(".")}`,
                  }))}
                />
              </Field>
              <Button
                variant="outline"
                disabled={!adding}
                onClick={() => {
                  if (!adding) return;
                  const path = adding.split(".");
                  setDoc(setAt(doc, path, getAt(inherited, path)));
                  setAdding(null);
                }}
              >
                <PlusIcon /> 添加
              </Button>
            </div>
          </TabsContent>
          <TabsContent value="json">
            <JsonEditor value={doc} rows={12} onChange={(v) => isPlainObject(v) && setDoc(v)} />
          </TabsContent>
        </Tabs>
        <div className="mt-4 flex gap-2">
          <Button
            disabled={!dirty || save.isPending}
            onClick={() => save.mutate({ path: { group_id: groupId }, body: doc })}
          >
            保存
          </Button>
          <Button variant="outline" disabled={!dirty} onClick={() => setDoc(settingsQ.data.value)}>
            还原
          </Button>
        </div>
      </CardContent>
    </Card>
  );
}
