#!/usr/bin/env python3
"""Local interactive web UI to render git-latexdiff PDFs between two commits.

    python3 -m latexdiff_viewer.server                 # http://127.0.0.1:8765
    python3 -m latexdiff_viewer.server --repo /path/to/latex/project
    python3 -m latexdiff_viewer.server --port 9000 --config difftool.toml

No third-party dependencies. Build logic lives in `core`; project-specific paths
and commands come from `config` (a difftool.toml/json in the target repo, plus
CLI overrides). Generated PDFs are cached under <build_dir>/diffs.

Two kinds of build:
  * diff  — git-latexdiff between two commits (with per-change PDF bookmarks)
  * full  — a plain (no-diff) compile of a single commit (project's build_command)
"""

from __future__ import annotations

import argparse
import json
import os
import queue
import re
import tempfile
import threading
import urllib.parse
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import config as _config
from . import core

HERE = os.path.dirname(os.path.abspath(__file__))
INDEX_HTML = os.path.join(HERE, "index.html")

# Set in configure(); the resolved project config + the PDF cache directory.
CFG: _config.Config
OUT_DIR: str

# ---------------------------------------------------------------------------
# Job state
# ---------------------------------------------------------------------------

JOBS_LOCK = threading.Lock()
INDEX_IO_LOCK = threading.Lock()    # serialise index.json writes across threads
JOBS: dict[str, dict] = {}          # id -> job dict
BUILD_QUEUE: "queue.Queue[str]" = queue.Queue()


def configure(cfg: _config.Config, out_dir: str | None = None) -> None:
    global CFG, OUT_DIR
    CFG = cfg
    # Cache generated PDFs under the project's build dir when it has one, else a
    # dedicated dot-dir so we never scatter files in the repo root.
    default_cache = os.path.join(cfg.repo_root, cfg.build_dir or ".latexdiff", "diffs")
    OUT_DIR = out_dir or default_cache
    os.makedirs(OUT_DIR, exist_ok=True)


def index_path() -> str:
    return os.path.join(OUT_DIR, "index.json")


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def safe(token: str) -> str:
    return re.sub(r"[^0-9a-zA-Z]", "_", token)


def diff_id(old: str, new: str) -> str:
    return f"{old}..{new}"


def full_id(commit: str) -> str:
    return f"full:{commit}"


def pdf_name(job: dict) -> str:
    if job["kind"] == "full":
        return f"full_{safe(job['commit'])}.pdf"
    return f"diff_{safe(job['old'])}__{safe(job['new'])}.pdf"


def public_job(job: dict) -> dict:
    """A copy safe to send to the browser (no big log blobs)."""
    out = {k: v for k, v in job.items() if k != "log"}
    out["has_pdf"] = bool(job.get("pdf") and os.path.exists(
        os.path.join(OUT_DIR, job["pdf"])))
    out["log_tail"] = "\n".join((job.get("log") or "").splitlines()[-40:])
    return out


def save_index() -> None:
    os.makedirs(OUT_DIR, exist_ok=True)
    with JOBS_LOCK:
        data = {jid: {k: v for k, v in j.items() if k != "log"}
                for jid, j in JOBS.items()}
    with INDEX_IO_LOCK:   # unique tmp + serialised replace: no cross-thread race
        fd, tmp = tempfile.mkstemp(dir=OUT_DIR, suffix=".tmp")
        with os.fdopen(fd, "w") as fh:
            json.dump(data, fh, indent=2)
        os.replace(tmp, index_path())


def load_index() -> None:
    try:
        with open(index_path()) as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return
    with JOBS_LOCK:
        for jid, job in data.items():
            job.setdefault("kind", "diff")
            if job.get("status") in ("building", "queued"):
                job["status"] = "error"
                job["error"] = "Interrupted (server restarted during build)."
            if job.get("status") == "done":
                pdf = job.get("pdf")
                if not pdf or not os.path.exists(os.path.join(OUT_DIR, pdf)):
                    job["status"] = "error"
                    job["error"] = "PDF missing."
            JOBS[jid] = job


# ---------------------------------------------------------------------------
# Build worker (delegates the heavy lifting to core)
# ---------------------------------------------------------------------------

def _log_sink(jid: str):
    buf: list[str] = []

    def sink(line: str) -> None:
        buf.append(line)
        with JOBS_LOCK:
            if jid in JOBS:
                JOBS[jid]["log"] = "\n".join(buf[-400:])
    return sink


def _start(jid: str) -> dict:
    with JOBS_LOCK:
        job = JOBS[jid]
        job["status"] = "building"
        job["error"] = None
        job["started"] = now_iso()
        job["log"] = ""
    save_index()
    return job


