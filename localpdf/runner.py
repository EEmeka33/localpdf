"""Run a tool: default output names, batch processing, option coercion."""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .registry import Tool
from .utils import ToolError


def coerce(tool: Tool, options: dict[str, Any]) -> dict[str, Any]:
    """Convert string option values (from the web form) to their declared types and fill defaults."""
    out = {}
    for o in tool.opts:
        v = options.get(o.name, None)
        if v in (None, ""):
            if o.required:
                raise ToolError(f"option {o.flag} is required")
            if o.default is not None:
                out[o.name] = o.default
            continue
        try:
            if o.type == "int":
                v = int(v)
            elif o.type == "float":
                v = float(v)
            elif o.type == "bool" and isinstance(v, str):
                v = v.lower() in ("1", "true", "yes", "on")
        except ValueError:
            raise ToolError(f"{o.flag}: expected a {o.type}, got {v!r}") from None
        if o.choices and str(v) not in o.choices:
            raise ToolError(f"{o.flag}: choose one of {', '.join(o.choices)}")
        out[o.name] = v
    return out


def default_output(tool: Tool, source: str, ext: str, outdir: Path | None = None) -> Path:
    if re.match(r"^[a-z]+://", source, re.I):  # URL
        name = re.sub(r"^[a-z]+://(www\.)?", "", source, flags=re.I)
        stem = re.sub(r"[^\w.-]+", "_", name).strip("_.")[:80] or "page"
        base = outdir or Path.cwd()
    else:
        p = Path(source)
        stem, base = p.stem, outdir or p.parent
    if ext == "dir":
        return base / f"{stem}_{tool.name}"
    candidate = base / f"{stem}.{ext}"
    if candidate.exists() or Path(source).suffix.lower() == f".{ext}":
        candidate = base / f"{stem}_{tool.name.replace('-', '_')}.{ext}"
    return candidate


def run_tool(tool: Tool, inputs: list[str], output: str | Path | None = None, options: dict[str, Any] | None = None,
             outdir: Path | None = None) -> list[Path] | str:
    """Run ``tool``. Single-file tools given several inputs are run once per input (batch mode)."""
    options = coerce(tool, options or {})
    if len(inputs) < tool.min_inputs:
        raise ToolError(f"{tool.name} needs at least {tool.min_inputs} input file(s)")
    if tool.accept:
        for i in inputs:
            is_url = tool.allow_url and (re.match(r"^[a-z]+://", i, re.I) or not Path(i).exists())
            if not is_url and Path(i).suffix.lower() not in tool.accept and not Path(i).is_dir():
                raise ToolError(f"{Path(i).name}: unsupported file type for {tool.name} "
                                f"(accepts {', '.join(tool.accept)})")
    ext = tool.output_ext(options)

    batch = not tool.multiple and len(inputs) > 1
    if batch:
        target_dir = Path(output) if output else None
        if target_dir:
            target_dir.mkdir(parents=True, exist_ok=True)
        results: list[Path] = []
        reports = []
        for i in inputs:
            out = default_output(tool, i, ext, target_dir or outdir)
            res = tool.func([i], out, **options)
            if isinstance(res, str):
                reports.append(f"== {i} ==\n{res}")
            else:
                results += res
        return "\n\n".join(reports) if reports else results

    if ext == "none":
        return tool.func(inputs, None, **options)
    source = inputs[0] if inputs else tool.name
    target = Path(output) if output else default_output(tool, source, ext, outdir)
    if ext != "dir":
        target.parent.mkdir(parents=True, exist_ok=True)
    result = tool.func(inputs, target, **options)
    return result
