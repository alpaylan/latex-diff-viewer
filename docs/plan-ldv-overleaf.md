# Plan: `ldv` — personal save/diff/share flow + Overleaf support

> **Status (2026-07-09):** phases 0–5 implemented and verified; gist is the
> only share medium so far (private/public repo media still to do). The
> hosted viewer lives at the demo repo's existing Pages root
> (`https://alpaylan.github.io/latex-diff-viewer-demo/#<gist-id>`) — the
> store viewer and the hosted share viewer are the same index.html.
> Verified against real Overleaf (2026-07-09): the read link serves a grant
> interstitial; the grant POST needs the csrf token **and** the link's
> `#fragment` as `tokenHashPrefix` (link-sharing v2) — implemented, plus a
> grant-flow mock in `tests/mock_overleaf.py` run by CI. Share links render
> in the browser (user-confirmed).
> Deferred: repo share media, `link --git` against a real premium account,
> **auto-retry with opaque tables** (on a failed diff, retry once with
> `PICTUREENV=(?:picture|DIFnomarkup|tabular|table)` and tag the result —
> table markup is the most TeX-version-sensitive breakage; hit on a real
> thesis 2026-07-10, workaround documented in README),
> and **macro pre-expansion**: latexdiff can't mark up preamble changes, so
> redefining a macro (e.g. `\newcommand{\db}{Turso}` → `{Limbo}`) silently
> changes the rendered document with `changed_pages: 0` — hit on a real
> paper 2026-07-09. An opt-in pass expanding user macros before diffing
> would surface those edits; sharp edges (fragile macros, packages), design
> before building.

## Context

Today the tool is coupled to a git repo and centers on CI: workflows build diffs,
a Pages store serves them. This plan adds a *personal* flow for individual writers —
especially Overleaf users — who want:

- `ldv save` — snapshot the project now, no git knowledge required
- `ldv list` — see the timeline of saves / history points
- `ldv diff <a> <b>` — a latexdiff PDF between any two points, optionally shared
  as a link (auth piggybacks on the `gh` CLI)

Sharing separates *viewer* from *data*: the viewer is a static page hosted once
by this project (Pages on the demo repo); a user's share is only data, in one of
three media chosen at first `--share`:

1. **Secret gist (default)** — works on the free plan, not listed on the user's
   profile, unguessable URL. Link = `<hosted-viewer>/#<gist-id>`; the viewer
   fetches `manifest.json` + PDFs from gist raw URLs (CORS `*`, verified) and
   renders via PDF.js. Anyone with the link can read; nobody can find it.
2. **Private repo** — real access control on the free plan (only *Pages* from
   private repos is paid, plain private repos are not). Link = the github.com
   blob URL, which renders the PDF inline behind GitHub auth for invited
   co-authors; tool users can also `ldv view` over a clone.
3. **Public repo + Pages** — self-contained public store (the existing CI
   layout), explicit opt-in for public projects.

Overleaf support is tiered but the command set is identical:

- **No Overleaf account needed** (read-only share link, or a manually downloaded
  zip): saves go into a local *shadow git repo*; diffs are between saves.
- **Overleaf premium** (git bridge): the timeline is the bridge clone; `<a>`/`<b>`
  additionally resolve as dates and Overleaf version labels (labeled versions get
  real commit hashes — documented Overleaf behavior).

Both timelines are plain git repos, so one resolver and one build path serve both.
The project stays **stdlib-only Python**; the heavy deps (TeX Live, latexdiff,
git-latexdiff) remain external, exactly as now.

## CLI surface

New subcommands in the existing `cli.py` argparse tree (`sub.add_parser`, same
JSON-on-stdout / logs-on-stderr convention):

```
ldv save [-m MSG] [--from ZIP|DIR]     snapshot working tree (or an imported zip)
ldv list                               timeline: saves and/or Overleaf history
ldv diff <a> <b> [-o OUT] [--share]    build diff PDF; --share pushes + prints link
ldv view                               serve the local viewer over saves/shares
ldv link <read-url | --git PROJECT>    one-time: connect an Overleaf source
ldv pull                               refresh from the linked source
```

`ldv` is a console-script alias for the same `cli.main` as `latexdiff-viewer`.

**Ref resolution** for `<a>`/`<b>` (one function, tried in order):
1. save id (`s3`, or bare int) → nth save commit in the shadow repo
2. date `YYYY-MM-DD[THH:MM]` → `git rev-list -1 --before=<date> HEAD`
3. anything else → passed to git (Overleaf label commits, `HEAD~2`, short hashes)

## State layout

`$XDG_STATE_HOME/ldv/<project-id>/` (default `~/.local/state/ldv/`);
project-id = short hash of the project folder's realpath, replaced by the
Overleaf project id after `link`.

```
repo/        shadow git repo (saves)  OR  clone of git.overleaf.com (premium)
share/       clone of the share target: the secret gist or the store repo
state.json   {origin: {kind: folder|read-link|git-bridge, url},
              share: {kind: gist|repo, id_or_repo, visibility, link},
              saves: <seq counter>}
out/         built diff PDFs
```

## Module layout

**New: `workspace.py`** — state dir + `state.json` I/O; shadow-repo init;
`save()` (copy tree into `repo/`, excluding `.git`, the config's `build_dir`,
`out/`, editor junk; commit; return seq id); `import_zip()`; `timeline()`;
`resolve_ref()`.

**New: `overleaf.py`** — everything that talks to Overleaf, isolated because the
read-link fetch is *unofficial*: GET the `overleaf.com/read/<token>` URL with a
cookie jar (urllib + http.cookiejar) to mint an anonymous session and learn the
project id, then GET `/project/<id>/download/zip`. Also the premium path:
`link --git` clones `git.overleaf.com/<id>` into `repo/` (token via the standard
git credential helper), `pull()` fetches. Every network failure prints the manual
fallback: “download the zip from Overleaf’s menu, then `ldv save --from <zip>`”.

