#!/usr/bin/env python3
"""git-latexdiff --filter hook: record every change for an in-app change index.

git-latexdiff runs this inside the diff checkout with the flattened diff file
as the only argument; it must rewrite that file in place.

We inject a preamble block (just before \\begin{document}) that hooks latexdiff's
\\DIFadd / \\DIFdel (the commands wrapping the actually-rendered changed text). At
every change we:

  * write a type record to the .aux:  \\difchgmeta{N}{add|del}
  * drop a zref-abspage label:        difchgN  (page + abspage)
  * drop a PDF bookmark (bonus when hyperref is present).

The page is taken from the zref label, which is resolved at *shipout* — so content
that floats (tables, figures, landscape/rotated pages) reports the page it actually
lands on, not the source location where the markup was processed. `parse_changes`
in core.py reads the type record + the zref page to build the clickable index.
"""

import sys

INJECT = r"""
% --- diff-viewer: per-change index data (injected) --------------------------
\makeatletter
\usepackage{etoolbox}
\usepackage[abspage]{zref}
\providecommand{\difchgmeta}[2]{}% no-op so reading the .aux never errors
\newcounter{difchg}
\newcommand{\difchgmark}[1]{%
  % Only act during real typesetting. In a moving argument (a \section title
  % written to the .aux/.toc or a running-head mark) \protect is not
  % \@typeset@protect, and expanding our \write there breaks compilation; skipping
  % those contexts also avoids double-counting a heading change.
  \ifx\protect\@typeset@protect
    \ifmmode\else
      \stepcounter{difchg}%
      \immediate\write\@auxout{\string\difchgmeta{\arabic{difchg}}{#1}}%
      % zref records page + abspage at SHIPOUT, so floated content (tables/figures/
      % landscape) reports the page it actually lands on, not the source location.
      \zref@labelbyprops{difchg\arabic{difchg}}{page,abspage}%
      \ifdefined\pdfbookmark
        \pdfbookmark[0]{Change \thedifchg}{difchg.\arabic{difchg}}%
      \fi
    \fi
  \fi}
% hyperref builds the PDF-outline string for every section title by *expanding* it
% through \pdfstringdef. A changed heading (latexdiff emits \section{\DIFadd{...}})
% would drag our non-expandable \difchgmark (\write/\stepcounter/zref) into that
% expansion and fail with "Missing \endcsname". Make the mark a no-op there — it
% still runs during real typesetting, so the change index is unaffected.
\ifdefined\pdfstringdefDisableCommands
  \pdfstringdefDisableCommands{\def\difchgmark#1{}}%
\fi
% Hook \DIFadd / \DIFdel (which wrap the actual coloured/struck *text*) rather
% than \DIFaddbegin / \DIFdelbegin: the begin-markers also wrap structural,
% non-rendering changes (e.g. an edit inside a \chapter title).
\AtBeginDocument{%
  \ifdef{\DIFadd}{\pretocmd{\DIFadd}{\difchgmark{add}}{}{}}{}%
  \ifdef{\DIFdel}{\pretocmd{\DIFdel}{\difchgmark{del}}{}{}}{}%
}
\makeatother
% --- end diff-viewer block --------------------------------------------------
"""


def main() -> int:
    if len(sys.argv) < 2:
        return 0
    path = sys.argv[1]
    with open(path, encoding="utf-8", errors="surrogateescape") as fh:
        text = fh.read()

    if "difchgmark" in text:           # already injected
        return 0
    marker = r"\begin{document}"
    idx = text.find(marker)
    if idx == -1:                      # no preamble boundary found; leave as-is
        return 0
    text = text[:idx] + INJECT + "\n" + text[idx:]

    with open(path, "w", encoding="utf-8", errors="surrogateescape") as fh:
        fh.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
