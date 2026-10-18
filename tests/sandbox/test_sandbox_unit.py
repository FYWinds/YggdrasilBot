"""Pure sandbox rules: argv building, path mapping, listings, limits, renewal math, links, mapping encoding."""

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
from yggdrasil.sandbox.errors import SandboxToolError
from yggdrasil.sandbox.manager import _claim_ready, format_tree
from yggdrasil.sandbox.state import LinkRecord, Mapping, claim_name, link_id, shutdown_at, validate_port

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


def test_shutdown_time_idle_links_and_max_age() -> None:
    created, idle, age = 1000.0, 1800.0, 14400.0
    assert shutdown_at(now=2000, created_at=created, idle_s=idle, max_age_s=age, has_links=False) == 3800
    # Links keep the workspace up to its hard max age, never beyond.
    assert shutdown_at(now=2000, created_at=created, idle_s=idle, max_age_s=age, has_links=True) == 15400
    assert shutdown_at(now=14000, created_at=created, idle_s=idle, max_age_s=age, has_links=False) == 15400


@pytest.mark.parametrize(("port", "ok"), [(1024, True), (8000, True), (65535, True), (80, False), (1023, False)])
def test_validate_port_range(port: int, ok: bool) -> None:
    assert (validate_port(port) is None) is ok


@pytest.mark.parametrize("port", [8080, 9090, 65536, True])
def test_validate_port_rejects_reserved_and_invalid(port: int) -> None:
    assert validate_port(port) is not None


def test_generated_names() -> None:
    name = claim_name(10001)
    assert name.startswith("ygg-sbx-10001-")
    suffix = name.removeprefix("ygg-sbx-10001-")
    assert len(suffix) == 6
    assert suffix.isalnum()
    assert suffix.lower() == suffix
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
    )
    m = Mapping(
        group_id=10001,
        claim="ygg-sbx-10001-abc123",
        sandbox="ygg-sbx-10001-abc123",
        pod_ip="10.42.0.7",
        created_at=1000.25,
        last_used=1100.5,
        expires_at=2900.75,
        active=2,
        recreate=True,
        links=[link],
    )
    data = m.to_hash()
    assert all(isinstance(v, str) for v in data.values())
    back = Mapping.from_hash(10001, data)
    assert back == m
    assert back is not None
    info = back.info()
    assert info.links[0].url == link.url
    assert info.links[0].port == 8000
    assert info.active == 2
    assert info.recreate


@pytest.mark.parametrize(
    "data", [{}, {"active": "1"}, {"claim": "c", "created_at": "x"}, {"claim": "c", "links": "not json"}]
)
def test_unusable_hashes_decode_to_none(data: dict[str, str]) -> None:
    assert Mapping.from_hash(1, data) is None


def test_mapping_tolerates_missing_optional_fields_and_negative_active() -> None:
    m = Mapping.from_hash(7, {"claim": "c", "active": "-1"})
    assert m is not None
    assert m.active == 0
    assert m.sandbox is None
    assert m.links == []


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
