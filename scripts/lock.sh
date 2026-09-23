#!/usr/bin/env bash
# Regenerates requirements.txt, the pinned lock file Docker and CI install
# from. We use plain pip, not a fancier resolver, so "lock" means "build a
# clean throwaway venv, install the project, and write down whatever pip
# freeze says exists." Low-tech, but it's honest about what actually gets
# installed.
#
# Run this after touching the dependency list in pyproject.toml. Don't hand-
# edit requirements.txt; it won't survive the next run of this script anyway.
set -euo pipefail

cd "$(dirname "$0")/.."

LOCK_VENV="$(mktemp -d)"
trap 'rm -rf "$LOCK_VENV"' EXIT

python3.14 -m venv "$LOCK_VENV"
"$LOCK_VENV/bin/pip" install --upgrade pip --quiet
"$LOCK_VENV/bin/pip" install . --quiet
# Exclude the project itself: requirements.txt is a dependency lock, not
# a way to install newsbot. Callers install the project separately with
# `pip install -e .` (or `--no-deps .` in Docker) after this file.
"$LOCK_VENV/bin/pip" freeze --exclude-editable | grep -v '^newsbot @' > requirements.txt

echo "Wrote requirements.txt ($(wc -l < requirements.txt | tr -d ' ') packages)."
