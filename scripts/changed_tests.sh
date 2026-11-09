#!/usr/bin/env bash
# Affiche les cibles pytest liées aux fichiers modifiés (best effort).
#   - fichier de test modifié      → lui-même
#   - code applicatif modifié      → dossier tests/ de l'app concernée
#   - conftest / settings / deps   → toute la suite (affiche "ALL")
# Usage : scripts/changed_tests.sh [BASE]   (défaut : origin/main)
set -euo pipefail
base="${1:-origin/main}"

changed=$( {
  git diff --name-only "$base"...HEAD 2>/dev/null || true
  git diff --name-only HEAD
  git ls-files --others --exclude-standard
} | sort -u )

targets=()
for f in $changed; do
  case "$f" in
    conftest.py | savr_back/* | core/* | requirements*.txt | pyproject.toml)
      echo ALL
      exit 0
      ;;
    */migrations/*) ;;
    */tests/test_*.py) [ -f "$f" ] && targets+=("$f") ;;
    *.py)
      app="${f%%/*}"
      [ -d "$app/tests" ] && targets+=("$app/tests")
      ;;
  esac
done

[ ${#targets[@]} -eq 0 ] && exit 0
printf '%s\n' "${targets[@]}" | sort -u
