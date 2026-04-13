#!/bin/sh
set -e

REPO="https://raw.githubusercontent.com/kokoima/claude-swap/main"
INSTALL_DIR="${HOME}/.local/bin"

echo "Installing claude-swap..."
echo ""

mkdir -p "$INSTALL_DIR"
curl -fsSL "$REPO/claude-swap" -o "$INSTALL_DIR/claude-swap"
chmod +x "$INSTALL_DIR/claude-swap"

# Check PATH
case ":$PATH:" in
  *":$INSTALL_DIR:"*)
    ;;
  *)
    echo "Note: $INSTALL_DIR is not in your PATH."
    echo ""
    echo "Add this to your shell profile (~/.zshrc, ~/.bashrc, etc.):"
    echo "  export PATH=\"\$HOME/.local/bin:\$PATH\""
    echo ""
    ;;
esac

echo "Installed to $INSTALL_DIR/claude-swap"
echo ""
echo "Run 'claude-swap init' to set up your accounts."
