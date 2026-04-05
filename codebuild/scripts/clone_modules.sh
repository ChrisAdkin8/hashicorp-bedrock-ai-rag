#!/usr/bin/env bash
# clone_modules.sh — Clone Terraform Registry modules discovered by discover_modules.py.
# Reads clone URLs from /codebuild/output/modules.txt written by discover_modules.py.
set -euo pipefail

MODULES_FILE="/codebuild/output/modules.txt"
REPOS_DIR="/codebuild/output/repos"

if [[ ! -f "${MODULES_FILE}" ]]; then
  echo "No modules file found at ${MODULES_FILE} — skipping module clones"
  exit 0
fi

mapfile -t URLS < "${MODULES_FILE}"
echo "Cloning ${#URLS[@]} module repos..."

pids=()
for url in "${URLS[@]}"; do
  [[ -z "${url}" ]] && continue
  name=$(basename "${url}" .git)
  dest="${REPOS_DIR}/module-${name}"

  if [[ -d "${dest}/.git" ]]; then
    echo "Skipping ${name} — already cloned"
    continue
  fi

  git clone --depth 1 --single-branch "${url}" "${dest}" 2>&1 || {
    echo "WARN: Failed to clone ${url} — skipping"
  } &
  pids+=($!)
done

failed=0
for pid in "${pids[@]}"; do
  if ! wait "${pid}"; then
    failed=$((failed + 1))
  fi
done

if [[ "${failed}" -gt 0 ]]; then
  echo "WARN: ${failed} module clone(s) failed — continuing with available modules"
fi

echo "Module clone phase complete."
