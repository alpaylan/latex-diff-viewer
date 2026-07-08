# latex-diff-viewer

Render [`git-latexdiff`](https://gitlab.com/git-latexdiff/git-latexdiff) between
two commits for **any** LaTeX project — as a GitHub Action that comments on your
PRs, a browsable GitHub Pages site, or a local interactive web app. All the TeX
tooling is baked into a prebuilt Docker image, so you only provide your build
instructions.

Added text is blue + underlined, removed text is red + struck through. The viewer
also shows a **changed-pages index** you can click to jump straight to each
change — automatically, for any project (no configuration required).

---

## 1. GitHub Action — PR comments + artifacts

**No config needed for the common case.** If your project has a single LaTeX
document and builds with `latexmk`, just add the workflow below — the tool
**auto-detects the main `.tex`** (the file with `\documentclass` +
`\begin{document}`) and uses `latexmk -pdf`.

Add a `difftool.toml` (see [`difftool.example.toml`](difftool.example.toml)) *only
to override something* — e.g. a non-`latexmk` build, or when several documents make
the main file ambiguous. Anything in it can equally be passed as workflow `with:`
inputs, so the file is never mandatory:

```toml
# every key optional
build_command = "make"        # e.g. if you don't use latexmk
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

You can also **run it by hand**: Actions → *LaTeX diff* → *Run workflow*, and
optionally type the two commits to compare (blank compares the latest commit
against its parent). Manual and push runs have no PR to comment on, so they
report the result on the run's **Summary** page and via the uploaded artifact.

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

### The Pages viewer — two architectures

A repo can have **one** Pages source, so pick one:

- **Store viewer (recommended, unified).** A `latexdiff-store` branch holds the
  diffs + manifest + viewer; **push-seeded recent history and on-demand requests
  both append to it**, so they show in one viewer. Set Settings → Pages → *Deploy
  from a branch* → `latexdiff-store`. Wire it with one file —
  [`examples/consumer.yml`](examples/consumer.yml) — combining:
  - `pr-diff.yml` on **pull_request**: builds base→head, publishes it to the store,
    and comments a **"View the diff ↗"** link (with an artifact fallback for fork
    PRs, which can't push to the store) — so PR diffs render in the viewer, no
    artifact download needed.
  - `store-seed.yml` on **push**: pre-builds the last `pages_recent` commits' diffs.
  - `issue-diff.yml` on **issues**: an issue titled `latexdiff <base>..<head>` builds
    that diff on demand, comments a viewer link, and closes the issue. The viewer's
    **"Request diff"** button opens exactly such an issue (a self-filling cache);
    it shows **"Building…"** if one's already in progress.

  Both use the same `concurrency` group so they never race; the store keeps the
  most recent `retain` (default 50) diffs. (Actions *artifacts* can't power a static
  viewer — they need auth and expire; the branch is what makes diffs publicly
  reachable.)

- **Pre-generated only.** [`pages.yml`](.github/workflows/pages.yml) deploys a fresh
  site via `actions/deploy-pages` (Settings → Pages → *GitHub Actions*). Simpler,
  but no on-demand requests, and it can't coexist with the store viewer.

The **PR action is independent of both** — it only comments + uploads an artifact,
so it works alongside either (or neither).

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
| `build_dir` | *(unset)* | latexmk out_dir — set it only if your project's `latexmkrc` writes the PDF into a subdir, so the diff build can find it. Not needed for the changed-pages index. |
| `output_pdf` | `{build_dir}/{jobname}.pdf` | Where `build_command` leaves the PDF (auto-discovered if it differs). |
| `latexdiff_options` | `[]` | Extra flags for `git latexdiff`. |
| `untracked_assets` | `[]` | Globs of gitignored files to mirror into checkouts (local only). |
| `pages_recent` | `10` | Pages viewer pre-builds a diff for each of the last N commits (vs parent). |
| `pages_pairs` | `[]` | Pin specific `base..compare` diffs for Pages (overrides `pages_recent`). |

A JSON `difftool.json` with the same keys works too (for Python < 3.11 without
`tomllib`).

### The changed-pages index

Works automatically, no config. The diff `--filter` records each change into the
`.aux`; the tool preserves `git-latexdiff`'s work tree (`--no-cleanup` +
`--tmpdirprefix`) and reads those records straight from it, so the clickable
page list is produced for any project.

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

## License

latex-diff-viewer's own code is licensed under the [MIT License](LICENSE).

It orchestrates existing tools without including their code. The published
Docker image additionally **bundles** `git-latexdiff` (permissive/BSD),
`latexdiff` and `latexmk` (GPL), and TeX Live (mixed) — each under its own
license, unmodified. See [THIRD-PARTY-NOTICES.md](THIRD-PARTY-NOTICES.md).
