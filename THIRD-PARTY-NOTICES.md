# Third-party notices

`latex-diff-viewer` orchestrates several existing programs. **Its own source code
(the `latexdiff_viewer` package, `action.yml`, workflows, `Dockerfile`) does not
include or modify any third-party code** — it invokes these tools as separate
processes at runtime.

For convenience, the published Docker image
(`ghcr.io/alpaylan/latex-diff-viewer`) **bundles** the programs below. Each is
distributed under its own license; each tool's own license/copyright notice is
retained, unmodified, in the image. This file also serves as the written offer
to obtain their source (GPL v2 §3 / GPL v3 §6): the corresponding source is
available, unmodified, from the upstream links below.

| Component | License | Copyright | Source |
|---|---|---|---|
| git-latexdiff | Permissive ("keep this notice") / BSD-3-Clause | © 2012–2017 Matthieu Moy | https://gitlab.com/git-latexdiff/git-latexdiff |
| latexdiff | GNU GPL v3 or later | © 2004–2022 F. J. Tilmann | https://github.com/ftilmann/latexdiff · https://ctan.org/pkg/latexdiff |
| latexmk | GNU GPL v2 or later | © John Collins | https://ctan.org/pkg/latexmk |
| latexpand | Permissive / GPL | © Matthieu Moy | https://gitlab.com/latexpand/latexpand · https://ctan.org/pkg/latexpand |
| TeX Live (pdflatex, etc.) | Mixed (LPPL, GPL, …) | TeX Live contributors | https://tug.org/texlive/ (base image: `texlive/texlive`) |

None of these programs are modified by this project. Their full license texts
ship inside the image (e.g. TeX Live's under `/usr/share/texlive` and package
headers in the scripts themselves) and are available at the sources above.

## git-latexdiff notice (reproduced as required)

> Copyright (c) 2012 - 2017, Matthieu Moy <Matthieu.Moy@grenoble-inp.fr>
> Copying and distribution of git-latexdiff and its testsuite, with or without
> modification, are permitted in any medium without royalty provided the
> copyright notice and this notice are preserved. This file is offered as-is,
> without any warranty.
