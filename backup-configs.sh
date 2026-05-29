#!/usr/bin/env bash
# Quick backup of important user configs

set -euo pipefail

BACKUP_DIR="${HOME}/backups/configs/$(date +%Y-%m-%d)"
mkdir -p "$BACKUP_DIR"

echo "==> Backing up configs to $BACKUP_DIR"

# Important dotfiles and folders
cp -r ~/.config "$BACKUP_DIR/" 2>/dev/null || true
cp ~/.zshrc ~/.gitconfig ~/.ssh/config "$BACKUP_DIR/" 2>/dev/null || true

echo "==> Backup complete: $BACKUP_DIR"
