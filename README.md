# latex-diff-viewer

Render [`git-latexdiff`](https://gitlab.com/git-latexdiff/git-latexdiff) between
any two commits of **any** LaTeX project, and browse the result — on pull requests,
via on-demand requests, and as a published, browsable web viewer. All the TeX
tooling ships in a prebuilt Docker image; you provide only your build instructions
(often none — it auto-detects).

**Live demo:** <https://alpaylan.github.io/latex-diff-viewer-demo/> — pick two
commits, view the rendered diff (added text blue + underlined, removed red + struck
through), and click a changed page to jump straight to it. The
[demo repo](https://github.com/alpaylan/latex-diff-viewer-demo) is a tiny paper with
a curated commit history (text edits, a table/float change, a section add).

![Walkthrough: open a diff, jump to a changed page, view an added section](https://raw.githubusercontent.com/alpaylan/latex-diff-viewer-demo/main/demo.gif)

---

## Quick start (recommended)

Get PR diffs, on-demand diffs, and a browsable viewer — all in one file.

1. **Add the workflow.** Copy [`examples/consumer.yml`](examples/consumer.yml) to
   `.github/workflows/latex-diff.yml`.
2. **(Optional) add `difftool.toml`.** Skip it if your project is a single document
   built with `latexmk` — the tool auto-detects the main `.tex` (the file with
   `\documentclass` + `\begin{document}`). Otherwise:
   ```toml
   main = "paper.tex"
   build_command = "make"      # only if you don't use latexmk
   ```
3. **Turn on Pages.** After the first run creates the `latexdiff-store` branch, set
   **Settings → Pages → Deploy from a branch → `latexdiff-store` / (root)**.

That's it. Now:

| You do… | You get… |
|---|---|
| Open a **PR** | the base→head diff **rendered in the viewer**, linked from a PR comment |
| **Push** to `main` | recent commits' diffs + a **current-draft** full render, seeded into the viewer |
| Open an **issue** `latexdiff <base>..<head>` | that diff built **on demand**, with a viewer link |

The viewer (the `latexdiff-store` branch, served by Pages) shows all of them in one
place, with a clickable **changed-pages index** — automatically, no config.

---

## What it looks like

**PR comment** (the diff renders in the viewer; the artifact is just a fork-PR fallback):

> ### 📄 LaTeX diff
> ✅ Built `a1b2c3d..e4f5a6b` — **2 changed page(s)**.
>
> **[View the diff ↗](https://you.github.io/repo/?diff=a1b2c3d..e4f5a6b)**  ·  or download the **latex-diff** artifact from the run.

**Issue request** — a single comment that transitions in place:

> 🔧 Building diff `v1..HEAD`… Follow along in the [workflow run ▸](#).

…then, when it's done (and the issue auto-closes):

> 📄 Built `v1..HEAD` — **2 changed page(s)**. View it here: `https://you.github.io/repo/?diff=v1..HEAD`

**Viewer on mobile** — full-screen PDF (rendered with PDF.js so it works on iOS),
with slide-in drawers:

```
┌──────────────────────────┐
│ ☰ Diffs        Changes(2) │
├──────────────────────────┤
│                          │
│     diff PDF (canvas)    │
│     pinch · scroll       │
│                          │
│                [−][⤢][+] │
└──────────────────────────┘
  ☰ Diffs   → picker + diffs/renders drawer
  Changes(2) → drawer; tap a page to jump
```

---

## How it works

```
   PR ──────────┐   push ─────────┐   issue "latexdiff a..b" ──┐
                ▼                  ▼                            ▼
          pr-diff.yml        store-seed.yml               issue-diff.yml
                └──────── store-add / store-seed ──────────────┘
                                  │  (append, idempotent, one concurrency group)
                                  ▼
                 latexdiff-store branch  =  manifest.json + PDFs + viewer
                                  │  (served by GitHub Pages, branch source)
                                  ▼
                  browsable viewer  ·  ?diff=a..b deep links
```

Everything appends to one store branch via the same idempotent `store-add`, so PR,
push, and on-demand diffs coexist in a single viewer. The changed-pages index is
**float-aware**: each change's page is recorded at *shipout* (via `zref-abspage`),
so a changed table or figure points to the page it actually lands on — not the
source line. (GitHub *artifacts* can't power a static viewer — they need auth and
expire — which is why the store lives in a branch.)

---

## Configuration (`difftool.toml`)

All keys optional; a JSON `difftool.json` with the same keys also works.

| key | default | meaning |
|---|---|---|
| `main` | auto-detected, else `main.tex` | Main LaTeX file. |
| `build_command` | `latexmk -pdf -f -interaction=nonstopmode {main}` | Your full build. `{main}`/`{build_dir}`/`{jobname}` are substituted. |
| `build_dir` | *(unset)* | latexmk out_dir — set only if your `latexmkrc` writes the PDF into a subdir. |
| `output_pdf` | `{build_dir}/{jobname}.pdf` | Where `build_command` leaves the PDF (auto-discovered if it differs). |
| `latexdiff_options` | `[]` | Extra flags for `git latexdiff` (e.g. `--add-to-config=VERBATIMLINEENV=code`). |
| `untracked_assets` | `[]` | Globs of gitignored files to mirror into checkouts (e.g. generated figures). |
| `pages_recent` | `10` | How many recent commits `store-seed` pre-builds on push. |
| `pages_pairs` | `[]` | Pin specific `base..compare` diffs (overrides `pages_recent`). |

---

## Local interactive viewer (no CI)

Point it at any repo — nothing to copy in:

```bash
git clone https://github.com/alpaylan/latex-diff-viewer && cd latex-diff-viewer
PYTHONPATH=src python3 -m latexdiff_viewer.server --repo /path/to/your/paper
# -> http://127.0.0.1:8765   (pick Base/Compare, Generate diff)
```

Or from the Docker image, against the current directory:

```bash
docker run --rm -it -p 8765:8765 -v "$PWD:/repo" -w /repo \
  ghcr.io/alpaylan/latex-diff-viewer:v1 serve --host 0.0.0.0
```

Needs `python3`, `git-latexdiff`, `latexmk`, and a LaTeX engine on `PATH` — or just
use the Docker image, which has them.

---

## Personal saves & Overleaf (`ldv`)

No git, no CI: snapshot a folder whenever you like, diff any two snapshots,
share the result as a link. Works for Overleaf projects on any plan.

```bash
pip install git+https://github.com/alpaylan/latex-diff-viewer  # gives you `ldv`
ldv doctor        # checks what's installed and which features are ready

cd my-paper/
ldv save -m "before the rewrite"     # snapshot -> s1, s2, … (stored under ~/.local/state/ldv)
ldv list                             # the timeline
ldv diff s1 s3                       # latexdiff PDF between two saves
ldv diff s1                          # …or against the latest save
ldv diff 2026-07-01 2026-07-09       # …or by date (last save at/before each)
ldv view                             # browse saves in the local web viewer
```

**Sharing.** `ldv diff s1 s3 --share` publishes the diff to a per-project
*secret gist* (needs the [GitHub CLI](https://cli.github.com), `gh auth login`)
and prints a viewer link you can send to co-authors — no install needed on
their side. Secret gists are unlisted and off your profile, but **anyone with
the link can read the diff** — don't share embargoed work this way.

**Overleaf.**
- Any plan: `ldv link "<read-only share link>"` — run it from anywhere; no
  local folder is involved. The project registers under its Overleaf title
  (`--name` to override; `ldv projects` lists them), and `ldv pull` /
  `ldv diff` find it from any directory — `--project <name>` picks one when
  you have several. Each pull that changed something becomes a save. (This
  uses the same endpoints your browser does; if Overleaf changes them,
  download the zip via Menu → Download → Source and run
  `ldv save --from project.zip`.)
- Premium (git bridge): `ldv link --git <project-id>` clones the project's
  git history as the timeline; `ldv pull` updates it. Dates and Overleaf
  version labels work as `ldv diff` points; label milestones in Overleaf's
  history to pin them.

Building diffs locally still needs the TeX toolchain (`git-latexdiff`,
`latexmk`); `save`/`list`/`pull`/`--share` of an existing diff do not.

---

## Alternatives

- **Artifact only (no Pages).** Use the composite action directly for a PR comment +
  a downloadable PDF, without the store viewer:
  ```yaml
  - uses: alpaylan/latex-diff-viewer@v1
    with: { config: difftool.toml }
  ```
  Inputs: `config`, `main`/`build_command`/`build_dir`/`latexdiff_options`/`assets`
  (overrides), `base`/`head`, `full`, `comment`, `artifact_name`, `image`.
  Outputs: `diff_pdf`, `full_pdf`, `changed_pages`.
- **Pre-generated Pages.** [`pages.yml`](.github/workflows/pages.yml) deploys a fresh
  site via `actions/deploy-pages` (Pages source: *GitHub Actions*). Simpler, but no
  on-demand requests, and it can't coexist with the store viewer (one Pages source
  per repo).

---

## CLI

The Docker image's entrypoint (also `python3 -m latexdiff_viewer.cli`):

```bash
latex-diff-viewer build-diff  --old A --new B -o out/diff.pdf   # JSON + changed pages
latex-diff-viewer build-full  --commit HEAD   -o out/full.pdf
latex-diff-viewer store-add   --old A --new B --store site      # append a diff to a store
latex-diff-viewer store-seed  --store site                      # append recent diffs + a full render
latex-diff-viewer serve       --port 8765                       # local interactive UI
```

Personal-flow subcommands (`ldv` is the same CLI): `save`, `list`,
`diff <a> [<b>] [--share]`, `view`, `link`, `pull` — see
[Personal saves & Overleaf](#personal-saves--overleaf-ldv).

## Development

```bash
docker build -t ldv:test .
```

`selftest.yml` builds the image and exercises the CLI against a two-commit sample in
`tests/sample-project/`; `docker-publish.yml` pushes the image to GHCR on a `v*` tag.

## License

This project's own code is [MIT](LICENSE). The Docker image bundles `git-latexdiff`
(permissive/BSD), `latexdiff` and `latexmk` (GPL), and TeX Live (mixed), each under
its own license, unmodified — see [THIRD-PARTY-NOTICES.md](THIRD-PARTY-NOTICES.md).