def _finish(jid: str, out_pdf: str, rc: int, log_lines: list[str],
            elapsed: float, what: str) -> None:
    with JOBS_LOCK:
        job = JOBS[jid]
        job["log"] = "\n".join(log_lines[-400:])
        job["elapsed"] = elapsed
        job["finished"] = now_iso()
        if os.path.exists(out_pdf):
            job["status"] = "done"
            job["pdf"] = os.path.basename(out_pdf)
            job["error"] = None
        else:
            job["status"] = "error"
            job["error"] = (f"{what} exited {rc} and produced no PDF. "
                            "See the build log below.")
    save_index()


def run_diff_build(jid: str) -> None:
    job = _start(jid)
    out_pdf = os.path.join(OUT_DIR, pdf_name(job))
    res = core.build_diff(CFG, job["old"], job["new"], out_pdf,
                          on_line=_log_sink(jid))
    with JOBS_LOCK:
        JOBS[jid]["changes"] = res.changes
    _finish(jid, out_pdf, res.rc, res.log, res.elapsed, "git-latexdiff")


def run_full_build(jid: str) -> None:
    job = _start(jid)
    out_pdf = os.path.join(OUT_DIR, pdf_name(job))
    res = core.build_full(CFG, job["commit"], out_pdf, on_line=_log_sink(jid))
    _finish(jid, out_pdf, res.rc, res.log, res.elapsed, "build")


def build_worker() -> None:
    while True:
        jid = BUILD_QUEUE.get()
        try:
            with JOBS_LOCK:
                kind = JOBS[jid]["kind"]
            if kind == "full":
                run_full_build(jid)
            else:
                run_diff_build(jid)
        except Exception as exc:  # noqa: BLE001 - never let the worker die
            with JOBS_LOCK:
                job = JOBS.get(jid)
                if job:
                    job["status"] = "error"
                    job["error"] = f"{type(exc).__name__}: {exc}"
            save_index()
        finally:
            BUILD_QUEUE.task_done()


def enqueue(job: dict) -> dict:
    jid = job["id"]
    with JOBS_LOCK:
        existing = JOBS.get(jid)
        if existing and existing.get("status") in ("building", "queued"):
            return public_job(existing)
        JOBS[jid] = job
        result = public_job(job)
    save_index()
    BUILD_QUEUE.put(jid)
    return result


def enqueue_diff(old: str, new: str) -> dict:
    cm = core.commit_map(CFG.repo_root)
    o, n = cm.get(old, {}), cm.get(new, {})
    return enqueue({
        "id": diff_id(old, new), "kind": "diff",
        "old": old, "new": new,
        "old_subject": o.get("subject", ""), "new_subject": n.get("subject", ""),
        "old_date": o.get("date", ""), "new_date": n.get("date", ""),
        "status": "queued", "created": now_iso(),
        "pdf": None, "error": None, "log": "",
    })


def enqueue_full(commit: str) -> dict:
    info = core.commit_map(CFG.repo_root).get(commit, {})
    return enqueue({
        "id": full_id(commit), "kind": "full",
        "commit": commit,
        "subject": info.get("subject", ""), "date": info.get("date", ""),
        "status": "queued", "created": now_iso(),
        "pdf": None, "error": None, "log": "",
    })


