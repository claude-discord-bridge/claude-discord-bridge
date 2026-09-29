#!/usr/bin/env bash
# Install the dicobot control command by symlinking it into ~/.local/bin.
# A symlink (not a copy) means `git pull` updates the command automatically.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SRC="$REPO_DIR/deploy/dicobot"
BIN_DIR="$HOME/.local/bin"
DEST="$BIN_DIR/dicobot"

[[ -f "$SRC" ]] || { echo "not found: $SRC" >&2; exit 1; }

mkdir -p "$BIN_DIR"
chmod +x "$SRC"

if [[ -e "$DEST" && ! -L "$DEST" ]]; then
  mv "$DEST" "$DEST.bak.$(date +%s)"
  echo "기존 파일을 백업했습니다: $DEST.bak.*"
fi

ln -sfn "$SRC" "$DEST"
echo "설치됨: $DEST -> $SRC"

case ":$PATH:" in
  *":$BIN_DIR:"*) ;;
  *)
    echo
    echo "⚠️  $BIN_DIR 이 PATH에 없습니다. 셸 설정에 추가하세요:"
    echo "    echo 'export PATH=\"\$HOME/.local/bin:\$PATH\"' >> ~/.zshrc"
    ;;
esac

echo
echo "확인: dicobot help"
