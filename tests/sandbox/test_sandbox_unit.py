"""Pure sandbox rules: argv building, path mapping, listings, limits, renewal math, links, workspace names, record
encoding and the model-facing workspace list."""

import json
from dataclasses import replace

import pytest
from pydantic_ai.workspaces import FileEntry

from yggdrasil.sandbox.backend import (
    CommandLimits,
    Endpoint,
    _Budget,
    is_protected,
    parse_listing,
    rest_path,
    shell_argv,
)
from yggdrasil.sandbox.errors import SandboxError, SandboxToolError
from yggdrasil.sandbox.manager import _claim_ready, format_tree, format_workspaces, parse_listener, reap_argv
from yggdrasil.sandbox.state import (
    Link,
    LinkRecord,
    Mapping,
    claim_name,
    clean_description,
    link_id,
    name_error,
    parse_ws_key,
    shutdown_at,
    valid_name,
    validate_port,
    ws_key,
)

WRAP = ["/bin/sh", "-c", 'exec "$@" </dev/null', "sh"]


def test_string_commands_run_under_bash_without_rc_files() -> None:
    assert shell_argv("echo hi && ls", True) == [*WRAP, "/bin/bash", "--norc", "--noprofile", "-c", "echo hi && ls"]


def test_argv_commands_are_passed_literally() -> None:
    assert shell_argv(["printf", "%s", "$(x)"], False) == [*WRAP, "printf", "%s", "$(x)"]


@pytest.mark.parametrize(
    ("command", "shell", "error"),
    [
        ("true", False, TypeError),
        (["true"], True, TypeError),
        ([], False, TypeError),
        (b"true", False, TypeError),
        (["a", 1], False, TypeError),
        (["a\x00b"], False, ValueError),
        ("a\x00b", True, ValueError),
    ],
)
def test_invalid_commands_are_rejected(command: object, shell: bool, error: type[Exception]) -> None:
    with pytest.raises(error):
        shell_argv(command, shell)  # pyright: ignore[reportArgumentType]


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("/workspace", ""),
        ("/workspace/", ""),
        ("/workspace/a/b.txt", "a/b.txt"),
        ("/workspace/a/../b", "b"),
        ("/workspace/有 空格#?.txt", "%E6%9C%89%20%E7%A9%BA%E6%A0%BC%23%3F.txt"),
        ("/workspace-evil/x", None),
        ("/tmp/x", None),
        ("/", None),
    ],
)
def test_rest_path_maps_onto_the_sandboxd_root(path: str, expected: str | None) -> None:
    assert rest_path("/workspace", path) == expected


def test_remove_protects_root_and_ancestors_only() -> None:
    assert is_protected("/workspace", "/workspace")
    assert is_protected("/workspace", "/workspace/")
    assert is_protected("/workspace", "/")
    assert not is_protected("/workspace", "/workspace/a")
    assert not is_protected("/workspace", "/workspaces")


def test_endpoint_for_pod_handles_ipv6() -> None:
    assert Endpoint.for_pod("10.42.0.7") == Endpoint("http://10.42.0.7:8080", "10.42.0.7:9090")
    assert Endpoint.for_pod("fd00::7") == Endpoint("http://[fd00::7]:8080", "[fd00::7]:9090")


def test_parse_listing_follows_symlinks_and_sorts() -> None:
    raw = b"f\tf\t4\tb.txt\0d\td\t4096\tdir\0d\tl\t3\tlink\0L\tl\t4\tloop\0f\tl\t5\tfl\0"
    assert parse_listing("/w", raw) == (
        FileEntry(name="b.txt", path="/w/b.txt", is_dir=False, size=4),
        FileEntry(name="dir", path="/w/dir", is_dir=True, size=None),
        FileEntry(name="fl", path="/w/fl", is_dir=False, size=None),
        FileEntry(name="link", path="/w/link", is_dir=True, size=None),
        FileEntry(name="loop", path="/w/loop", is_dir=False, size=None),
    )


