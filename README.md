# overrrrhere-gamecube

Compilation, sur un Mac de GitHub Actions, du cœur GameCube utilisé par l'app personnelle overrrrhere (iPhone).

- **Source** : [iCube](https://github.com/Provenance-Emu/iCube) (Provenance-Emu, dérivé de [Dolphin](https://github.com/dolphin-emu/dolphin)), figé au commit `f24ca8741b80fa8737b3b4e47940997016cd8052`.
- **Mode** : sans JIT (Cached Interpreter). Aucun débogueur, aucun StikDebug.
- **Pont** : `pont/GameCube.mm` (API C dans `pont/GameCube.h`), compilé dans la bibliothèque iOS de Dolphin. Il reprend la façon dont iCube démarre Dolphin et lance un jeu.
- **Sortie** : `GameCube.framework` (iPhone arm64) et `Sys.zip` (fichiers de données de Dolphin), dans les artefacts du workflow « Cœur GameCube ».

Ce dépôt ne contient ni jeu, ni BIOS, ni clé, ni secret. Il ne contient pas le code de l'app overrrrhere.

## Sécurité

- Workflow lancé **à la main uniquement** (`workflow_dispatch`), jamais par une pull request.
- Droits du jeton GitHub en **lecture seule** ; aucun secret utilisé.
- iCube et les actions GitHub sont **figés par leur identifiant exact** (SHA), pas par un nom de branche ou de version qui pourrait changer.

## Licence

GPL-2.0-or-later, comme Dolphin et iCube (voir `LICENSE`).
