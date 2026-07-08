"""latex-diff-viewer: build git-latexdiff PDFs for any LaTeX project.

Public surface:
  * config.load(repo_root, ...) -> Config
  * core.build_diff / core.build_full / core.parse_changes
  * server.main() (local interactive UI)
  * cli.main() (CI entrypoint: build-diff | build-full | pages | serve)
"""

__version__ = "1.0.0"