def _limits(**kw: float) -> CommandLimits:
    base = {"max_commands": 3, "invocation_s": 100.0, "command_max_s": 50.0, "default_timeout_s": 20.0}
    return CommandLimits(**{**base, **kw})  # pyright: ignore[reportArgumentType]


def test_budget_defaults_caps_and_counts() -> None:
    budget = _Budget(_limits())
    assert budget.admit(None) == (20.0, None)
    timeout, note = budget.admit(500)
    assert timeout == 50.0
    assert note is not None
    assert "50" in note
    budget.seconds = 90
    timeout, note = budget.admit(30)
    assert timeout == 10.0
    assert note is not None
    assert "10" in note
    with pytest.raises(SandboxToolError, match="次数"):
        budget.admit(1)


def test_budget_refuses_when_time_is_spent() -> None:
    budget = _Budget(_limits(max_commands=10))
    budget.seconds = 99.5
    with pytest.raises(SandboxToolError, match="总时长"):
        budget.admit(5)
    assert budget.commands == 0


def test_unlimited_budget_passes_timeout_through() -> None:
    assert _Budget(None).admit(None) == (None, None)
    assert _Budget(None).admit(7) == (7, None)


def test_shutdown_time_idle_links_and_hard_cap() -> None:
    cap, idle = 15400.0, 1800.0
    assert shutdown_at(now=2000, max_until=cap, idle_s=idle, has_links=False) == 3800
    # Links keep the workspace up to its hard cap, never beyond.
    assert shutdown_at(now=2000, max_until=cap, idle_s=idle, has_links=True) == 15400
    assert shutdown_at(now=14000, max_until=cap, idle_s=idle, has_links=False) == 15400
    # A background job pushed the cap out: idle renewals follow it.
    assert shutdown_at(now=15000, max_until=20000.0, idle_s=idle, has_links=False) == 16800


@pytest.mark.parametrize(("port", "ok"), [(1024, True), (8000, True), (65535, True), (80, False), (1023, False)])
def test_validate_port_range(port: int, ok: bool) -> None:
    assert (validate_port(port) is None) is ok


@pytest.mark.parametrize("port", [8080, 9090, 65536, True])
def test_validate_port_rejects_reserved_and_invalid(port: int) -> None:
    assert validate_port(port) is not None


def test_generated_names() -> None:
    name = claim_name(10001, "pelican-game")
    assert name.startswith("ygg-sbx-10001-pelican-game-")
    suffix = name.removeprefix("ygg-sbx-10001-pelican-game-")
    assert len(suffix) == 6
    assert suffix.isalnum()
    assert suffix.lower() == suffix
    # A long group id and a 32-character name still fit an object name, without a dangling '-'.
    long = claim_name(1234567890123, "a" * 30 + "-b")
    assert len(long) <= 63
    assert "--" not in long
    assert long.startswith("ygg-sbx-1234567890123-aaaa")
    rid = link_id()
    assert len(rid) == 10
    assert rid.isalnum()


def test_mapping_round_trips_through_a_redis_hash() -> None:
    link = LinkRecord(
        id="abcdefghij",
        port=8000,
        url="https://abcdefghij.example.com",
        command="python -m http.server 8000",
        created_at=1.5,
        pgid=42,
        pid=43,
    )
    m = Mapping(
        group_id=10001,
        name="pelican-game",
        description="鹈鹕小游戏",
        claim="ygg-sbx-10001-pelican-game-abc123",
        sandbox="ygg-sbx-10001-pelican-game-abc123",
        pod_ip="10.42.0.7",
        created_at=1000.25,
        last_used=1100.5,
        expires_at=2900.75,
        max_until=15400.5,
        active=2,
        recreate=True,
        links=[link],
    )
    data = m.to_hash()
    assert all(isinstance(v, str) for v in data.values())
    back = Mapping.from_hash(10001, data)
    assert back == m
    assert back is not None
    info = back.info(("job1",))
    assert (info.name, info.description, info.max_until, info.jobs) == (
        "pelican-game",
        "鹈鹕小游戏",
        15400.5,
        ("job1",),
    )
    assert info.links[0].url == link.url
    assert info.links[0].port == 8000
    assert info.links[0].pid == 43
    assert info.active == 2
    assert info.recreate
    assert not info.pending


