"""Resolve a LaTeX project's build instructions into a single Config object.

Precedence (low -> high):
    built-in defaults  <  difftool.toml / difftool.json  <  explicit overrides
where "overrides" are the CLI flags / Action inputs the caller passes in.

The whole point of this module is that *nothing* downstream (core, server, cli)
hardcodes "main.tex" / "build/main.pdf" / "figures/**/*.pdf" any more: every one
of those lives here as a default a project can override.
"""

from __future__ import annotations

import json
import os
import shlex
from dataclasses import dataclass, field

try:                       # stdlib since 3.11; present in our Docker image.
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - only on <3.11 without a .toml
    tomllib = None  # type: ignore[assignment]

# Config file names looked up in the repo root, in order.
CONFIG_NAMES = ("difftool.toml", "difftool.json", ".difftool.toml", ".difftool.json")

# Every value a project may set, with its default. `None` means "derive it".
DEFAULTS: dict = {
    "main": "main.tex",
    "build_dir": "",                 # latexmk out_dir; "" = latexmk writes to CWD.
    "jobname": None,                 # derived from basename(main) without extension
    "output_pdf": None,              # derived: {build_dir}/{jobname}.pdf (or {jobname}.pdf)
    "build_command": "latexmk -pdf -f -interaction=nonstopmode {main}",
    "latexdiff_options": [],         # extra flags handed to `git latexdiff`
    "untracked_assets": [],          # globs copied into checkouts (e.g. gitignored figures)
    "pages_pairs": [],               # [[base, compare], ...] diffs pre-built for the Pages site
    "pages_recent": 10,              # if pages_pairs is empty, pre-build the last N commits vs parent
}


@dataclass
class Config:
    repo_root: str
    main: str
    build_dir: str
    jobname: str
    output_pdf: str                  # relative to repo_root
    build_command: list[str]         # already tokenised, placeholders expanded
    latexdiff_options: list[str]
    untracked_assets: list[str]
    pages_pairs: list[list[str]]
    pages_recent: int = 10
    source: str | None = None        # path of the config file used, if any

    # --- derived paths -----------------------------------------------------
    @property
    def build_path(self) -> str:
        """Absolute build directory (== repo root when build_dir is unset)."""
        return os.path.join(self.repo_root, self.build_dir) if self.build_dir \
            else self.repo_root

    @property
    def aux_path(self) -> str:
        """Absolute path of the .aux latexmk writes for the diff build."""
        return os.path.join(self.build_path, f"{self.jobname}.aux")

    @property
    def output_pdf_abs(self) -> str:
        return os.path.join(self.repo_root, self.output_pdf)

    def as_dict(self) -> dict:
        return {
            "repo_root": self.repo_root, "main": self.main,
            "build_dir": self.build_dir, "jobname": self.jobname,
            "output_pdf": self.output_pdf, "build_command": self.build_command,
            "latexdiff_options": self.latexdiff_options,
            "untracked_assets": self.untracked_assets,
            "pages_pairs": self.pages_pairs, "pages_recent": self.pages_recent,
            "source": self.source,
        }


# ---------------------------------------------------------------------------
# Parsing helpers (config-file values may be strings *or* lists; CLI/env are strings)
# ---------------------------------------------------------------------------

def _as_command(value) -> list[str] | None:
    if value is None:
        return None
    if isinstance(value, list):
        return [str(v) for v in value]
    return shlex.split(str(value))          # "latexmk -pdf main.tex" -> tokens


def _as_flags(value) -> list[str] | None:
    """latexdiff_options: a list, or a shell-ish string."""
    if value is None:
        return None
    if isinstance(value, list):
        return [str(v) for v in value]
    return shlex.split(str(value))


def _as_globs(value) -> list[str] | None:
    """untracked_assets: a list, or a comma/newline-separated string."""
    if value is None:
        return None
    if isinstance(value, list):
        return [str(v) for v in value]
    parts = [p.strip() for chunk in str(value).splitlines() for p in chunk.split(",")]
    return [p for p in parts if p]


def _as_pairs(value) -> list[list[str]] | None:
    """pages_pairs: [["base","head"], ...] or "base..head, other..HEAD"."""
    if value is None:
        return None
    if isinstance(value, list):
        out = []
        for item in value:
            if isinstance(item, (list, tuple)) and len(item) == 2:
                out.append([str(item[0]), str(item[1])])
            elif isinstance(item, str) and ".." in item:
                a, b = item.split("..", 1)
                out.append([a.strip(), b.strip()])
        return out
    out = []
    for chunk in str(value).replace("\n", ",").split(","):
        chunk = chunk.strip()
        if ".." in chunk:
            a, b = chunk.split("..", 1)
            out.append([a.strip(), b.strip()])
    return out


