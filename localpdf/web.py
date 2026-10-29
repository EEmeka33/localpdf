"""Local web interface – an iLovePDF-style tool grid served from 127.0.0.1.

Files are processed in a private temporary folder on this machine and deleted
right after the result is downloaded. Nothing ever leaves your computer.
"""
from __future__ import annotations

import io
import os
import shutil
import tempfile
import threading
import webbrowser
import zipfile
from dataclasses import asdict
from pathlib import Path

from flask import Flask, abort, jsonify, request, send_file, send_from_directory
from werkzeug.utils import secure_filename

from . import __version__
from .registry import CATEGORIES, load_all
from .runner import run_tool
from .utils import ToolError

STATIC = Path(__file__).parent / "static"


def create_app() -> Flask:
    app = Flask(__name__, static_folder=None)
    app.config["MAX_CONTENT_LENGTH"] = int(os.environ.get("LOCALPDF_MAX_UPLOAD_MB", "2048")) * 1024 * 1024
    tools = load_all()

    @app.get("/")
    def index():
        return send_from_directory(STATIC, "index.html")

    @app.get("/static/<path:name>")
    def static_files(name):
        return send_from_directory(STATIC, name)

    @app.get("/api/tools")
    def list_tools():
        payload = []
        for t in tools.values():
            d = {k: v for k, v in asdict(t).items() if k != "func"}
            d["opts"] = [asdict(o) | {"flag": o.flag} for o in t.opts]
            payload.append(d)
        return jsonify({"version": __version__, "categories": CATEGORIES, "tools": payload})

    @app.post("/api/run/<name>")
    def run(name):
        if name not in tools:
            abort(404)
        t = tools[name]
        work = Path(tempfile.mkdtemp(prefix="localpdf-web-"))
        try:
            inputs = []
            for i, f in enumerate(request.files.getlist("files")):
                # keep the user's order and avoid name clashes
                target = work / "in" / f"{i:03d}" / (secure_filename(f.filename or "") or f"file{i}")
                target.parent.mkdir(parents=True)
                f.save(target)
                inputs.append(str(target))
            url = request.form.get("url", "").strip()
            if url and t.allow_url:
                inputs.insert(0, url)
            options = {}
            for o in t.opts:
                if o.type == "file":
                    up = request.files.get(f"opt_{o.name}")
                    if up and up.filename:
                        p = work / "opt" / (secure_filename(up.filename) or o.name)
                        p.parent.mkdir(parents=True, exist_ok=True)
                        up.save(p)
                        options[o.name] = str(p)
                elif o.type == "bool":
                    options[o.name] = request.form.get(o.name, "false")
                else:
                    options[o.name] = request.form.get(o.name, "")
            outdir = work / "out"
            outdir.mkdir()
            result = run_tool(t, inputs, None, options, outdir=outdir)
            if isinstance(result, str):
                shutil.rmtree(work, ignore_errors=True)
                return jsonify({"text": result})
            files = [Path(p) for p in result]
            if not files:
                raise ToolError("the tool produced no output")
            if len(files) == 1:
                data = files[0].read_bytes()
                download = files[0].name
            else:
                buf = io.BytesIO()
                with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
                    for p in files:
                        z.write(p, p.name)
                data, download = buf.getvalue(), f"{Path(inputs[0]).stem if inputs else name}_{name}.zip"
            shutil.rmtree(work, ignore_errors=True)
            resp = send_file(io.BytesIO(data), as_attachment=True, download_name=download)
            resp.headers["X-Output-Count"] = str(len(files))
            return resp
        except ToolError as e:
            shutil.rmtree(work, ignore_errors=True)
            return jsonify({"error": str(e)}), 400
        except Exception as e:  # noqa: BLE001 – report anything else to the page
            shutil.rmtree(work, ignore_errors=True)
            app.logger.exception("tool %s failed", name)
            return jsonify({"error": f"{type(e).__name__}: {e}"}), 500

    return app


def serve(host: str = "127.0.0.1", port: int = 8765, open_browser: bool = False) -> None:
    app = create_app()
    url = f"http://{'127.0.0.1' if host in ('0.0.0.0', '::') else host}:{port}"
    print(f"localpdf web interface running on {url}  (Ctrl+C to stop)")
    if host not in ("127.0.0.1", "localhost", "::1"):
        print("warning: listening on a non-local interface – anyone on your network can use it")
    if open_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    try:
        from waitress import serve as waitress_serve  # type: ignore

        waitress_serve(app, host=host, port=port, threads=4)
    except ImportError:
        app.run(host=host, port=port, threaded=True)