@pytest.mark.parametrize(
    "data",
    [
        {},
        {"active": "1"},
        {"claim": "c"},  # no name
        {"name": "Bad Name", "claim": "c"},
        {"name": "w"},  # no claim field at all
        {"name": "w", "claim": "c", "created_at": "x"},
        {"name": "w", "claim": "c", "links": "not json"},
    ],
)
def test_unusable_hashes_decode_to_none(data: dict[str, str]) -> None:
    assert Mapping.from_hash(1, data) is None


def test_mapping_tolerates_missing_optional_fields_and_negative_active() -> None:
    m = Mapping.from_hash(7, {"name": "w", "claim": "c", "active": "-1"})
    assert m is not None
    assert m.active == 0
    assert m.sandbox is None
    assert m.links == []
    assert m.description == ""
    pending = Mapping.from_hash(7, {"name": "w", "claim": "", "description": "空的"})
    assert pending is not None
    assert pending.pending
    assert pending.info().pending


@pytest.mark.parametrize("name", ["a", "pelican-game", "x2", "a-b-c", "a" * 32, "9lives"])
def test_valid_workspace_names(name: str) -> None:
    assert valid_name(name)
    assert name_error(name) is None


@pytest.mark.parametrize("name", ["", "-a", "a-", "Pelican", "a_b", "a b", "中文", "a" * 33, "a:b"])
def test_invalid_workspace_names(name: str) -> None:
    assert not valid_name(name)
    assert "小写字母" in (name_error(name) or "")


def test_workspace_keys_round_trip() -> None:
    assert parse_ws_key(ws_key(10001, "pelican-game")) == (10001, "pelican-game")
    assert parse_ws_key("ygg:sandbox:ws:10001") is None  # the old one-per-group key
    assert parse_ws_key("ygg:sandbox:ws:x:name") is None
    assert parse_ws_key("ygg:sandbox:ws:10001:Bad") is None


def test_descriptions_are_one_short_line() -> None:
    assert clean_description("  做网站\n  第二行 ") == "做网站 第二行"
    assert len(clean_description("字" * 500)) == 200


def test_workspace_list_for_the_model() -> None:
    now = 100_000.0
    base = Mapping(
        group_id=1,
        name="pelican-game",
        description="鹈鹕小游戏，代码在 /workspace/game",
        claim="c1",
        sandbox=None,
        pod_ip="10.0.0.1",
        created_at=now - 3900,
        last_used=now - 180,
        expires_at=now + 1620,
        max_until=now + 10000,
    ).info()
    busy = Mapping(
        group_id=1,
        name="site",
        description="",
        claim="c2",
        sandbox=None,
        pod_ip="10.0.0.2",
        created_at=now - 60,
        last_used=now,
        expires_at=now + 1800,
        max_until=now + 7200,
        active=1,
    ).info(("job1",))
    busy = replace(busy, links=(Link(port=8000, url="https://sbx-a.example.com", command="", created_at=0),))
    pending = Mapping(
        group_id=1,
        name="new-one",
        description="还没用",
        claim="",
        sandbox=None,
        pod_ip="",
        created_at=now,
        last_used=now,
        expires_at=0,
        max_until=0,
    ).info()
    text = format_workspaces([base, busy, pending], now=now, titles={"job1": "训练模型"}, current="site")
    assert text.splitlines() == [
        "- pelican-game：鹈鹕小游戏，代码在 /workspace/game",
        "  已存在 1 小时 5 分钟；闲置 3 分钟；约 27 分钟后闲置回收",
        "- site（当前选中）：（没有说明）",
        "  已存在 1 分钟；正在使用；最多还能保留 2 小时；公开链接 https://sbx-a.example.com（端口 8000）；"
        "后台任务 job1「训练模型」",
        "- new-one：还没用",
        "  空的，还没启动（第一次用时创建）",
    ]
    assert format_workspaces([], now=now, titles={}) == ""