def _expand(tokens: list[str], mapping: dict[str, str]) -> list[str]:
    """Substitute {main}/{build_dir}/{jobname} placeholders in each token."""
    out = []
    for t in tokens:
        for k, v in mapping.items():
            t = t.replace("{" + k + "}", v)
        out.append(t)
    return out


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------

def find_config(repo_root: str) -> str | None:
    for name in CONFIG_NAMES:
        p = os.path.join(repo_root, name)
        if os.path.isfile(p):
            return p
    return None


def read_config_file(path: str) -> dict:
    with open(path, "rb") as fh:
        raw = fh.read()
    if path.endswith(".toml"):
        if tomllib is None:
            raise RuntimeError(
                f"{path}: TOML config needs Python 3.11+ (tomllib). "
                "Use a difftool.json instead, or run a newer Python.")
        return tomllib.loads(raw.decode("utf-8"))
    return json.loads(raw.decode("utf-8"))


def load(repo_root: str, config_path: str | None = None,
         overrides: dict | None = None) -> Config:
    """Build the resolved Config for `repo_root`.

    `config_path` forces a specific file; otherwise the first of CONFIG_NAMES
    found in `repo_root` is used (a missing file is fine — defaults apply).
    `overrides` (CLI flags / Action inputs) win over the file; `None` values in
    it are ignored so an unset flag never clobbers a configured value.
    """
    repo_root = os.path.abspath(repo_root)
    values = dict(DEFAULTS)

    source = config_path or find_config(repo_root)
    if source and not os.path.isabs(source):
        source = os.path.join(repo_root, source)
    if source and not os.path.isfile(source):
        # An explicitly-requested file that isn't there: fall back to auto-discovery
        # (or defaults) rather than erroring — handy for `--config difftool.toml`
        # passed unconditionally by the Action.
        source = find_config(repo_root)
    if source:
        file_values = read_config_file(source)
        for k, v in file_values.items():
            if k in values:
                values[k] = v

    for k, v in (overrides or {}).items():
        if k in values and v is not None:
            values[k] = v

    # Normalise the shape of each field.
    main = str(values["main"])
    build_dir = str(values["build_dir"] or "")
    jobname = values["jobname"] or os.path.splitext(os.path.basename(main))[0]
    jobname = str(jobname)
    if values["output_pdf"]:
        output_pdf = values["output_pdf"]
    elif build_dir:
        output_pdf = os.path.join(build_dir, f"{jobname}.pdf")
    else:
        output_pdf = f"{jobname}.pdf"

    mapping = {"main": main, "build_dir": build_dir, "jobname": jobname}
    build_command = _expand(
        _as_command(values["build_command"]) or _as_command(DEFAULTS["build_command"]),
        mapping)
    latexdiff_options = _expand(_as_flags(values["latexdiff_options"]) or [], mapping)
    untracked_assets = _as_globs(values["untracked_assets"]) or []
    pages_pairs = _as_pairs(values["pages_pairs"]) or []
    try:
        pages_recent = int(values["pages_recent"])
    except (TypeError, ValueError):
        pages_recent = DEFAULTS["pages_recent"]

    return Config(
        repo_root=repo_root, main=main, build_dir=build_dir, jobname=jobname,
        output_pdf=str(output_pdf), build_command=build_command,
        latexdiff_options=latexdiff_options, untracked_assets=untracked_assets,
        pages_pairs=pages_pairs, pages_recent=pages_recent, source=source,
    )


# GitHub Actions exposes each input `foo` as env INPUT_FOO. Map the ones we take
# so cli.py can pull overrides straight from the environment when run in a step.
_ENV_KEYS = ("main", "build_dir", "jobname", "output_pdf", "build_command",
             "latexdiff_options", "untracked_assets", "pages_pairs", "pages_recent")


def overrides_from_env(env: dict | None = None) -> dict:
    env = env if env is not None else os.environ
    out: dict = {}
    for key in _ENV_KEYS:
        val = env.get("INPUT_" + key.upper())
        if val is not None and val != "":
            out[key] = val
    return out
