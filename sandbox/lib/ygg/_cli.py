"""`/opt/ygg/ygg` command line. Each command prints exactly one JSON line on success; usage errors exit 2 with a
message on stderr. Subcommand modules are imported on demand so `sh` / `proc` / `port` / `rpc` start fast."""

import argparse
import os
import sys

USAGE = """usage:
  ygg sh run <dir> --timeout S [--head N] [--tail N]
  ygg sh start <dir>
  ygg proc kill <dir>
  ygg py run <cell_dir> --timeout S [--max-images N] [--head N] [--tail N]
  ygg py restart
  ygg port <port>
  ygg rpc start <job_dir> [--env K=V]... [--cwd DIR] -- <argv...>
  ygg rpc send <job_dir> (--json STR | --file PATH)
  ygg rpc close <job_dir>"""

HEAD, TAIL, MAX_IMAGES = 2000, 6000, 6


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:  # type: ignore[override]
        sys.stderr.write(f"ygg: {message}\n{USAGE}\n")
        sys.exit(2)


def _parser(prog: str) -> _Parser:
    return _Parser(prog=prog, add_help=False)


def _nonneg(value: str) -> int:
    n = int(value)
    if n < 0:
        raise argparse.ArgumentTypeError("must be >= 0")
    return n


def _positive(value: str) -> float:
    f = float(value)
    if not f > 0:
        raise argparse.ArgumentTypeError("must be > 0")
    return f


def _output_args(p: _Parser) -> None:
    p.add_argument("--head", type=_nonneg, default=HEAD)
    p.add_argument("--tail", type=_nonneg, default=TAIL)


def main(argv: list[str]) -> int:
    cli = os.path.abspath(argv[0])
    args = argv[1:]
    if len(args) < 2 and args[:1] != ["port"]:
        sys.stderr.write(USAGE + "\n")
        return 2
    group, rest = args[0], args[1:]
    cmd = f"{group} {rest[0]}" if group != "port" else "port"

    if cmd == "sh run":
        p = _parser("ygg sh run")
        p.add_argument("dir")
        p.add_argument("--timeout", type=_positive, required=True)
        _output_args(p)
        ns = p.parse_args(rest[1:])
        from ygg import _sh

        return _sh.run(ns.dir, ns.timeout, ns.head, ns.tail)
    if cmd == "sh start":
        p = _parser("ygg sh start")
        p.add_argument("dir")
        ns = p.parse_args(rest[1:])
        from ygg import _sh

        return _sh.start(cli, ns.dir)
    if cmd == "proc kill":
        p = _parser("ygg proc kill")
        p.add_argument("dir")
        ns = p.parse_args(rest[1:])
        from ygg import _sh

        return _sh.kill(ns.dir)
    if cmd == "py run":
        p = _parser("ygg py run")
        p.add_argument("cell_dir")
        p.add_argument("--timeout", type=_positive, required=True)
        p.add_argument("--max-images", type=_nonneg, default=MAX_IMAGES)
        _output_args(p)
        ns = p.parse_args(rest[1:])
        from ygg import _py

        return _py.run(ns.cell_dir, ns.timeout, ns.max_images, ns.head, ns.tail)
    if cmd == "py restart":
        _parser("ygg py restart").parse_args(rest[1:])
        from ygg import _py

        return _py.restart()
    if cmd == "port":
        p = _parser("ygg port")
        p.add_argument("port", type=int)
        ns = p.parse_args(rest)
        if not 1 <= ns.port <= 65535:
            p.error("port must be 1-65535")
        from ygg import _common, _port

        return _common.emit(_port.info(ns.port))
    if cmd == "rpc start":
        own, sep, child = _split(rest[1:])
        if not sep:
            _parser("ygg rpc start").error("rpc start needs -- before the command")
        p = _parser("ygg rpc start")
        p.add_argument("job_dir")
        p.add_argument("--env", action="append", default=[])
        p.add_argument("--cwd")
        ns = p.parse_args(own)
        from ygg import _rpc

        return _rpc.start(cli, ns.job_dir, ns.env, ns.cwd, child)
    if cmd == "rpc send":
        p = _parser("ygg rpc send")
        p.add_argument("job_dir")
        p.add_argument("--json")
        p.add_argument("--file")
        ns = p.parse_args(rest[1:])
        from ygg import _rpc

        return _rpc.send(ns.job_dir, ns.json, ns.file)
    if cmd == "rpc close":
        p = _parser("ygg rpc close")
        p.add_argument("job_dir")
        ns = p.parse_args(rest[1:])
        from ygg import _rpc

        return _rpc.close(ns.job_dir)
    if cmd == "_supervise sh" and len(rest) == 2:
        from ygg import _sh

        return _sh.supervise(rest[1])
    if cmd == "_supervise rpc" and len(rest) >= 4 and rest[2] == "--":
        from ygg import _rpc

        return _rpc.supervise(rest[1], rest[3:])
    sys.stderr.write(f"ygg: unknown command {' '.join(args[:2])!r}\n{USAGE}\n")
    return 2


def _split(args: list[str]) -> tuple[list[str], bool, list[str]]:
    if "--" not in args:
        return args, False, []
    i = args.index("--")
    return args[:i], True, args[i + 1 :]
