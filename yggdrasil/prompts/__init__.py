"""Prompt templates: repo defaults (``*.j2`` next to this file) overridable from the dashboard.

``render(key, **vars)`` uses the active database version if one exists, else the
default file. Templates are Jinja2; unknown variables render empty so an edited
prompt referencing a removed variable degrades instead of failing. ``parse`` and
``compile_template`` take a template body as text (the dashboard's prompt editor).
"""

from functools import cache
from pathlib import Path

from jinja2 import Environment, Template, nodes

from yggdrasil.runtime import snapshot

_DIR = Path(__file__).parent
_env = Environment(autoescape=False, keep_trailing_newline=False, trim_blocks=True, lstrip_blocks=True)


def default_keys() -> list[str]:
    return sorted(p.stem for p in _DIR.glob("*.j2"))


def default_body(key: str) -> str:
    path = _DIR / f"{key}.j2"
    if not path.is_file():
        raise KeyError(key)
    return path.read_text(encoding="utf-8")


def parse(source: str) -> nodes.Template:
    """The template's syntax tree; raises ``TemplateSyntaxError`` when the body isn't valid Jinja2."""
    return _env.parse(source)


@cache
def compile_template(source: str) -> Template:
    return _env.from_string(source)


def body(key: str) -> str:
    return snapshot().prompts.get(key) or default_body(key)


def render(key: str, **variables: object) -> str:
    return compile_template(body(key)).render(**variables).strip()