def test_links_stored_without_pid_still_decode() -> None:
    link = LinkRecord.from_json({"id": "x", "port": 8000, "url": "u", "pgid": 7})
    assert (link.pgid, link.pid) == (7, None)


def test_reap_spares_kept_trees_and_link_listeners() -> None:
    links = [
        LinkRecord(id="a", port=8000, url="u", command="", created_at=0, pgid=40, pid=41),
        LinkRecord(id="b", port=8001, url="u", command="", created_at=0, pgid=None, pid=None),
    ]
    keep = {"kernel": "12", "job:abc": "30", "junk": "x"}
    assert reap_argv(keep, links) == [
        "/opt/ygg/reap",
        "--keep-tree",
        "30",
        "--keep-tree",
        "12",
        "--keep-pgid",
        "40",
        "--keep-tree",
        "41",
    ]
    assert reap_argv({}, []) == ["/opt/ygg/reap"]


def _port(**data: object) -> bytes:
    return (json.dumps(data) + "\n").encode()


def test_listener_must_listen_on_all_interfaces() -> None:
    ok = parse_listener(
        8000, 0, _port(listening=True, pid=5, pgid=4, addr="0.0.0.0", cmdline="python3 -m http.server"), b""
    )
    assert (ok.pid, ok.pgid, ok.cmdline) == (5, 4, "python3 -m http.server")
    assert parse_listener(8000, 0, _port(listening=True, pid=None, pgid=None, addr="::", cmdline=""), b"").pid is None
    with pytest.raises(SandboxError, match="没有程序在监听"):
        parse_listener(8000, 0, _port(listening=False, pid=None, pgid=None, addr=None, cmdline=""), b"")
    with pytest.raises(SandboxError, match=r"0\.0\.0\.0:8000"):
        parse_listener(8000, 0, _port(listening=True, pid=5, pgid=5, addr="127.0.0.1", cmdline="x"), b"")
    with pytest.raises(SandboxError, match="usage"):
        parse_listener(8000, 2, b"", b"usage: ygg port PORT")
    with pytest.raises(SandboxError, match="查不到"):
        parse_listener(8000, 0, b"not json", b"")


def test_claim_readiness() -> None:
    claim = {
        "status": {
            "conditions": [{"type": "Ready", "status": "True"}],
            "sandbox": {"name": "sb", "podIPs": ["10.42.0.9"]},
        }
    }
    assert _claim_ready(claim) == (True, "10.42.0.9", "sb", None)
    pending = {"status": {"conditions": [{"type": "Ready", "status": "False", "reason": "DependenciesNotReady"}]}}
    assert _claim_ready(pending) == (False, None, None, None)
    failed = {"status": {"conditions": [{"type": "Ready", "status": "False", "reason": "PodFailed"}]}}
    assert _claim_ready(failed)[3] == "PodFailed"
    assert _claim_ready({}) == (False, None, None, None)


def test_format_tree_sorts_and_limits() -> None:
    raw = "f\t2048\t0\tb.csv\nd\t4096\t0\tdata\nf\t10\t0\tdata/a.txt\nbroken line\n"
    tree = format_tree(raw).splitlines()
    assert len(tree) == 3
    assert tree[0].startswith("b.csv  2.0 KB  ")
    assert tree[1] == "data/"
    assert tree[2].startswith("data/a.txt  10 B  ")
    lines = format_tree("".join(f"f\t1\t0\tf{i:03}\n" for i in range(60)), limit=50).splitlines()
    assert len(lines) == 51
    assert lines[0].startswith("f000  1 B  ")
    assert "10" in lines[-1]
