# GitHub Downloads (sources)

Code source de l'app Umbrel `ezes-github-downloads` (dossier voisin à la racine
du store). Tableau de bord des téléchargements des assets de releases GitHub
pour tous les dépôts d'un compte ou d'une organisation ; compte, token et
fréquence de relevé se règlent dans l'interface.

Ce dossier est caché (`.src/`) pour ne pas être pris pour une app par Umbrel.

## Image

Construite par `.github/workflows/build-github-downloads.yml` (amd64 + arm64) et
publiée sur `ghcr.io/jihemezes/github-downloads`. Le paquet GHCR doit être
**public** pour que l'Umbrel puisse le télécharger.

## Nouvelle version

1. Modifier le code dans ce dossier.
2. Changer `version` dans `ezes-github-downloads/umbrel-app.yml` **et** le tag de
   l'image dans `ezes-github-downloads/docker-compose.yml`.
3. Pousser sur `main` : la CI construit et publie l'image.

## Essai en local

```bash
cd .src/github-downloads
docker build -t github-downloads .
docker run --rm -p 8080:8080 -v ghdl-data:/data github-downloads
# http://localhost:8080 puis « Réglages »
```

Ou sans Docker : `python3 ghdl.py serve`. Export CSV : `python3 ghdl.py export`.

## Token GitHub

Facultatif. Sans token : 60 requêtes/heure, dépôts publics seulement. Avec un
token (fine-grained, lecture de « Contents ») : 5000 requêtes/heure et dépôts
privés du compte. Environ une requête par dépôt à chaque relevé.

## Limites

- Seuls les assets uploadés sur une release sont comptés (pas les archives « Source code »).
- GitHub ne donne qu'un total cumulé : l'historique commence au premier relevé.
- Variables facultatives : `GHDL_OWNER`, `GITHUB_TOKEN`, `GHDL_REFRESH`, `GHDL_API`
  (GitHub Enterprise), `PORT`, `GHDL_DATA`.
