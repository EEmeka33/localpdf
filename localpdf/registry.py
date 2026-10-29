"""Central tool registry.

Every tool is declared once with the ``@tool`` decorator.  The CLI, the local
web UI and the test-suite are all generated from this registry, so adding a
tool means writing a single function.

Tool function contract::

    def my_tool(inputs: list[str], output: Path, **options) -> list[Path] | str

* ``inputs``  – input file paths (or URLs for tools with ``allow_url``)
* ``output``  – target file, or a directory when ``output="dir"``
* returns     – the list of files written, or a text report (``output="none"``)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

CATEGORIES = [
    "Organize PDF",
    "Optimize PDF",
    "Convert to PDF",
    "Convert from PDF",
    "Edit PDF",
    "PDF Security",
    "PDF Intelligence",
    "Extras",
]


@dataclass
class Opt:
    name: str
    type: str = "str"  # str | int | float | bool | choice | file | text
    default: Any = None
    help: str = ""
    choices: list[str] | None = None
    required: bool = False

    @property
    def flag(self) -> str:
        return "--" + self.name.replace("_", "-")


@dataclass
class Tool:
    name: str
    title: str
    category: str
    help: str
    func: Callable[..., Any]
    accept: tuple[str, ...] = (".pdf",)
    multiple: bool = False  # tool takes several input files
    min_inputs: int = 1
    output: str = "pdf"  # output extension, "dir" for many files, "none" for a text report
    opts: list[Opt] = field(default_factory=list)
    requires: tuple[str, ...] = ()  # optional external dependencies (see doctor)
    allow_url: bool = False
    extra: bool = False  # not part of iLovePDF's line-up – a bonus tool
    aliases: tuple[str, ...] = ()

    def output_ext(self, options: dict[str, Any]) -> str:
        """Some tools pick their output extension from an option (e.g. --format)."""
        if self.output not in ("dir", "none"):
            for o in self.opts:
                if o.name in ("format", "output_type") and o.type == "choice" and options.get(o.name):
                    return options[o.name]
        return self.output


TOOLS: dict[str, Tool] = {}


def tool(**kw: Any) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    def deco(func: Callable[..., Any]) -> Callable[..., Any]:
        t = Tool(func=func, **kw)
        if t.category not in CATEGORIES:
            raise ValueError(f"unknown category {t.category!r}")
        TOOLS[t.name] = t
        return func

    return deco


def load_all() -> dict[str, Tool]:
    # Importing the modules registers their tools.
    from .tools import ai, convert_from, convert_to, edit, extras, optimize, organize, security  # noqa: F401

    return TOOLS


def get(name: str) -> Tool:
    load_all()
    if name in TOOLS:
        return TOOLS[name]
    for t in TOOLS.values():
        if name in t.aliases:
            return t
    raise KeyError(name)
