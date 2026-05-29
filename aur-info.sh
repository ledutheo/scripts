#!/usr/bin/env bash
# Show info about installed AUR packages

echo "==> Installed AUR packages:"
pacman -Qm | column -t
