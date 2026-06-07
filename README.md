# scripts

> Collection de scripts utilitaires personnels.

Des outils concrets qui me simplifient la vie sur Arch/Manjaro.

## 📜 Scripts

| Script                | Description                                      |
|-----------------------|--------------------------------------------------|
| `update-system.sh`    | Mise à jour complète (pacman + AUR)              |
| `cleanup.sh`          | Nettoyage système (cache, journaux, orphelins)   |
| `dotfiles-update.sh`  | Met à jour les dotfiles depuis GitHub            |
| `backup-configs.sh`   | Sauvegarde rapide des configs importantes        |
| `aur-info.sh`         | Liste les paquets AUR installés                  |
| `fix-grok-keyboard-locale.sh` | Répare locale UTF-8 + clavier Grok TUI   |

Voir aussi : [google-takeout-audit](https://github.com/ledutheo/google-takeout-audit) — audit graphique Google Takeout (repo dédié).

## 🚀 Installation

```bash
git clone git@github.com:ledutheo/scripts.git ~/scripts
cd ~/scripts
chmod +x *.sh

# Lier dans ~/.local/bin (recommandé)
mkdir -p ~/.local/bin
for f in *.sh; do ln -sf "$PWD/$f" "$HOME/.local/bin/"; done
```

## Licence

MIT
