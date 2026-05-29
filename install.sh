#!/usr/bin/env bash
set -euo pipefail

REPO_OWNER="t0masGutierrez"
REPO_NAME="canvas"
CANVAS_BRANCH="${CANVAS_BRANCH:-main}"
CANVAS_INSTALL_DIR="${CANVAS_INSTALL_DIR:-$HOME/.local/share/canvas-cli}"
CANVAS_BIN_DIR="${CANVAS_BIN_DIR:-$HOME/.local/bin}"
CANVAS_ARCHIVE_URL="${CANVAS_ARCHIVE_URL:-https://github.com/${REPO_OWNER}/${REPO_NAME}/archive/refs/heads/${CANVAS_BRANCH}.tar.gz}"

log() {
  printf '%s\n' "$*"
}

fail() {
  printf 'canvas install: %s\n' "$*" >&2
  exit 1
}

command_exists() {
  command -v "$1" >/dev/null 2>&1
}

python_is_supported() {
  "$1" - <<'PY' >/dev/null 2>&1
import sys
import venv

raise SystemExit(0 if sys.version_info >= (3, 11) else 1)
PY
}

find_python() {
  if [ -n "${PYTHON:-}" ]; then
    python_is_supported "$PYTHON" && {
      printf '%s\n' "$PYTHON"
      return
    }
    fail "PYTHON is set, but it is not Python 3.11+ with venv support: $PYTHON"
  fi

  for candidate in python3.13 python3.12 python3.11 python3 python; do
    if command_exists "$candidate" && python_is_supported "$candidate"; then
      command -v "$candidate"
      return
    fi
  done

  fail "Python 3.11+ with venv support is required. Install Python from https://www.python.org/downloads/ or Homebrew, then rerun this installer."
}

require_macos_and_chrome() {
  if [ "${CANVAS_SKIP_MACOS_CHECK:-0}" != "1" ] && [ "$(uname -s)" != "Darwin" ]; then
    fail "This CLI currently supports macOS because it reads your local Google Chrome Canvas session."
  fi

  if [ "${CANVAS_SKIP_CHROME_CHECK:-0}" = "1" ]; then
    return
  fi

  if [ -d "/Applications/Google Chrome.app" ] || [ -d "$HOME/Applications/Google Chrome.app" ]; then
    return
  fi

  fail "Google Chrome is required. Install Chrome, log into Canvas there, then rerun this installer."
}

copy_if_exists() {
  source_path="$1"
  destination_path="$2"
  if [ -e "$source_path" ]; then
    cp "$source_path" "$destination_path"
  fi
}

refresh_source() {
  source_dir="$1"
  destination_dir="$2"

  [ -d "$source_dir/src" ] || fail "Downloaded source is missing src/"
  [ -f "$source_dir/pyproject.toml" ] || fail "Downloaded source is missing pyproject.toml"

  mkdir -p "$destination_dir"
  rm -rf "$destination_dir/src"
  cp -R "$source_dir/src" "$destination_dir/src"
  cp "$source_dir/pyproject.toml" "$destination_dir/pyproject.toml"
  copy_if_exists "$source_dir/README.md" "$destination_dir/README.md"
  copy_if_exists "$source_dir/install.sh" "$destination_dir/install.sh"
  copy_if_exists "$source_dir/.gitignore" "$destination_dir/.gitignore"
}

download_source() {
  tmpdir="$(mktemp -d "${TMPDIR:-/tmp}/canvas-install.XXXXXX")"
  archive="$tmpdir/source.tar.gz"
  curl -fsSL "$CANVAS_ARCHIVE_URL" -o "$archive" || fail "Could not download $CANVAS_ARCHIVE_URL"
  tar -xzf "$archive" -C "$tmpdir" || fail "Could not unpack downloaded source"
  find "$tmpdir" -mindepth 1 -maxdepth 1 -type d | head -n 1
}

profile_file_for_shell() {
  shell_name="$(basename "${SHELL:-}")"
  case "$shell_name" in
    zsh) printf '%s\n' "$HOME/.zshrc" ;;
    bash) printf '%s\n' "$HOME/.bash_profile" ;;
    *) printf '%s\n' "$HOME/.profile" ;;
  esac
}

path_contains_bin_dir() {
  case ":$PATH:" in
    *":$CANVAS_BIN_DIR:"*) return 0 ;;
    *) return 1 ;;
  esac
}

ensure_path_setup() {
  PATH_SETUP_MESSAGE=""
  if path_contains_bin_dir; then
    return
  fi

  if [ "${CANVAS_SKIP_PATH_SETUP:-0}" = "1" ]; then
    PATH_SETUP_MESSAGE="Add this to your shell before running canvas: export PATH=\"$CANVAS_BIN_DIR:\$PATH\""
    return
  fi

  profile_file="$(profile_file_for_shell)"
  mkdir -p "$(dirname "$profile_file")"
  touch "$profile_file"
  if ! grep -F "$CANVAS_BIN_DIR" "$profile_file" >/dev/null 2>&1; then
    {
      printf '\n# Added by canvas CLI installer\n'
      printf 'export PATH="%s:$PATH"\n' "$CANVAS_BIN_DIR"
    } >> "$profile_file"
  fi
  PATH_SETUP_MESSAGE="Added $CANVAS_BIN_DIR to $profile_file. Open a new terminal or run: export PATH=\"$CANVAS_BIN_DIR:\$PATH\""
}

write_launcher() {
  venv_canvas="$1"
  launcher="$2"
  mkdir -p "$(dirname "$launcher")"
  {
    printf '#!/usr/bin/env bash\n'
    printf 'exec %q "$@"\n' "$venv_canvas"
  } > "$launcher"
  chmod 755 "$launcher"
}

main() {
  require_macos_and_chrome

  python_bin="$(find_python)"
  source_root="$CANVAS_INSTALL_DIR/source"
  venv_dir="$CANVAS_INSTALL_DIR/.venv"
  launcher="$CANVAS_BIN_DIR/canvas"

  tmpdir=""
  cleanup() {
    if [ -n "$tmpdir" ] && [ -d "$tmpdir" ]; then
      rm -rf "$tmpdir"
    fi
  }
  trap cleanup EXIT

  log "Installing Canvas CLI..."
  log "Using Python: $python_bin"

  if [ -n "${CANVAS_SOURCE_DIR:-}" ]; then
    refresh_source "$CANVAS_SOURCE_DIR" "$source_root"
  else
    downloaded_source="$(download_source)"
    refresh_source "$downloaded_source" "$source_root"
  fi

  "$python_bin" -m venv "$venv_dir"
  "$venv_dir/bin/python" -m pip install --upgrade pip setuptools wheel
  "$venv_dir/bin/python" -m pip install -e "$source_root"

  write_launcher "$venv_dir/bin/canvas" "$launcher"
  ensure_path_setup

  log ""
  log "Canvas CLI installed at $launcher"
  if [ -n "${PATH_SETUP_MESSAGE:-}" ]; then
    log "$PATH_SETUP_MESSAGE"
  fi
  log ""
  log "Next steps:"
  log "  canvas setup https://school.instructure.com"
  log "  canvas courses"
}

main "$@"
