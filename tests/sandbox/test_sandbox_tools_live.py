"""``sh`` / ``py`` end to end: the chat tools through a real Session against a sandboxd running the workspace
image's ``/opt/ygg`` helpers (``ygg-sbx-test:dev``), with a fake Kubernetes API and in-process Redis.

Needs the YGG_TEST_SANDBOXD_* variables (see ``helpers.py``); skipped otherwise.
"""

import base64
import io
from collections.abc import AsyncIterator
from typing import Any, cast

import fakeredis
import pytest
from PIL import Image
from pydantic_ai import BinaryContent, RunContext, ToolReturn
from pydantic_ai.models.test import TestModel
from pydantic_ai.usage import RunUsage

from tests.sandbox.helpers import HAVE_SANDBOXD, NO_SANDBOXD, SANDBOXD_TOKEN
from yggdrasil.agent import sandbox_tools
from yggdrasil.agent.deps import Deps, Handles
from yggdrasil.agent.sandbox_tools import py, sh
from yggdrasil.sandbox import Session, WorkspaceManager
from yggdrasil.sandbox.backend import Endpoint
from yggdrasil.sandbox.k8s import KubeClient
from yggdrasil.sandbox.state import keep_key
from yggdrasil.settings import Settings

pytestmark = pytest.mark.skipif(not HAVE_SANDBOXD, reason=NO_SANDBOXD)

GROUP = 10001


@pytest.fixture
def mgr(sandboxd: Endpoint, kube_client: KubeClient, fake_redis: fakeredis.FakeAsyncRedis) -> WorkspaceManager:
    return WorkspaceManager(
        token=SANDBOXD_TOKEN,
        namespace="ns",
        warmpool="pool",
        link_domain="links.example.com",
        kube=kube_client,
        redis=lambda: fake_redis,
        endpoint=lambda _ip: sandboxd,
    )


def _ctx(session: Session) -> RunContext[Deps]:
    settings = Settings.model_validate({"moderation": {"enabled": False}})
    deps = Deps(cast(Any, None), GROUP, 2, settings, Handles(), "chat")
    deps.sandbox = session
    return RunContext(deps=deps, model=TestModel(), usage=RunUsage())


@pytest.fixture
async def session(mgr: WorkspaceManager) -> AsyncIterator[Session]:
    s = mgr.session(GROUP, invocation_id="live", cfg=Settings().tools)
    # A clean kernel: the local sandboxd outlives tests (and earlier runs).
    await s.backend.exec(["/opt/ygg/ygg", "py", "restart"], seconds=30)
    yield s
    found = await mgr.backend_for(GROUP)
    if found is not None:
        await found[0].exec(["/opt/ygg/ygg", "py", "restart"], seconds=30)
        await found[0].aclose()
    await s.close()
    await mgr.destroy(GROUP, reason="test", force=True)


def _png_b64() -> str:
    out = io.BytesIO()
    Image.new("RGB", (16, 16), "red").save(out, "PNG")
    return base64.b64encode(out.getvalue()).decode()


async def test_sh_runs_scripts_and_pages_long_output(session: Session) -> None:
    ctx = _ctx(session)
    text = str(await sh(ctx, "cd /workspace && mkdir -p t && cat > t/a.txt <<'EOF'\nhello\nEOF\ncat t/a.txt; exit 3"))
    assert "[退出码 3，" in text
    assert "hello" in text
    assert "这是新建的空工作区" in text or "[工作区]" not in text
    long = str(await sh(ctx, "seq 1 200000"))
    assert "只显示了开头和结尾" in long
    assert "/workspace/.ygg/sh/" in long
    assert "200000" in long  # the tail is kept
    timed_out = str(await sh(ctx, "echo before; sleep 30", timeout=2))
    assert timed_out.startswith("[超时：运行超过 2 秒")
    assert "before" in timed_out
    # A background child of the script does not hold the call open.
    quick = str(await sh(ctx, "sleep 60 & echo started"))
    assert "started" in quick


async def test_py_kernel_images_effects_and_reap(
    mgr: WorkspaceManager, session: Session, fake_redis: fakeredis.FakeAsyncRedis, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sandbox_tools, "sees_images", lambda _task: True)
    ctx = _ctx(session)
    first = await py(ctx, "x = 21\nprint('hi')\nx * 2")
    assert isinstance(first, str)
    assert "hi" in first
    assert "结果：42" in first
    kernel = await fake_redis.hget(keep_key(GROUP), "kernel")  # pyright: ignore[reportGeneralTypeIssues]
    assert str(kernel).isdigit()
    assert "结果：21" in str(await py(ctx, "x"))

    shown = await py(
        ctx,
        "import base64\n"
        "from IPython.display import Image, display\n"
        f"png = base64.b64decode('{_png_b64()}')\n"
        "display(Image(data=png))\n"
        "open('/workspace/red.png', 'wb').write(png)\n"
        "show('/workspace/red.png', caption='红块')",
    )
    assert isinstance(shown, ToolReturn)
    assert sum(isinstance(c, BinaryContent) for c in cast(list[Any], shown.content)) == 1
    assert "show：已附上图 1（红块）" in str(shown.return_value)
    assert len(ctx.deps.images) == 1

    served = await py(
        ctx,
        "web = start('python3 -m http.server 8000 --bind 0.0.0.0', name='web')\n"
        "wait_port(8000)\n"
        "lo = start('python3 -m http.server 8001 --bind 127.0.0.1', name='lo')\n"
        "import time; time.sleep(1.5)\n"
        "publish(8000)\n"
        "publish(8001)\n"
        "web.pid",
    )
    assert isinstance(served, str)
    assert "https://sbx-" in served, served
    assert "只监听了 127.0.0.1" in served
    info = await mgr.get(GROUP)
    assert info is not None
    [link] = info.links
    assert link.port == 8000
    assert "http.server" in link.command

    timed_out = await py(ctx, "import time\ntime.sleep(30)", timeout=2)
    assert "已中断" in str(timed_out)
    assert "结果：21" in str(await py(ctx, "x"))

    # The end-of-invocation reap spares the kernel and the published server (a child of the kernel).
    await session.close()
    probe = mgr.session(GROUP, invocation_id="probe", cfg=Settings().tools)
    try:
        alive = str(await sh(_ctx(probe), "exec 3<>/dev/tcp/127.0.0.1/8000 && echo up"))
        assert "up" in alive
        assert "结果：21" in str(await py(_ctx(probe), "x"))
        assert await probe.unpublish(8000)
        restarted = await py(_ctx(probe), "x", restart=True)
        assert "已按要求重启" in str(restarted)
        assert "NameError" in str(restarted)
    finally:
        await probe.close()
