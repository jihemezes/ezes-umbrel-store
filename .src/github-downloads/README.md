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

Ou sans Docker : `python3 ghdl.py serve`.

## Graphique et exports

- Le graphique affiche les téléchargements **par période** (jour, puis semaine ou mois
  sur les longues durées) ou le **cumul**, au choix, pour le total ou par dépôt, par
  version ou par asset (les 6 plus gros, le reste est regroupé dans « Autres »), sur
  7 jours à tout l'historique. Un clic sur un dépôt restreint le graphique à ce dépôt.
- Le gain d'un asset entre deux relevés est la différence de ses compteurs. Un asset
  d'une release publiée entre-temps compte pour tout son total ; un asset ancien qui
  apparaît (dépôt ou token ajouté, forks activés) est ignoré, pour éviter un faux pic.
- Boutons **Exporter CSV** (dernier relevé) et **Historique CSV** (tous les relevés) :
  séparateur point-virgule et BOM UTF-8 pour s'ouvrir directement dans Excel en français.
- En ligne de commande : `python3 ghdl.py export [--history] [--out fichier.csv]`
  (séparateur virgule).
- Réglages du graphique pré-sélectionnables par l'adresse : `?mode=cum&by=repo&days=90`.

## Token GitHub

Facultatif. Sans token : 60 requêtes/heure, dépôts publics seulement. Avec un
token (fine-grained, lecture de « Contents ») : 5000 requêtes/heure et dépôts
privés du compte. Environ une requête par dépôt à chaque relevé.

## Limites

- Seuls les assets uploadés sur une release sont comptés (pas les archives « Source code »).
- GitHub ne donne qu'un total cumulé : l'historique commence au premier relevé.
- Variables facultatives : `GHDL_OWNER`, `GITHUB_TOKEN`, `GHDL_REFRESH`, `GHDL_API`
  (GitHub Enterprise), `PORT`, `GHDL_DATA`.
