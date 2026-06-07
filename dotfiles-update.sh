#!/usr/bin/env bash
# Update your dotfiles from GitHub

set -euo pipefail

DOTFILES_DIR="$HOME/dotfiles"

if [[ ! -d "$DOTFILES_DIR" ]]; then
  echo "Error: $DOTFILES_DIR does not exist"
  echo "Clone it first: git clone git@github.com:ledutheo/dotfiles.git ~/github/dotfiles"
  exit 1
fi

cd "$DOTFILES_DIR"

echo "==> Pulling latest changes..."
git pull --rebase

echo "==> Re-running installer to update symlinks..."
./install.sh

echo "==> Dotfiles updated successfully."