**New: `share.py`** — thin glue around the existing store machinery, with the
share medium behind one small interface (`ensure_target()`, `push()`, `link()`).
`gh auth status` preflight everywhere.
- *Gist (default):* first share mints a secret gist (`gh gist create` with a
  stub — the gist API is text-only, but gists are git repos, so PDFs go in by
  pushing the gist's git remote with `gh` as credential helper). Each share:
  pull `share/`, `store.add_diff(...)` into it, push, print
  `<hosted-viewer>/#<gist-id>`. Gists have **no directories**: verify the store
  layout is flat (manifest + PDFs at top level) or flatten names for gist mode.
- *Repo modes:* `gh repo create latex-diff-store` (`--private` default for
  blob-link sharing; `--public` + Pages enablement via `gh api` on opt-in),
  `store.add_diff` into `share/<project-id>/`, push (bump `http.postBuffer`
  as the CI flow does), print the blob URL (private) or the Pages deep link
  (public).

**New: hosted viewer** — extend the static viewer (`pages.py` `INDEX_HTML` /
`index.html`) with a *remote-manifest mode*: when `location.hash` names a gist
id, fetch `manifest.json` and PDFs from `gist.githubusercontent.com` raw URLs
(CORS `access-control-allow-origin: *` — verified) into ArrayBuffers for PDF.js.
Publish it on the demo repo's existing Pages. The fragment keeps the gist id
out of server logs.

**Reused unchanged:**
- `core.build_diff` / `core.build_full` — the entire build engine (`core.py:307`),
  including the changed-pages index; the shadow repo is just another `repo_root`
- `core.git` / `list_commits` / `rev_parse` (`core.py:41-77`) — timeline + resolver
- `config.load` + `config.detect_main` (`config.py:190`, `config.py:159`) —
  Overleaf projects have no `difftool.toml`; auto-detect already handles that
- `server.py serve` — already works against the shadow repo for the browser
  viewer (its picker reads local git via `/api/commits`)
- `store.add_diff` / `store.add_full` + the `pages.py` viewer — the share flow
  reuses them verbatim: manifest merge, retention/pruning, idempotency
  (`_existing` skips already-built pairs), PDF.js viewer with deep links.
  Code untouched; the CI flow is unaffected

## Phases

**0. Packaging** — add `pyproject.toml` (stdlib-only, `requires-python >= 3.11`
for tomllib) with entry points `ldv` and `latexdiff-viewer` → `cli.main`.
Dockerfile keeps working as-is (PYTHONPATH); optionally switch to `pip install`.

**1. Save/list** — `workspace.py`, `save`, `list`, `--from` zip import.
No network. Verifiable immediately on `tests/sample-project`.

**2. Diff** — resolver + `diff <a> <b>` building into `out/` via
`core.build_diff`; print the JSON result and the PDF path; `--serve` opens the
existing local viewer.

**3. Share** — `share.py` with the gist medium + the hosted viewer's
remote-manifest mode; then the private/public repo modes. `ldv view` for
local viewing (serve the state dir's `share/`/`out/` with the static viewer).

**4. Overleaf** — `overleaf.py`: `link <read-url>` + `pull` (zip fetch) first;
then `link --git` (premium bridge) with date/label resolution over the clone.

**5. Docs + tests** — README section “Using with Overleaf / personal saves”;
extend `selftest.yml` with a save → edit → save → `diff s1 s2` cycle asserting
`ok` and `changed_pages > 0` (mirrors the existing regression guard); unit-test
manifest/link generation offline — no gist or repo pushes from CI.

## Verification

- After phases 1–2: on this machine (latexmk/latexdiff present), run the cycle
  against a copy of `tests/sample-project`: `save` → edit main.tex → `save` →
  `diff s1 s2` → assert `ok: true`, `changed_pages > 0`, PDF opens.
- Phase 3: one real `--share` per medium against the user's gh account
  (outward-facing — confirm before creating gists/repos): gist link opens in
  the hosted viewer and renders; private-repo blob link renders the PDF on
  github.com; a second `--share` of the same pair is a no-op (idempotency via
  `store._existing`).
- Phase 4: needs a real read link (and, for premium, a git token) from the user;
  verify zip fetch, `pull` idempotency, label → commit, date resolution.

## Risks / notes

- Read-link zip endpoint is unofficial → isolated in `overleaf.py`, manual-zip
  fallback always printed on failure.
- Secret gists are unlisted, **not** access-controlled: anyone with the link
  reads the diff. That is the chosen trade-off for the default; users needing
  real auth pick the private-repo medium. Say both plainly in the docs.
- Gist raw URLs stop serving files over ~10 MB; figure-heavy diff PDFs can
  exceed that. Detect at push time and suggest the repo medium (or clone).
- Gists have a flat namespace — confirm/flatten the store layout for gist mode.
- Pages deploys lag pushes by ~a minute (public-repo medium); print the URL
  with a note rather than polling.
- Viewer-from-subdirectory (repo media): `index.html` fetches `manifest.json`
  relatively so a per-project subdir should just work, but verify in phase 3
  (some viewer features key off `GITHUB_REPOSITORY`-derived paths — check
  `_repo_full()` usage in `store.py` and `REPO_FULL` in `index.html`).
- The hosted viewer is a public page loading user data by fragment id; it must
  never send the fragment anywhere (no analytics, no server round-trips).
- Git bridge collapses changes between pulls (author attribution goes to the
  latest change); time resolution for premium = pull frequency. Document, and
  suggest labels for milestones.
