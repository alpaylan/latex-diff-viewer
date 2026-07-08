"""Generate a static GitHub Pages viewer: pre-built diffs + a manifest + index.html.

CI can't host the interactive server, so we pre-render the diffs listed in
`cfg.pages_pairs` (plus a full PDF of HEAD as the "current draft") into a folder
alongside a `manifest.json` and the shared `index.html`. `index.html` detects the
absence of the live API and renders from that manifest instead.
"""

from __future__ import annotations

import json
import os
import re
import shutil
from datetime import datetime, timezone

from . import config as _config
from . import core

HERE = os.path.dirname(os.path.abspath(__file__))
INDEX_HTML = os.path.join(HERE, "index.html")


def _safe(token: str) -> str:
    return re.sub(r"[^0-9a-zA-Z]", "_", token)


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def generate(cfg: _config.Config, out_dir: str, on_line=None) -> dict:
    os.makedirs(out_dir, exist_ok=True)
    diffs: list[dict] = []

    # Each configured pair -> a diff PDF + its change index.
    for old_ref, new_ref in cfg.pages_pairs:
        try:
            core.ensure_ref(cfg.repo_root, old_ref)
            core.ensure_ref(cfg.repo_root, new_ref)
            o = core.commit_info(cfg.repo_root, old_ref)
            n = core.commit_info(cfg.repo_root, new_ref)
        except Exception as exc:  # noqa: BLE001
            if on_line:
                on_line(f"[pages] skip {old_ref}..{new_ref}: {exc}")
            continue
        name = f"diff_{_safe(o['short'])}__{_safe(n['short'])}.pdf"
        res = core.build_diff(cfg, o["hash"], n["hash"],
                              os.path.join(out_dir, name), on_line=on_line)
        if not res.ok:
            if on_line:
                on_line(f"[pages] diff {old_ref}..{new_ref} produced no PDF")
            continue
        diffs.append({
            "id": f"{o['short']}..{n['short']}", "kind": "diff", "status": "done",
            "pdf": name, "old": o["short"], "new": n["short"],
            "old_subject": o["subject"], "new_subject": n["subject"],
            "old_date": o["date"], "new_date": n["date"],
            "elapsed": res.elapsed, "changes": res.changes,
        })

    # A full PDF of HEAD, shown as the pinned "current draft".
    head = core.commit_info(cfg.repo_root, "HEAD")
    full_name = f"full_{_safe(head['short'])}.pdf"
    full = core.build_full(cfg, head["hash"], os.path.join(out_dir, full_name),
                           on_line=on_line)
    if full.ok:
        diffs.insert(0, {
            "id": f"full:{head['short']}", "kind": "full", "status": "done",
            "pdf": full_name, "commit": head["short"], "current": True,
            "subject": head["subject"], "date": head["date"],
            "elapsed": full.elapsed, "changes": [],
        })

    manifest = {
        "generated": _now_iso(),
        "repo": os.path.basename(cfg.repo_root),
        "main": cfg.main,
        "diffs": diffs,
    }
    with open(os.path.join(out_dir, "manifest.json"), "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=2)
    shutil.copy2(INDEX_HTML, os.path.join(out_dir, "index.html"))
    # A .nojekyll keeps GitHub Pages from mangling the asset filenames.
    open(os.path.join(out_dir, ".nojekyll"), "w").close()
    if on_line:
        on_line(f"[pages] wrote {len(diffs)} item(s) to {out_dir}")
    return manifest
