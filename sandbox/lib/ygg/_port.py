"""Who listens on a TCP port: /proc/net/tcp{,6} LISTEN entries, socket inode -> /proc/<pid>/fd owner."""

import ipaddress
import os
from typing import Any

from ygg._common import cmdline_args, proc_stat

_LISTEN = "0A"
WILDCARD = ("0.0.0.0", "::")


def _addr(hex_addr: str) -> str:
    raw = bytes.fromhex(hex_addr)
    if len(raw) == 4:
        return str(ipaddress.IPv4Address(raw[::-1]))
    # tcp6: four 32-bit words, each in host (little-endian) byte order.
    return str(ipaddress.IPv6Address(b"".join(raw[i : i + 4][::-1] for i in range(0, 16, 4))))


def listeners(port: int) -> list[tuple[str, int]]:
    """(address, socket inode) of every LISTEN socket on ``port``, wildcard addresses first."""
    found: list[tuple[str, int]] = []
    for table in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            with open(table, encoding="ascii") as f:
                rows = f.read().splitlines()[1:]
        except OSError:
            continue
        for row in rows:
            fields = row.split()
            if len(fields) < 10 or fields[3] != _LISTEN:
                continue
            hex_addr, _, hex_port = fields[1].partition(":")
            if int(hex_port, 16) == port:
                found.append((_addr(hex_addr), int(fields[9])))
    found.sort(key=lambda item: item[0] not in WILDCARD)
    return found


def socket_owners(inodes: set[int]) -> dict[int, list[int]]:
    """inode -> pids holding it (only processes whose fds this uid may read)."""
    targets = {f"socket:[{inode}]": inode for inode in inodes}
    owners: dict[int, list[int]] = {}
    for name in os.listdir("/proc"):
        if not name.isdigit():
            continue
        fd_dir = f"/proc/{name}/fd"
        try:
            fds = os.listdir(fd_dir)
        except OSError:
            continue
        for fd in fds:
            try:
                link = os.readlink(f"{fd_dir}/{fd}")
            except OSError:
                continue
            inode = targets.get(link)
            if inode is not None:
                owners.setdefault(inode, []).append(int(name))
    return owners


def _topmost(pids: list[int]) -> int:
    """Of several holders of one socket (a pre-fork server), the one whose parent is not also a holder."""
    holders = set(pids)
    for pid in sorted(pids):
        st = proc_stat(pid)
        if st is None or st[1] not in holders:
            return pid
    return min(pids)


def info(port: int) -> dict[str, Any]:
    found = listeners(port)
    if not found:
        return {"listening": False, "pid": None, "pgid": None, "addr": None, "cmdline": ""}
    owners = socket_owners({inode for _, inode in found})
    addr, inode = found[0]
    public = addr in WILDCARD
    for candidate, candidate_inode in found:  # same reachability, but prefer a listener whose owner is visible
        if (candidate in WILDCARD) == public and candidate_inode in owners:
            addr, inode = candidate, candidate_inode
            break
    pids = owners.get(inode)
    pid = _topmost(pids) if pids else None
    st = proc_stat(pid) if pid is not None else None
    return {
        "listening": True,
        "pid": pid,
        "pgid": st[2] if st else None,
        "addr": addr,
        "cmdline": " ".join(cmdline_args(pid)) if pid is not None else "",
    }
