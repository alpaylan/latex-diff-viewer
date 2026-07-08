# Full TeX Live so downstream projects compile exactly as they do locally.
FROM texlive/texlive:latest

# git: history + git-latexdiff temp checkouts.  python3: the tool itself.
RUN apt-get update \
 && apt-get install -y --no-install-recommends git python3 ca-certificates \
 && rm -rf /var/lib/apt/lists/*

# Fail the build early if the TeX Live image is missing anything we rely on.
RUN command -v git-latexdiff && command -v latexmk \
 && command -v latexpand && command -v latexdiff && command -v pdflatex \
 && command -v python3

# git-latexdiff needs an identity; trust the CI workspace it mounts.
RUN git config --global user.email "bot@latex-diff-viewer" \
 && git config --global user.name  "latex-diff-viewer" \
 && git config --global --add safe.directory '*'

# Install the tool. Copying just src/ keeps the image layer small + cache-friendly.
COPY src/ /opt/latex-diff-viewer/src/
ENV PYTHONPATH=/opt/latex-diff-viewer/src
ENV PYTHONUNBUFFERED=1

# Default working dir is the mounted repo (GitHub sets /github/workspace).
WORKDIR /github/workspace

ENTRYPOINT ["python3", "-m", "latexdiff_viewer.cli"]
CMD ["--help"]
