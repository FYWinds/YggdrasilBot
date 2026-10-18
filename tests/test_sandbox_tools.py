import io
from typing import Any, cast

import pytest
from PIL import Image
from pydantic import ValidationError

from yggdrasil.agent.deps import Deps, Handles
from yggdrasil.agent.sandbox_tools import attach_image, safe_name, suite_enabled, workspace_rel
from yggdrasil.agent.tools import MAX_IMAGES, tool_names
from yggdrasil.moderation.image import ImageVerdict, parse_answer
from yggdrasil.settings import Settings, ToolSettings
from yggdrasil.tools.errors import ToolError


def _png() -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (8, 8), "red").save(out, "PNG")
    return out.getvalue()


def _settings(**moderation: Any) -> Settings:
    return Settings.model_validate({"moderation": moderation})


def _deps(settings: Settings) -> Deps:
    return Deps(cast(Any, None), 1, 2, settings, Handles(), "chat")


def test_idle_must_outlast_invocation_budget():
    ToolSettings(sandbox_idle_min=11, sandbox_invocation_s=600)
    with pytest.raises(ValidationError, match="sandbox_idle_min"):
        ToolSettings(sandbox_idle_min=10, sandbox_invocation_s=600)
    with pytest.raises(ValidationError, match="sandbox_command_max_s"):
        ToolSettings(sandbox_command_timeout_s=200, sandbox_command_max_s=100)


def test_suite_only_for_group_chat_with_every_switch_on():
    on, off = Settings(), Settings.model_validate({"tools": {"sandbox_enabled": False}})
    assert suite_enabled(on, "chat", 1, global_settings=on, configured=True)
    assert not suite_enabled(on, "tldr", 1, global_settings=on, configured=True)
    assert not suite_enabled(on, "chat", None, global_settings=on, configured=True)
    assert not suite_enabled(on, "chat", 1, global_settings=on, configured=False)
    # The kill switch is read from the global settings, a group override cannot turn it back on.
    assert not suite_enabled(on, "chat", 1, global_settings=off, configured=True)
    no_sandbox = Settings.model_validate({"tools": {"enabled": ["web_search"]}})
    assert not suite_enabled(no_sandbox, "chat", 1, global_settings=on, configured=True)


def test_plain_tool_lists_never_carry_the_pseudo_name():
    enabled = Settings().tools.enabled
    assert "sandbox" in enabled
    assert "sandbox" not in tool_names(enabled, "chat")
    assert "sandbox" not in tool_names(enabled, "tldr")


def test_workspace_paths():
    assert workspace_rel("/workspace/out/a.png") == "out/a.png"
    assert workspace_rel("out/a.png") == "out/a.png"
    for bad in ("/etc/passwd", "../x.png", "/workspace/../etc/x", "/workspace"):
        with pytest.raises(ToolError):
            workspace_rel(bad)
    assert safe_name("../../报表 2024.xlsx", "file1") == "报表_2024.xlsx"
    assert safe_name("C:\\tmp\\a b.txt", "file1") == "a_b.txt"
    assert safe_name("...", "file1") == "file1"


def test_image_moderation_answer_fails_closed():
    assert parse_answer("SAFE") == (True, "")
    assert parse_answer("UNSAFE: gore") == (False, "gore")
    assert parse_answer("unsafe") == (False, "unspecified")
    assert parse_answer("这张图没问题") == (False, "error")


async def test_send_image_shares_the_cap_with_render_tools():
    deps = _deps(_settings(enabled=False))
    deps.images = [(b"x", "")] * (MAX_IMAGES - 1)
    result = await attach_image(deps, _png(), "图")
    assert "还能再发 0 张" in result
    with pytest.raises(ToolError, match="已经有"):
        await attach_image(deps, _png(), "图")


async def test_send_image_moderation_follows_group_output_switch():
    calls: list[bytes] = []

    async def unsafe(blob: bytes) -> ImageVerdict:
        calls.append(blob)
        return ImageVerdict(False, "gore", "vision-model", 10, 2)

    unmoderated = _deps(_settings(enabled=True, check_output=False))
    await attach_image(unmoderated, _png(), "", check=unsafe)
    assert not calls
    assert len(unmoderated.images) == 1

    moderated = _deps(_settings(enabled=True, check_output=True))
    with pytest.raises(ToolError, match="gore"):
        await attach_image(moderated, _png(), "", check=unsafe)
    assert len(calls) == 1
    assert not moderated.images
    assert moderated.vision_tokens == [("vision-model", 10, 2)]


async def test_send_image_rejects_non_images():
    with pytest.raises(ToolError, match="不是能发的图片"):
        await attach_image(_deps(_settings(enabled=False)), b"not an image", "")
