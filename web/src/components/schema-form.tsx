import type { ReactNode } from "react";
import { PlusIcon, Trash2Icon } from "lucide-react";
import { useState } from "react";
import { Field, JsonEditor, MultiToggle, NumberInput, OptionSelect, TagInput } from "@/components/common";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Switch } from "@/components/ui/switch";
import { isPlainObject } from "@/lib/format";

/** The subset of JSON Schema that pydantic emits for yggdrasil.settings.Settings. */
export type JsonSchema = {
  type?: string | string[];
  title?: string;
  description?: string;
  default?: unknown;
  properties?: Record<string, JsonSchema>;
  additionalProperties?: JsonSchema | boolean;
  items?: JsonSchema;
  enum?: unknown[];
  const?: unknown;
  $ref?: string;
  $defs?: Record<string, JsonSchema>;
  anyOf?: JsonSchema[];
  allOf?: JsonSchema[];
  minimum?: number;
  maximum?: number;
};

export const SECTION_LABELS: Record<string, string> = {
  persona: "人设",
  interaction: "交互",
  moderation: "审核",
  ratelimit: "限流与预算",
  context: "上下文",
  tools: "工具",
  memory: "记忆整理",
  privacy: "隐私",
};

/** Inline $ref / single allOf, and unwrap Optional (anyOf with null). */
export function resolveSchema(schema: JsonSchema, root: JsonSchema): JsonSchema {
  let s = schema;
  for (let i = 0; i < 8; i++) {
    if (s.$ref) {
      const { $ref, ...rest } = s;
      const target = root.$defs?.[$ref.replace("#/$defs/", "")] ?? {};
      s = { ...target, ...rest };
    } else if (s.allOf?.length === 1) {
      const { allOf, ...rest } = s;
      s = { ...allOf[0], ...rest };
    } else if (s.anyOf) {
      const nonNull = s.anyOf.filter((v) => v.type !== "null");
      if (nonNull.length !== 1) return s;
      const { anyOf: _, ...rest } = s;
      s = { ...nonNull[0], ...rest };
    } else {
      return s;
    }
  }
  return s;
}

export function getAt(obj: unknown, path: string[]): unknown {
  let cur = obj;
  for (const key of path) {
    if (!isPlainObject(cur)) return undefined;
    cur = cur[key];
  }
  return cur;
}

export function setAt(obj: Record<string, unknown>, path: string[], value: unknown): Record<string, unknown> {
  const [head, ...rest] = path;
  if (rest.length === 0) return { ...obj, [head]: value };
  const child = isPlainObject(obj[head]) ? (obj[head] as Record<string, unknown>) : {};
  return { ...obj, [head]: setAt(child, rest, value) };
}

/** Remove a leaf and prune objects left empty. */
export function unsetAt(obj: Record<string, unknown>, path: string[]): Record<string, unknown> {
  const [head, ...rest] = path;
  const out = { ...obj };
  if (rest.length === 0 || !isPlainObject(out[head])) {
    delete out[head];
    return out;
  }
  const child = unsetAt(out[head] as Record<string, unknown>, rest);
  if (Object.keys(child).length) out[head] = child;
  else delete out[head];
  return out;
}

/** Fields under `value` that differ from `base` (nested objects diffed recursively), or undefined if equal. */
export function sparseDiff(value: unknown, base: unknown): unknown {
  if (isPlainObject(value) && isPlainObject(base)) {
    const out: Record<string, unknown> = {};
    for (const [k, v] of Object.entries(value)) {
      const d = sparseDiff(v, base[k]);
      if (d !== undefined) out[k] = d;
    }
    return Object.keys(out).length ? out : undefined;
  }
  return JSON.stringify(value) === JSON.stringify(base) ? undefined : value;
}

export type Leaf = { path: string[]; schema: JsonSchema };

/** Every editable leaf: anything that is not an object with fixed properties. */
export function leafPaths(schema: JsonSchema, root: JsonSchema, prefix: string[] = []): Leaf[] {
  const s = resolveSchema(schema, root);
  if (!s.properties) return [{ path: prefix, schema: s }];
  return Object.entries(s.properties).flatMap(([k, child]) => leafPaths(child, root, [...prefix, k]));
}

/** Leaf paths present in a sparse document, following the schema's object structure. */
export function presentLeaves(doc: unknown, schema: JsonSchema, root: JsonSchema, prefix: string[] = []): Leaf[] {
  const s = resolveSchema(schema, root);
  if (!s.properties || !isPlainObject(doc)) return [{ path: prefix, schema: s }];
  return Object.keys(doc).flatMap((k) =>
    s.properties![k] ? presentLeaves(doc[k], s.properties![k], root, [...prefix, k]) : [{ path: [...prefix, k], schema: {} }],
  );
}

function typeOf(s: JsonSchema): string | undefined {
  return Array.isArray(s.type) ? s.type.find((t) => t !== "null") : s.type;
}

