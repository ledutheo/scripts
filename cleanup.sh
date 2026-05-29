#!/usr/bin/env bash
# Quick system cleanup for Arch/Manjaro

set -euo pipefail

echo "==> Cleaning pacman cache..."
sudo pacman -Sc --noconfirm

echo "==> Removing old journal logs (keeping last 2 weeks)..."
sudo journalctl --vacuum-time=2weeks

echo "==> Cleaning user cache..."
rm -rf ~/.cache/thumbnails/* 2>/dev/null || true
rm -rf ~/.cache/mozilla/firefox/*/cache2 2>/dev/null || true

echo "==> Cleanup complete."
echo "Tip: Run 'du -sh ~/.cache' to see current cache size."
