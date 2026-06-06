#!/usr/bin/env bash
# Quick system update script for Arch/Manjaro

set -euo pipefail

echo "==> Updating package databases and system..."
sudo pacman -Syu --noconfirm

if command -v yay &>/dev/null; then
  echo "==> Updating AUR packages with yay..."
  yay -Syu --noconfirm
elif command -v paru &>/dev/null; then
  echo "==> Updating AUR packages with paru..."
  paru -Syu --noconfirm
fi

echo "==> Removing orphaned packages..."
orphans=$(pacman -Qtdq || true)
if [[ -n "$orphans" ]]; then
  sudo pacman -Rns -- ${orphans}
else
  echo "No orphans found."
fi

echo "==> Cleaning package cache..."
sudo pacman -Sc --noconfirm

echo "==> System update complete."