export function SchemaInput({
  schema,
  root,
  value,
  onChange,
}: {
  schema: JsonSchema;
  root: JsonSchema;
  value: unknown;
  onChange: (v: unknown) => void;
}) {
  const s = resolveSchema(schema, root);
  const type = typeOf(s);
  if (s.enum) {
    return (
      <OptionSelect
        value={value == null ? null : String(value)}
        onChange={(v) => onChange(v)}
        options={s.enum.map((e) => ({ value: String(e), label: String(e) }))}
      />
    );
  }
  if (type === "boolean") {
    return <Switch checked={Boolean(value)} onCheckedChange={onChange} />;
  }
  if (type === "integer" || type === "number") {
    return (
      <NumberInput
        className="max-w-48"
        step={type === "integer" ? 1 : "any"}
        value={typeof value === "number" ? value : null}
        onChange={(v) => v !== null && onChange(v)}
      />
    );
  }
  if (type === "string") {
    return <Input value={typeof value === "string" ? value : ""} onChange={(e) => onChange(e.target.value)} />;
  }
  if (type === "array" && s.items) {
    const items = resolveSchema(s.items, root);
    const list = Array.isArray(value) ? value.map(String) : [];
    if (items.enum) return <MultiToggle options={items.enum.map(String)} value={list} onChange={onChange} />;
    if (typeOf(items) === "string") return <TagInput value={list} onChange={onChange} />;
  }
  if (type === "object" && typeof s.additionalProperties === "object") {
    return <MapEditor schema={s.additionalProperties} root={root} value={value} onChange={onChange} />;
  }
  return <JsonEditor value={value} onChange={onChange} rows={4} />;
}

function MapEditor({
  schema,
  root,
  value,
  onChange,
}: {
  schema: JsonSchema;
  root: JsonSchema;
  value: unknown;
  onChange: (v: unknown) => void;
}) {
  const map = isPlainObject(value) ? value : {};
  const [newKey, setNewKey] = useState("");
  const itemSchema = resolveSchema(schema, root);
  const add = () => {
    const k = newKey.trim();
    if (!k || k in map) return;
    onChange({ ...map, [k]: defaultsOf(itemSchema, root) });
    setNewKey("");
  };
  return (
    <div className="grid gap-3">
      {Object.entries(map).map(([k, v]) => (
        <div key={k} className="rounded-lg border p-3">
          <div className="mb-2 flex items-center justify-between">
            <span className="font-mono text-sm">{k}</span>
            <Button
              size="icon-sm"
              variant="ghost"
              aria-label={`删除 ${k}`}
              onClick={() => {
                const { [k]: _, ...rest } = map;
                onChange(rest);
              }}
            >
              <Trash2Icon />
            </Button>
          </div>
          <SchemaObject schema={itemSchema} root={root} value={v} onChange={(nv) => onChange({ ...map, [k]: nv })} />
        </div>
      ))}
      <div className="flex gap-2">
        <Input placeholder="新键名" value={newKey} onChange={(e) => setNewKey(e.target.value)} className="max-w-72" />
        <Button size="sm" variant="outline" onClick={add}>
          <PlusIcon /> 添加
        </Button>
      </div>
    </div>
  );
}

function defaultsOf(schema: JsonSchema, root: JsonSchema): unknown {
  const s = resolveSchema(schema, root);
  if (s.default !== undefined) return s.default;
  if (s.properties) return Object.fromEntries(Object.entries(s.properties).map(([k, v]) => [k, defaultsOf(v, root)]));
  return null;
}

export function FieldLabel({ name, schema }: { name: string; schema: JsonSchema }): ReactNode {
  return (
    <span className="flex items-baseline gap-2">
      <span className="font-mono text-xs">{name}</span>
      {schema.title && <span className="text-xs font-normal text-muted-foreground">{schema.title}</span>}
    </span>
  );
}

/** Renders an object schema: nested objects become sections, everything else a labelled input. */
export function SchemaObject({
  schema,
  root,
  value,
  onChange,
}: {
  schema: JsonSchema;
  root: JsonSchema;
  value: unknown;
  onChange: (v: unknown) => void;
}) {
  const s = resolveSchema(schema, root);
  const obj = isPlainObject(value) ? value : {};
  if (!s.properties) return <SchemaInput schema={s} root={root} value={value} onChange={onChange} />;
  return (
    <div className="grid gap-4">
      {Object.entries(s.properties).map(([key, child]) => {
        const cs = resolveSchema(child, root);
        if (cs.properties) {
          return (
            <fieldset key={key} className="grid gap-3 rounded-lg border p-3">
              <legend className="px-1">
                <FieldLabel name={key} schema={cs} />
              </legend>
              {cs.description && <p className="text-xs text-muted-foreground">{cs.description}</p>}
              <SchemaObject
                schema={cs}
                root={root}
                value={obj[key]}
                onChange={(nv) => onChange({ ...obj, [key]: nv })}
              />
            </fieldset>
          );
        }
        return (
          <Field key={key} label={<FieldLabel name={key} schema={cs} />} hint={cs.description}>
            <SchemaInput schema={cs} root={root} value={obj[key]} onChange={(nv) => onChange({ ...obj, [key]: nv })} />
          </Field>
        );
      })}
    </div>
  );
}
