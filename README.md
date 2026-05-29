# scripts

> Collection de scripts utilitaires personnels.

Des petits outils qui me rendent la vie plus simple au quotidien.

## 📜 Scripts disponibles

| Script              | Description                              | Usage                     |
|---------------------|------------------------------------------|---------------------------|
| `update-system`     | Mise à jour complète (pacman + AUR)      | `./update-system.sh`      |
| `cleanup`           | Nettoyage système (cache, journaux, etc) | `./cleanup.sh`            |
| `dotfiles-update`   | Met à jour les dotfiles                  | `./dotfiles-update.sh`    |

## 🚀 Installation

```bash
git clone git@github.com:ledutheo/scripts.git ~/scripts
cd ~/scripts

# Rendre les scripts exécutables
chmod +x *.sh

# Optionnel : les lier dans ~/.local/bin
for f in *.sh; do ln -sf "$PWD/$f" "$HOME/.local/bin/${f}"; done
```

## 🔧 Ajouter au PATH

Si tu veux les avoir dans ton PATH facilement :

```bash
export PATH="$HOME/scripts:$PATH"
```

## Licence

MIT
