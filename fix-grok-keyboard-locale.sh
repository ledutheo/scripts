#!/bin/bash
# Script de réparation du problème de clavier + encodage (locale UTF-8)
# Cause des saisies bizarres dans Grok TUI et ailleurs

set -e

echo "=== Réparation locale clavier/encodage ==="
echo "LANG actuel: $LANG"

echo
echo "Étape 1: Activation de fr_CH.UTF-8 dans /etc/locale.gen"
sudo sed -i 's/# fr_CH.UTF-8 UTF-8/fr_CH.UTF-8 UTF-8/' /etc/locale.gen || true

echo
echo "Étape 2: Génération des locales (peut prendre 10-20 secondes)"
sudo locale-gen

echo
echo "Étape 3: Configuration par défaut"
echo 'LANG="fr_CH.UTF-8"' | sudo tee /etc/default/locale > /dev/null
echo 'LC_ALL="fr_CH.UTF-8"' | sudo tee -a /etc/default/locale > /dev/null

echo
echo "=== Terminé ==="
echo "IMPORTANT: Déconnecte-toi complètement de ta session (ou redémarre)"
echo "puis reconnecte-toi."
echo
echo "Après reconnexion, vérifie avec:"
echo "  locale charmap"
echo "(doit afficher UTF-8)"
echo
echo "Puis relance 'grok' et teste ton clavier."
