# latex-diff-viewer

Render [`git-latexdiff`](https://gitlab.com/git-latexdiff/git-latexdiff) between
two commits for **any** LaTeX project — as a GitHub Action that comments on your
PRs, a browsable GitHub Pages site, or a local interactive web app. All the TeX
tooling is baked into a prebuilt Docker image, so you only provide your build
instructions.

Added text is blue + underlined, removed text is red + struck through. When your
project builds into a `build_dir`, the viewer also shows a **changed-pages index**
you can click to jump straight to each change.

---

## 1. GitHub Action — PR comments + artifacts

Add a `difftool.toml` to your repo root (see [`difftool.example.toml`](difftool.example.toml)):

```toml
main = "main.tex"
build_command = "latexmk -pdf -f -interaction=nonstopmode main.tex"
```

Then `.github/workflows/latex-diff.yml`:

```yaml
name: LaTeX diff
on:
  pull_request:
  push:
    branches: [main]

jobs:
  diff:
    if: github.event_name == 'pull_request'
    runs-on: ubuntu-latest
    permissions:
      contents: read
      pull-requests: write      # to post the comment
    steps:
      - uses: actions/checkout@v4
        with: { fetch-depth: 0 }   # git-latexdiff needs both commits
      - uses: alpaylan/latex-diff-viewer@v1
        with:
          config: difftool.toml

  pages:
    if: github.event_name == 'push'
    uses: alpaylan/latex-diff-viewer/.github/workflows/pages.yml@v1
    permissions:
      contents: read
      pages: write
      id-token: write
```

On every PR you get a comment with the changed-page count plus a **downloadable
artifact** containing the diff PDF and a full PDF of the head commit. On every
push to `main` the browsable viewer is published to GitHub Pages (enable
**Settings → Pages → Source: GitHub Actions** once).

A ready-to-copy version lives in [`examples/consumer.yml`](examples/consumer.yml).

### Action inputs

| input | default | meaning |
|-------|---------|---------|
| `config` | `difftool.toml` | Path to the config file. |
| `main` / `build_command` / `build_dir` / `latexdiff_options` / `assets` | — | Override the corresponding config key. |
| `base` / `head` | PR base / head | Commits to diff. |
| `full` | `true` | Also build a full (no-diff) PDF of `head`. |
| `comment` | `true` | Post/update the PR comment. |
| `artifact_name` | `latex-diff` | Uploaded artifact name. |
| `image` | `ghcr.io/alpaylan/latex-diff-viewer:v1` | Prebuilt tool image. |

Outputs: `diff_pdf`, `full_pdf`, `changed_pages`.

---

## 2. Local interactive viewer

Point the tool at any LaTeX repo — no need to copy anything into it:

```bash
git clone https://github.com/alpaylan/latex-diff-viewer
cd latex-diff-viewer
PYTHONPATH=src python3 -m latexdiff_viewer.server --repo /path/to/your/paper
# -> http://127.0.0.1:8765
```

Pick a **Base** and **Compare** commit and hit **Generate diff**; a **Current
draft** (full PDF of `HEAD`) is auto-built and pinned. Needs `python3`,
`git-latexdiff`, `latexmk` and a LaTeX engine on `PATH` (or just use the Docker
image below).

Or run it from the Docker image against the current directory:

```bash
docker run --rm -it -p 8765:8765 -v "$PWD:/repo" -w /repo \
  ghcr.io/alpaylan/latex-diff-viewer:v1 serve --host 0.0.0.0
```

---

## 3. Configuration (`difftool.toml`)

| key | default | meaning |
|-----|---------|---------|
| `main` | `main.tex` | Main LaTeX file. |
| `build_command` | `latexmk -pdf -f -interaction=nonstopmode {main}` | Your project's own full build. `{main}`, `{build_dir}`, `{jobname}` are substituted. |
| `build_dir` | *(unset)* | latexmk out_dir. **Set it (with a matching `latexmkrc $out_dir`) to enable the changed-pages index.** |
| `output_pdf` | `{build_dir}/{jobname}.pdf` | Where `build_command` leaves the PDF (auto-discovered if it differs). |
| `latexdiff_options` | `[]` | Extra flags for `git latexdiff`. |
| `untracked_assets` | `[]` | Globs of gitignored files to mirror into checkouts (local only). |
| `pages_pairs` | `[]` | `base..compare` diffs to pre-build for Pages. |

A JSON `difftool.json` with the same keys works too (for Python < 3.11 without
`tomllib`).

### The changed-pages index

The index is powered by records the diff `--filter` writes into the `.aux`. That
file only survives `git-latexdiff`'s cleanup when your project builds into a
`build_dir` (so the tool can symlink it back). Set `build_dir` + a `latexmkrc`
with `$out_dir` to get it; otherwise the diff PDF is still produced, just without
the clickable page list.

---

## 4. CLI

The Docker image's entrypoint (also runnable via `python3 -m latexdiff_viewer.cli`):

```bash
latex-diff-viewer build-diff --old A --new B -o out/diff.pdf   # prints JSON + changes
latex-diff-viewer build-full --commit HEAD  -o out/full.pdf
latex-diff-viewer pages      -o site                            # static viewer
latex-diff-viewer serve      --port 8765                        # interactive UI
```

---

## Development

```bash
docker build -t ldv:test .          # build the image
```

CI (`.github/workflows/selftest.yml`) builds the image and exercises
`build-diff` / `build-full` / `pages` against a tiny two-commit sample project in
`tests/sample-project/`. `docker-publish.yml` pushes the image to GHCR on a `v*`
tag.
