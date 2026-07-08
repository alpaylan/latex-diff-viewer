#!/usr/bin/env python3
"""git-latexdiff --filter hook: record every change for an in-app change index.

git-latexdiff runs this inside the diff checkout with the flattened diff file
as the only argument; it must rewrite that file in place.

We add a small preamble block (just before \\begin{document}) that hooks
latexdiff's own \\DIFaddbegin / \\DIFdelbegin markers (defined with
\\DeclareRobustCommand, so we patch them with etoolbox's \\pretocmd).  At every
change we:

  * write a record to the .aux:  \\difchgmeta{N}{add|del}{abspage}{printedpage}
    -> the server parses these to build a clickable list of changes.
  * drop a PDF bookmark too (harmless bonus for viewers that show an outline).

`abspage` (physical page, via atbegshi) is what the UI uses to jump the embedded
viewer; `printedpage` (\\thepage) is shown as the human label.
"""

import sys

INJECT = r"""
% --- diff-viewer: per-change index data (injected) --------------------------
\makeatletter
\usepackage{etoolbox}
\usepackage{atbegshi}
\providecommand{\difchgmeta}[4]{}% defined as no-op so reading .aux never errors
\newcounter{difchg}
\newcounter{difabspage}
\AtBeginShipout{\stepcounter{difabspage}}
\newcommand{\difchgmark}[1]{%
  % Only act during real typesetting. When a \DIFadd/\DIFdel sits in a moving
  % argument (a \section title written to the .aux/.toc or a running-head mark)
  % \protect is not \@typeset@protect, and expanding our \write/\pdfbookmark
  % there breaks compilation ("Missing \endcsname"). Skipping those contexts also
  % avoids double-counting a heading change.
  \ifx\protect\@typeset@protect
    \ifmmode\else
      \stepcounter{difchg}%
      % record first, so a stray \pdfbookmark error can never drop a change
      \immediate\write\@auxout{%
        \string\difchgmeta{\arabic{difchg}}{#1}%
        {\the\numexpr\value{difabspage}+1\relax}{\thepage}}%
      % \pdfbookmark only exists with hyperref; the change index reads the .aux
      % record above, so the outline entry is a pure bonus when hyperref is there.
      \ifdefined\pdfbookmark
        \pdfbookmark[0]{Change \thedifchg\space(p.\thepage)}{difchg.\arabic{difchg}}%
      \fi
    \fi
  \fi}
% Hook \DIFadd / \DIFdel (which wrap the actual coloured/struck *text*) rather
% than \DIFaddbegin / \DIFdelbegin: the begin-markers also wrap structural,
% non-rendering changes (e.g. an edit inside a \chapter title), which would
% index entries that jump to a page with nothing visibly changed.
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
