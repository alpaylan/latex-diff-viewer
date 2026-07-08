"""Append a single diff to a persistent, publicly-served store (a git branch).

The issue-driven flow uses this: an issue titled `latexdiff <base>..<head>` fires
a workflow that calls `store-add`, which builds that one diff and *merges* it into
a store directory (the `latexdiff-store` branch) — a `manifest.json` index plus the
diff PDFs plus the viewer. GitHub Pages serves that branch, so the viewer can render
any already-built diff directly (no auth, unlike Actions artifacts), and offer to
open an issue for ones that don't exist yet.
"""

from __future__ import annotations

import json
import os
import shutil

from . import config as _config
from . import core
from .pages import INDEX_HTML, _now_iso, _safe


def manifest_path(store_dir: str) -> str:
    return os.path.join(store_dir, "manifest.json")


def load_manifest(store_dir: str) -> dict:
    try:
        with open(manifest_path(store_dir), encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def _repo_full() -> str:
    return os.environ.get("GITHUB_REPOSITORY", "")


def add_diff(cfg: _config.Config, store_dir: str, base_ref: str, head_ref: str,
             retain: int = 50, on_line=None) -> dict:
    """Build one diff and merge it into `store_dir` (manifest + PDF), pruning to
    the `retain` most-recent diffs. Returns a JSON-able result dict."""
    os.makedirs(store_dir, exist_ok=True)
    try:
        core.ensure_ref(cfg.repo_root, base_ref)
        core.ensure_ref(cfg.repo_root, head_ref)
        o = core.commit_info(cfg.repo_root, base_ref)
        n = core.commit_info(cfg.repo_root, head_ref)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"could not resolve {base_ref}..{head_ref}: {exc}"}

    name = f"diff_{_safe(o['short'])}__{_safe(n['short'])}.pdf"
    res = core.build_diff(cfg, o["hash"], n["hash"],
                          os.path.join(store_dir, name), on_line=on_line)
    if not res.ok:
        return {"ok": False, "error": "diff build produced no PDF",
                "id": f"{o['short']}..{n['short']}"}

    entry = {
        "id": f"{o['short']}..{n['short']}", "kind": "diff", "status": "done",
        "pdf": name, "old": o["short"], "new": n["short"],
        "old_subject": o["subject"], "new_subject": n["subject"],
        "old_date": o["date"], "new_date": n["date"],
        "elapsed": res.elapsed, "changes": res.changes, "added": _now_iso(),
    }

    manifest = load_manifest(store_dir)
    diffs = [d for d in manifest.get("diffs", []) if d.get("id") != entry["id"]]
    diffs.insert(0, entry)
    diffs.sort(key=lambda d: d.get("added", ""), reverse=True)   # newest first

    keep = diffs[:max(int(retain), 1)]
    for dropped in diffs[max(int(retain), 1):]:                  # prune oldest
        pdf = dropped.get("pdf")
        if pdf and os.path.exists(os.path.join(store_dir, pdf)):
            try:
                os.remove(os.path.join(store_dir, pdf))
            except OSError:
                pass

    repo_full = _repo_full()
    manifest = {
        "generated": _now_iso(),
        "repo": (repo_full.split("/")[-1] or os.path.basename(cfg.repo_root)),
        "repo_full": repo_full,
        "main": cfg.main,
        "diffs": keep,
    }
    with open(manifest_path(store_dir), "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=2)
    shutil.copy2(INDEX_HTML, os.path.join(store_dir, "index.html"))
    open(os.path.join(store_dir, ".nojekyll"), "w").close()

    if on_line:
        on_line(f"[store] added {entry['id']} — {len(keep)} kept")
    return {"ok": True, "id": entry["id"], "pdf": name,
            "changed_pages": len(res.changes), "kept": len(keep)}
