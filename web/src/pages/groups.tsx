import { useMutation, useQuery } from "@tanstack/react-query";
import { ChevronsUpDownIcon, DownloadIcon, PlusIcon, SlidersHorizontalIcon, Trash2Icon } from "lucide-react";
import { type FormEvent, useEffect, useState } from "react";
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
import {
  ConfirmButton,
  Empty,
  Field,
  JsonEditor,
  NumberInput,
  PageHeader,
  PendingButton,
  QueryState,
} from "@/components/common";
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
import {
  Command,
  CommandEmpty,
  CommandGroup,
  CommandInput,
  CommandItem,
  CommandList,
} from "@/components/ui/command";
import { Input } from "@/components/ui/input";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { Switch } from "@/components/ui/switch";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { fmtTime, isPlainObject } from "@/lib/format";

export function GroupsPage() {
  const groups = useQuery(listGroupsOptions());
  const upsert = useMutation(upsertGroupMutation());
  const add = useMutation({
    ...upsertGroupMutation(),
    onSuccess: () => {
      toast.success("已加入白名单");
      setNewId(null);
      setNewName("");
    },
  });
  const remove = useMutation({ ...deleteGroupMutation(), onSuccess: () => toast.success("已删除") });
  const backfill = useMutation({ ...backfillGroupMutation(), onSuccess: () => toast.success("已请求回填") });
  const [selected, setSelected] = useState<number | null>(null);
  const [newId, setNewId] = useState<number | null>(null);
  const [newName, setNewName] = useState("");
  const [names, setNames] = useState<Record<number, string>>({});

  const save = (g: GroupOut, patch: Partial<Pick<GroupOut, "name" | "enabled">>) =>
    upsert.mutate({ path: { group_id: g.group_id }, body: { name: g.name, enabled: g.enabled, ...patch } });
  const submitNew = (e: FormEvent) => {
    e.preventDefault();
    if (newId && !add.isPending) add.mutate({ path: { group_id: newId }, body: { name: newName, enabled: true } });
  };

  return (
    <div>
      <PageHeader title="群管理" description="白名单、群名和群级设置覆盖。加入白名单后可以回填历史消息。" />
      <Card className="mb-6">
        <CardContent>
          <form onSubmit={submitNew} className="flex flex-wrap items-end gap-3">
            <Field label="群号" className="w-48">
              <NumberInput value={newId} onChange={setNewId} placeholder="QQ 群号" />
            </Field>
            <Field label="名称" className="w-64">
              <Input value={newName} onChange={(e) => setNewName(e.target.value)} />
            </Field>
            <PendingButton type="submit" pending={add.isPending} disabled={!newId}>
              {!add.isPending && <PlusIcon data-icon="inline-start" />}
              添加并启用
            </PendingButton>
          </form>
        </CardContent>
      </Card>
      <QueryState isLoading={groups.isLoading} error={groups.error} />
      {groups.data?.length === 0 && <Empty description="在上面填群号，把群加入白名单。">还没有群</Empty>}
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
                    aria-label={`群 ${g.group_id} 的名称`}
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
                  <Switch
                    aria-label={`白名单启用 ${g.name || g.group_id}`}
                    checked={g.enabled}
                    onCheckedChange={(enabled) => save(g, { enabled })}
                  />
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
                      aria-pressed={selected === g.group_id}
                      onClick={() => setSelected(selected === g.group_id ? null : g.group_id)}
                    >
                      <SlidersHorizontalIcon data-icon="inline-start" /> 设置覆盖
                    </Button>
                    <PendingButton
                      size="sm"
                      variant="outline"
                      pending={backfill.isPending && backfill.variables?.path.group_id === g.group_id}
                      disabled={backfill.isPending}
                      onClick={() => backfill.mutate({ path: { group_id: g.group_id } })}
                    >
                      {!(backfill.isPending && backfill.variables?.path.group_id === g.group_id) && (
                        <DownloadIcon data-icon="inline-start" />
                      )}
                      回填
                    </PendingButton>
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
  const [picking, setPicking] = useState(false);

  useEffect(() => {
    if (settingsQ.data) setDoc(settingsQ.data.value);
  }, [settingsQ.data]);

  // The API returns pydantic's JSON Schema verbatim.
  const root = schemaQ.data as JsonSchema | undefined;
  if (!root || !settingsQ.data) {
    return <QueryState isLoading={schemaQ.isLoading || settingsQ.isLoading} error={schemaQ.error ?? settingsQ.error} variant="card" />;
  }

  const inherited = settingsQ.data.defaults;
  const present = presentLeaves(doc, root, root).filter((l) => l.path.length);
  const presentKeys = new Set(present.map((l) => pathKey(l.path)));
  const available = leafPaths(root, root).filter((l) => !presentKeys.has(pathKey(l.path)));
  const sections = [...new Set(available.map((l) => l.path[0]))];
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
              <div key={pathKey(path)} className="flex items-start gap-2 rounded-lg border p-3">
                <Field
                  className="flex-1"
                  label={
                    <span>
                      <span className="text-muted-foreground">{SECTION_LABELS[path[0]] ?? path[0]} · </span>
                      <span className="font-mono">{path.slice(1).join(".") || path[0]}</span>
                    </span>
                  }
                  hint={
                    <>
                      {schema.description && <span className="block">{schema.description}</span>}
                      <span className="block">全局值：{JSON.stringify(getAt(inherited, path))}</span>
                    </>
                  }
                >
                  <SchemaInput
                    schema={schema}
                    root={root}
                    value={getAt(doc, path)}
                    onChange={(v) => setDoc(setAt(doc, path, v))}
                  />
                </Field>
                <Button
                  size="icon-sm"
                  variant="ghost"
                  aria-label={`移除覆盖 ${pathKey(path)}`}
                  onClick={() => setDoc(unsetAt(doc, path))}
                >
                  <Trash2Icon />
                </Button>
              </div>
            ))}
            <Popover open={picking} onOpenChange={setPicking}>
              <PopoverTrigger asChild>
                <Button
                  variant="outline"
                  role="combobox"
                  aria-expanded={picking}
                  disabled={!available.length}
                  className="w-fit"
                >
                  <PlusIcon data-icon="inline-start" /> 添加覆盖
                  <ChevronsUpDownIcon data-icon="inline-end" className="text-muted-foreground" />
                </Button>
              </PopoverTrigger>
              <PopoverContent className="w-96 p-0" align="start">
                <Command>
                  <CommandInput aria-label="搜索设置项" placeholder="搜索设置项" />
                  <CommandList>
                    <CommandEmpty>没有匹配的设置项</CommandEmpty>
                    {sections.map((section) => (
                      <CommandGroup key={section} heading={SECTION_LABELS[section] ?? section}>
                        {available
                          .filter((l) => l.path[0] === section)
                          .map((l) => (
                            <CommandItem
                              key={pathKey(l.path)}
                              value={pathKey(l.path)}
                              keywords={[SECTION_LABELS[section] ?? section, l.schema.description ?? ""]}
                              onSelect={() => {
                                setDoc(setAt(doc, l.path, getAt(inherited, l.path)));
                                setPicking(false);
                              }}
                            >
                              <span className="font-mono text-xs">{l.path.slice(1).join(".") || section}</span>
                            </CommandItem>
                          ))}
                      </CommandGroup>
                    ))}
                  </CommandList>
                </Command>
              </PopoverContent>
            </Popover>
          </TabsContent>
          <TabsContent value="json">
            <Field label="覆盖文档（JSON）">
              <JsonEditor value={doc} rows={12} onChange={(v) => isPlainObject(v) && setDoc(v)} />
            </Field>
          </TabsContent>
        </Tabs>
        <div className="mt-4 flex gap-2">
          <PendingButton
            pending={save.isPending}
            disabled={!dirty}
            onClick={() => save.mutate({ path: { group_id: groupId }, body: doc })}
          >
            保存
          </PendingButton>
          <Button variant="outline" disabled={!dirty} onClick={() => setDoc(settingsQ.data.value)}>
            还原
          </Button>
        </div>
      </CardContent>
    </Card>
  );
}