def ensure_current_full() -> None:
    """Pin a 'current draft' = the full PDF of HEAD, auto-built on startup."""
    try:
        head = core.rev_parse(CFG.repo_root, "HEAD")
    except Exception:  # noqa: BLE001
        return
    cur_id = full_id(head)

    stale = []
    with JOBS_LOCK:
        for jid, j in list(JOBS.items()):
            if j.get("auto") and jid != cur_id:
                stale.append((jid, j.get("pdf")))
                JOBS.pop(jid, None)
        for j in JOBS.values():
            j.pop("current", None)
        existing = JOBS.get(cur_id)
    for _, pdf in stale:
        if pdf:
            try:
                os.remove(os.path.join(OUT_DIR, pdf))
            except OSError:
                pass

    have = bool(existing and existing.get("status") == "done"
                and existing.get("pdf")
                and os.path.exists(os.path.join(OUT_DIR, existing["pdf"])))
    if not have:
        enqueue_full(head)
    with JOBS_LOCK:
        JOBS[cur_id]["current"] = True
        JOBS[cur_id]["auto"] = True
    save_index()


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = "latexdiff-ui/2.0"

    def log_message(self, fmt, *args):  # quieter logs
        pass

    def _send_json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_bytes(self, body: bytes, ctype: str, code=200, extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        if not length:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw)
        except ValueError:
            return {}

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path in ("/", "/index.html"):
            self._serve_index()
        elif path == "/api/meta":
            self._send_json({"main": CFG.main, "repo": os.path.basename(CFG.repo_root),
                             "static": False})
        elif path == "/api/commits":
            self._api_commits()
        elif path == "/api/diffs":
            self._api_diffs()
        elif path.startswith("/api/diff/"):
            self._api_diff_status(urllib.parse.unquote(path[len("/api/diff/"):]))
        elif path.startswith("/pdf/"):
            self._serve_pdf(urllib.parse.unquote(path[len("/pdf/"):]))
        else:
            self._send_json({"error": "not found"}, 404)

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        if path == "/api/diff":
            self._api_create_diff()
        elif path == "/api/full":
            self._api_create_full()
        else:
            self._send_json({"error": "not found"}, 404)

    def do_DELETE(self):
        path = urllib.parse.urlparse(self.path).path
        if path.startswith("/api/diff/"):
            self._api_delete(urllib.parse.unquote(path[len("/api/diff/"):]))
        else:
            self._send_json({"error": "not found"}, 404)

    def _serve_index(self):
        try:
            with open(INDEX_HTML, "rb") as fh:
                body = fh.read()
        except OSError:
            self._send_bytes(b"index.html missing", "text/plain", 500)
            return
        self._send_bytes(body, "text/html; charset=utf-8")

    def _api_commits(self):
        try:
            self._send_json({"commits": core.list_commits(CFG.repo_root)})
        except Exception as exc:  # noqa: BLE001
            self._send_json({"error": str(exc)}, 500)

    def _api_diffs(self):
        with JOBS_LOCK:
            jobs = [public_job(j) for j in JOBS.values()]
        jobs.sort(key=lambda j: j.get("created", ""), reverse=True)
        self._send_json({"diffs": jobs})

    def _api_diff_status(self, jid):
        with JOBS_LOCK:
            job = JOBS.get(jid)
            payload = public_job(job) if job else None
        if payload is None:
            self._send_json({"error": "unknown job"}, 404)
        else:
            self._send_json(payload)

    def _api_create_diff(self):
        data = self._read_json()
        old = (data.get("old") or "").strip()
        new = (data.get("new") or "").strip()
        if not old or not new:
            self._send_json({"error": "old and new commits are required"}, 400)
            return
        if old == new:
            self._send_json({"error": "pick two different commits"}, 400)
            return
        self._send_json(enqueue_diff(old, new))

    def _api_create_full(self):
        data = self._read_json()
        commit = (data.get("commit") or "").strip()
        if not commit:
            self._send_json({"error": "commit is required"}, 400)
            return
        self._send_json(enqueue_full(commit))

    def _api_delete(self, jid):
        with JOBS_LOCK:
            job = JOBS.pop(jid, None)
        if job and job.get("pdf"):
            try:
                os.remove(os.path.join(OUT_DIR, job["pdf"]))
            except OSError:
                pass
        save_index()
        self._send_json({"ok": True})

    def _serve_pdf(self, jid):
        with JOBS_LOCK:
            job = JOBS.get(jid)
            pdf = job.get("pdf") if job else None
        if not pdf:
            self._send_bytes(b"no pdf", "text/plain", 404)
            return
        full = os.path.join(OUT_DIR, pdf)
        if not os.path.exists(full):
            self._send_bytes(b"pdf missing", "text/plain", 404)
            return
        with open(full, "rb") as fh:
            body = fh.read()
        self._send_bytes(body, "application/pdf", extra={
            "Content-Disposition": f'inline; filename="{pdf}"',
            "Cache-Control": "private, max-age=600"})


def run_server(host: str, port: int) -> None:
    load_index()
    worker = threading.Thread(target=build_worker, daemon=True)
    worker.start()
    ensure_current_full()

    httpd = ThreadingHTTPServer((host, port), Handler)
    url = f"http://{host}:{port}"
    print(f"latex-diff-viewer  ->  {url}", flush=True)
    print(f"repo: {CFG.repo_root}", flush=True)
    print(f"main: {CFG.main}   out: {OUT_DIR}", flush=True)
    print(f"config: {CFG.source or '(defaults)'}", flush=True)
    print("Ctrl-C to stop.", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="git-latexdiff local web UI")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--repo", default=".", help="LaTeX project repo root")
    ap.add_argument("--config", default=None, help="path to difftool.toml/json")
    ap.add_argument("--main", default=None, help="override the main LaTeX file")
    args = ap.parse_args(argv)

    cfg = _config.load(args.repo, args.config, overrides={"main": args.main})
    configure(cfg)
    run_server(args.host, args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
