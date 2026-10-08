"""Global and per-group settings documents (validated by :class:`yggdrasil.settings.Settings`)."""

import typing
from typing import Any

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, ValidationError

from yggdrasil.admin.auth import Actor
from yggdrasil.admin.common import change, not_found
from yggdrasil.db.models import Group, SettingsDoc
from yggdrasil.db.session import get_session
from yggdrasil.settings import Settings, effective_settings

router = APIRouter(prefix="/settings", tags=["settings"])


class SettingsView(BaseModel):
    value: dict[str, Any]  # the stored (sparse) document
    effective: dict[str, Any]  # merged over defaults (and global, for groups)
    defaults: dict[str, Any]  # what ``value`` is merged over


def _unknown_keys(doc: dict[str, Any], model: type[BaseModel], prefix: str = "") -> list[str]:
    """Keys pydantic would silently ignore (typos); nested models and dict[str, Model] are walked."""
    out: list[str] = []
    for key, value in doc.items():
        field = model.model_fields.get(key)
        path = f"{prefix}{key}"
        if field is None:
            out.append(path)
            continue
        if not isinstance(value, dict):
            continue
        ann = field.annotation
        if isinstance(ann, type) and issubclass(ann, BaseModel):
            out += _unknown_keys(value, ann, f"{path}.")
        elif typing.get_origin(ann) is dict:
            item = typing.get_args(ann)[1]
            if isinstance(item, type) and issubclass(item, BaseModel):
                for k, v in value.items():
                    if isinstance(v, dict):
                        out += _unknown_keys(v, item, f"{path}.{k}.")
    return out


def validate_doc(global_doc: dict[str, Any], group_doc: dict[str, Any] | None) -> Settings:
    """Validate a document by merging it the way the runtime does; 422 on bad types or unknown keys."""
    unknown = _unknown_keys(group_doc if group_doc is not None else global_doc, Settings)
    if unknown:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            [{"loc": ["body", *k.split(".")], "msg": "未知的设置项", "type": "extra_forbidden"} for k in unknown],
        )
    try:
        return effective_settings(global_doc, group_doc)
    except ValidationError as e:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            [{"loc": ["body", *err["loc"]], "msg": err["msg"], "type": err["type"]} for err in e.errors()],
        ) from e


async def global_doc() -> dict[str, Any]:
    async with get_session() as s:
        doc = await s.get(SettingsDoc, "global")
        return dict(doc.value) if doc else {}


@router.get("/schema")
async def get_settings_schema() -> dict[str, Any]:
    return Settings.model_json_schema()


@router.get("/global")
async def get_global_settings() -> SettingsView:
    value = await global_doc()
    return SettingsView(
        value=value,
        effective=effective_settings(value, None).model_dump(mode="json"),
        defaults=Settings().model_dump(mode="json"),
    )


@router.put("/global")
async def put_global_settings(body: dict[str, Any], actor: Actor) -> SettingsView:
    effective = validate_doc(body, None)
    async with change(actor, "settings", "update", "global") as c:
        doc = await c.session.get(SettingsDoc, "global")
        if doc is None:
            c.session.add(SettingsDoc(key="global", value=body))
        else:
            doc.value = body
        c.detail = {"value": body}
    return SettingsView(
        value=body, effective=effective.model_dump(mode="json"), defaults=Settings().model_dump(mode="json")
    )


@router.get("/groups/{group_id}")
async def get_group_settings(group_id: int) -> SettingsView:
    base = await global_doc()
    async with get_session() as s:
        g = await s.get(Group, group_id)
        if g is None:
            raise not_found(f"群 {group_id} ")
        value = dict(g.settings)
    return SettingsView(
        value=value,
        effective=effective_settings(base, value).model_dump(mode="json"),
        defaults=effective_settings(base, None).model_dump(mode="json"),
    )


@router.put("/groups/{group_id}")
async def put_group_settings(group_id: int, body: dict[str, Any], actor: Actor) -> SettingsView:
    base = await global_doc()
    effective = validate_doc(base, body)
    async with change(actor, "group", "settings", group_id) as c:
        g = await c.session.get(Group, group_id)
        if g is None:
            raise not_found(f"群 {group_id} ")
        g.settings = body
        c.detail = {"value": body}
    return SettingsView(
        value=body,
        effective=effective.model_dump(mode="json"),
        defaults=effective_settings(base, None).model_dump(mode="json"),
    )
