#!/usr/bin/env bash
#
# Exocortex one-line installer.
#
# Usage:
#   curl -LsSf https://raw.githubusercontent.com/xD4O/exocortex/main/install.sh | bash
#   curl -LsSf .../install.sh | bash -s -- --dir ~/exocortex --branch main
#
# What it does:
#   1. Verifies prerequisites (git, curl).
#   2. Installs `uv` if it isn't already on PATH.
#   3. Clones the repo to ${INSTALL_DIR:-$HOME/exocortex}.
#   4. Runs `uv sync` to materialize the venv + dependencies.
#   5. Prints the next-step commands.
#
# It does NOT modify your shell profile, mutate global tools, or start the
# daemon for you.

set -euo pipefail

REPO_URL="${EXOCORTEX_REPO_URL:-https://github.com/xD4O/exocortex.git}"
BRANCH="main"
INSTALL_DIR="${HOME}/exocortex"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dir) INSTALL_DIR="$2"; shift 2 ;;
    --branch) BRANCH="$2"; shift 2 ;;
    --repo) REPO_URL="$2"; shift 2 ;;
    -h|--help)
      sed -n '2,20p' "$0"
      exit 0
      ;;
    *) echo "unknown flag: $1" >&2; exit 2 ;;
  esac
done

note()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn()  { printf '\033[1;33m!!\033[0m %s\n' "$*" >&2; }
fatal() { printf '\033[1;31mxx\033[0m %s\n' "$*" >&2; exit 1; }

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || fatal "missing prerequisite: $1"
}

note "Exocortex installer"
note "  repo:  ${REPO_URL}"
note "  ref:   ${BRANCH}"
note "  dest:  ${INSTALL_DIR}"

require_cmd git
require_cmd curl

if ! command -v uv >/dev/null 2>&1; then
  note "uv not found — installing from astral.sh"
  curl -LsSf https://astral.sh/uv/install.sh | sh
  # The uv installer drops uv at ~/.local/bin or ~/.cargo/bin depending on
  # platform; expose it for the rest of this script without touching your
  # shell profile.
  export PATH="${HOME}/.local/bin:${HOME}/.cargo/bin:${PATH}"
  command -v uv >/dev/null 2>&1 || fatal "uv install completed but \`uv\` is not on PATH; open a new shell and re-run, or add ~/.local/bin to your PATH"
else
  note "uv already on PATH ($(uv --version))"
fi

if [[ -d "${INSTALL_DIR}" ]]; then
  if [[ -d "${INSTALL_DIR}/.git" ]]; then
    note "directory exists and is a git repo — pulling latest from ${BRANCH}"
    git -C "${INSTALL_DIR}" fetch origin "${BRANCH}"
    git -C "${INSTALL_DIR}" checkout "${BRANCH}"
    git -C "${INSTALL_DIR}" pull --ff-only origin "${BRANCH}"
  else
    fatal "destination ${INSTALL_DIR} exists and is not a git checkout — refusing to overwrite. Pass --dir to install elsewhere."
  fi
else
  note "cloning into ${INSTALL_DIR}"
  git clone --branch "${BRANCH}" "${REPO_URL}" "${INSTALL_DIR}"
fi

note "running 'uv sync' (this materializes .venv + deps)"
( cd "${INSTALL_DIR}" && uv sync )

cat <<EOF

\033[1;32mDone.\033[0m Exocortex is installed at ${INSTALL_DIR}.

Next steps:
  cd ${INSTALL_DIR}
  uv run precog --help
  uv run precog daemon start        # web UI on http://127.0.0.1:8756

Wire an agent (Claude Code / Hermes / Codex) to the MCP server: see
${INSTALL_DIR}/docs/auto-recall.md.

EOF
