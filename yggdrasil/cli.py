"""Bootstrap / ops CLI: ``python -m yggdrasil.cli <command>``.

Everything here can also be done from the dashboard; the CLI exists for first setup
and scripted environments. API keys are read from an environment variable, never argv.
"""

import argparse
import asyncio
import json
import os
import sys

import httpx
from sqlalchemy import select

from yggdrasil.config import APP_CONFIG
from yggdrasil.crypto import decrypt, encrypt, mask
from yggdrasil.db.models import Admin, ConfigAudit, Group, Provider, TaskRoute
from yggdrasil.db.session import dispose_engine, get_session
from yggdrasil.llm.registry import TASKS


def _out(*parts: object) -> None:
    sys.stdout.write(" ".join(str(p) for p in parts) + "\n")


async def _audit(session, entity: str, entity_id: str, action: str, detail: dict) -> None:
    session.add(ConfigAudit(actor="cli", entity=entity, entity_id=entity_id, action=action, detail=detail))


async def _publish(reason: str) -> None:
    from yggdrasil.runtime import publish_change

    try:
        await publish_change(reason)
    except Exception as e:
        _out("warning: could not publish config change:", e)


async def provider_add(args: argparse.Namespace) -> None:
    key = os.environ.get(args.key_env) if args.key_env else None
    if args.key_env and not key:
        raise SystemExit(f"env var {args.key_env} is empty")
    async with get_session() as session:
        p = await session.scalar(select(Provider).where(Provider.name == args.name))
        if p is None:
            p = Provider(name=args.name, base_url=args.base_url)
            session.add(p)
        p.base_url = args.base_url.rstrip("/")
        p.api_format = args.format
        if key:
            p.api_key_enc = encrypt(key)
        await session.flush()
        await _audit(
            session, "provider", str(p.id), "upsert", {"name": p.name, "base_url": p.base_url, "format": p.api_format}
        )
        _out("provider", p.id, p.name, p.base_url, "key", mask(key) if key else "(unchanged)")
    await _publish(f"provider {args.name}")


async def provider_refresh(args: argparse.Namespace) -> None:
    async with get_session() as session:
        p = await session.scalar(select(Provider).where(Provider.name == args.name))
        if p is None:
            raise SystemExit(f"no provider {args.name}")
        headers = {"User-Agent": APP_CONFIG.llm_user_agent}
        if p.api_key_enc:
            headers["Authorization"] = f"Bearer {decrypt(p.api_key_enc)}"
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(f"{p.base_url}/models", headers=headers)
            resp.raise_for_status()
        p.models = sorted(m["id"] for m in resp.json().get("data", []))
        from datetime import UTC, datetime

        p.models_fetched_at = datetime.now(UTC)
        _out(p.name, json.dumps(p.models, ensure_ascii=False))
    await _publish(f"provider {args.name} models")


async def provider_list(_: argparse.Namespace) -> None:
    async with get_session() as session:
        for p in (await session.scalars(select(Provider).order_by(Provider.id))).all():
            _out(p.id, p.name, p.base_url, "enabled" if p.enabled else "disabled", len(p.models or []), "models")


async def route_set(args: argparse.Namespace) -> None:
    if args.task not in TASKS:
        raise SystemExit(f"unknown task {args.task}; one of {', '.join(TASKS)}")
    params = {}
    for kv in args.param or []:
        k, _, v = kv.partition("=")
        try:
            params[k] = json.loads(v)
        except ValueError:
            params[k] = v
    async with get_session() as session:
        p = await session.scalar(select(Provider).where(Provider.name == args.provider))
        if p is None:
            raise SystemExit(f"no provider {args.provider}")
        r = await session.get(TaskRoute, args.task)
        if r is None:
            r = TaskRoute(task=args.task, provider_id=p.id, model=args.model)
            session.add(r)
        r.provider_id, r.model, r.params = p.id, args.model, params
        if args.fallback:
            fp_name, _, fmodel = args.fallback.partition(":")
            fp = await session.scalar(select(Provider).where(Provider.name == fp_name))
            if fp is None or not fmodel:
                raise SystemExit("--fallback must be provider:model")
            r.fallback_provider_id, r.fallback_model = fp.id, fmodel
        await _audit(session, "task_route", args.task, "upsert", {"provider": p.name, "model": args.model})
        _out("route", args.task, "->", p.name, args.model, params or "")
    await _publish(f"route {args.task}")


async def group_enable(args: argparse.Namespace) -> None:
    async with get_session() as session:
        g = await session.get(Group, args.group_id)
        if g is None:
            g = Group(group_id=args.group_id)
            session.add(g)
        g.enabled = not args.disable
        if args.name:
            g.name = args.name
        await _audit(session, "group", str(args.group_id), "enable" if g.enabled else "disable", {})
        _out("group", args.group_id, "enabled" if g.enabled else "disabled")
    await _publish(f"group {args.group_id}")


async def admin_add(args: argparse.Namespace) -> None:
    async with get_session() as session:
        if await session.get(Admin, (args.kind, args.value)) is None:
            session.add(Admin(kind=args.kind, value=args.value))
        await _audit(session, "admin", f"{args.kind}:{args.value}", "add", {})
    await _publish("admins")


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m yggdrasil.cli")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("provider-add", help="create/update an OpenAI-compatible provider")
    p.add_argument("name")
    p.add_argument("base_url", help="including /v1")
    p.add_argument("--key-env", help="environment variable holding the API key")
    p.add_argument("--format", choices=["openai", "anthropic"], default="openai", help="chat wire format")
    p.set_defaults(fn=provider_add)

    p = sub.add_parser("provider-refresh", help="fetch /models for a provider")
    p.add_argument("name")
    p.set_defaults(fn=provider_refresh)

    sub.add_parser("provider-list").set_defaults(fn=provider_list)

    p = sub.add_parser("route-set", help="route a task to provider/model")
    p.add_argument("task")
    p.add_argument("provider")
    p.add_argument("model")
    p.add_argument("--param", action="append", help="k=v (JSON values allowed), e.g. temperature=0.3")
    p.add_argument("--fallback", help="provider:model")
    p.set_defaults(fn=route_set)

    p = sub.add_parser("group", help="whitelist a group")
    p.add_argument("group_id", type=int)
    p.add_argument("--name", default="")
    p.add_argument("--disable", action="store_true")
    p.set_defaults(fn=group_enable)

    p = sub.add_parser("admin-add")
    p.add_argument("kind", choices=["qq", "email"])
    p.add_argument("value")
    p.set_defaults(fn=admin_add)

    args = parser.parse_args()

    async def runner() -> None:
        try:
            await args.fn(args)
        finally:
            await dispose_engine()

    asyncio.run(runner())


if __name__ == "__main__":
    main()
