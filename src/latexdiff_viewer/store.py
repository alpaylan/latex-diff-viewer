"""Append diffs and full renders to a persistent, publicly-served store (a branch).

The store directory (the `latexdiff-store` branch) holds a `manifest.json` index +
the PDFs + the viewer. GitHub Pages serves it, so the viewer renders any built diff
or full PDF directly (no auth, unlike Actions artifacts). Entries are one of:

  * kind "diff" — a base..head latexdiff (id "<old>..<new>").
  * kind "full" — a no-diff render of one commit (id "full:<short>"); the newest is
    flagged current (the "current draft").
"""

from __future__ import annotations

import json
import os
import shutil

from . import config as _config
from . import core
from .pages import INDEX_HTML, _now_iso, _safe


# Bump when the change-capture mechanism changes, so older cached entries are
# rebuilt on next request rather than kept by the idempotency check.
#   v2 = --no-cleanup harvest (changed pages without a build_dir)
#   v3 = zref shipout pages (floats/figures report the page they land on)
INDEX_VERSION = 3


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


def _existing(store_dir: str, entry_id: str):
    """A current-version entry with that id whose PDF is present, else None."""
    for d in load_manifest(store_dir).get("diffs", []):
        if d.get("id") == entry_id and d.get("pdf") \
                and os.path.exists(os.path.join(store_dir, d["pdf"])) \
                and d.get("index_version", 1) >= INDEX_VERSION:
            return d
    return None


def _merge_write(cfg: _config.Config, store_dir: str, entry: dict,
                 retain: int, full_retain: int = 10) -> int:
    """Merge `entry` into the manifest, prune diffs/fulls separately (keeping the
    newest `retain` diffs and `full_retain` fulls), flag the newest full current,
    and write the manifest + viewer. Returns how many entries were kept."""
    entries = [d for d in load_manifest(store_dir).get("diffs", []) if d.get("id") != entry["id"]]
    entries.insert(0, entry)
    diffs = sorted((e for e in entries if e.get("kind") != "full"),
                   key=lambda d: d.get("added", ""), reverse=True)
    fulls = sorted((e for e in entries if e.get("kind") == "full"),
                   key=lambda d: d.get("added", ""), reverse=True)
    keep_diffs, keep_fulls = diffs[:max(retain, 1)], fulls[:max(full_retain, 1)]
    for dropped in diffs[max(retain, 1):] + fulls[max(full_retain, 1):]:
        pdf = dropped.get("pdf")
        if pdf and os.path.exists(os.path.join(store_dir, pdf)):
            try:
                os.remove(os.path.join(store_dir, pdf))
            except OSError:
                pass
    for i, f in enumerate(keep_fulls):
        f["current"] = (i == 0)   # newest full is the "current draft"

    repo_full = _repo_full()
    manifest = {
        "generated": _now_iso(),
        "repo": (repo_full.split("/")[-1] or os.path.basename(cfg.repo_root)),
        "repo_full": repo_full,
        "main": cfg.main,
        "diffs": keep_diffs + keep_fulls,
    }
    os.makedirs(store_dir, exist_ok=True)
    with open(manifest_path(store_dir), "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=2)
    shutil.copy2(INDEX_HTML, os.path.join(store_dir, "index.html"))
    open(os.path.join(store_dir, ".nojekyll"), "w").close()
    return len(keep_diffs) + len(keep_fulls)


def add_diff(cfg: _config.Config, store_dir: str, base_ref: str, head_ref: str,
             retain: int = 50, on_line=None, skip_existing: bool = True) -> dict:
    """Build one diff and merge it into the store. An already-built (current-version)
    diff for the same pair is returned as-is when skip_existing is set."""
    os.makedirs(store_dir, exist_ok=True)
    try:
        core.ensure_ref(cfg.repo_root, base_ref)
        core.ensure_ref(cfg.repo_root, head_ref)
        o = core.commit_info(cfg.repo_root, base_ref)
        n = core.commit_info(cfg.repo_root, head_ref)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"could not resolve {base_ref}..{head_ref}: {exc}"}

    diff_id = f"{o['short']}..{n['short']}"
    if skip_existing and (d := _existing(store_dir, diff_id)):
        if on_line:
            on_line(f"[store] {diff_id} already built — skipping rebuild")
        return {"ok": True, "id": diff_id, "pdf": d["pdf"], "existing": True,
                "changed_pages": len(d.get("changes", []))}

    name = f"diff_{_safe(o['short'])}__{_safe(n['short'])}.pdf"
    res = core.build_diff(cfg, o["hash"], n["hash"],
                          os.path.join(store_dir, name), on_line=on_line)
    if not res.ok:
        out = {"ok": False, "error": "diff build produced no PDF", "id": diff_id}
        if res.error_log:
            out["log"] = res.error_log
        return out

    entry = {
        "id": diff_id, "kind": "diff", "status": "done", "pdf": name,
        "old": o["short"], "new": n["short"],
        "old_subject": o["subject"], "new_subject": n["subject"],
        "old_date": o["date"], "new_date": n["date"],
        "elapsed": res.elapsed, "changes": res.changes, "added": _now_iso(),
        "index_version": INDEX_VERSION,
    }
    kept = _merge_write(cfg, store_dir, entry, retain)
    if on_line:
        on_line(f"[store] added {diff_id} — {kept} kept")
    return {"ok": True, "id": diff_id, "pdf": name,
            "changed_pages": len(res.changes), "kept": kept}


def add_full(cfg: _config.Config, store_dir: str, commit: str,
             retain: int = 50, on_line=None, skip_existing: bool = True) -> dict:
    """Build a no-diff render of `commit` and merge it into the store as the
    current draft (kind "full")."""
    os.makedirs(store_dir, exist_ok=True)
    try:
        core.ensure_ref(cfg.repo_root, commit)
        c = core.commit_info(cfg.repo_root, commit)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"could not resolve {commit}: {exc}"}

    full_id = f"full:{c['short']}"
    if skip_existing and (d := _existing(store_dir, full_id)):
        if on_line:
            on_line(f"[store] {full_id} already built — skipping rebuild")
        return {"ok": True, "id": full_id, "pdf": d["pdf"], "existing": True}

    name = f"full_{_safe(c['short'])}.pdf"
    res = core.build_full(cfg, c["hash"], os.path.join(store_dir, name), on_line=on_line)
    if not res.ok:
        return {"ok": False, "error": "full build produced no PDF", "id": full_id}

    entry = {
        "id": full_id, "kind": "full", "status": "done", "pdf": name,
        "commit": c["short"], "subject": c["subject"], "date": c["date"],
        "elapsed": res.elapsed, "changes": [], "added": _now_iso(),
        "index_version": INDEX_VERSION,
    }
    kept = _merge_write(cfg, store_dir, entry, retain)
    if on_line:
        on_line(f"[store] added {full_id} (full render) — {kept} kept")
    return {"ok": True, "id": full_id, "pdf": name, "kept": kept}
