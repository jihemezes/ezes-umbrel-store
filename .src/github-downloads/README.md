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

## Analyses

L'onglet **Analyses** est calculé à partir des relevés enregistrés ; les fenêtres de 7 et 30 jours
sont mesurées à partir du dernier relevé. Plus l'historique s'allonge, plus c'est riche : certaines
analyses demandent un minimum de relevés et l'indiquent à l'écran.

| Analyse | Principe | Historique requis |
|---|---|---|
| Rythme sur 7 jours | gains des 7 derniers jours, divisés par la durée couverte | 1 jour |
| Tendance | 7 derniers jours comparés aux 7 précédents | 14 jours |
| Pics inhabituels | jours complets très au-dessus de la médiane (écart absolu médian, seuil fixe si l'activité est très régulière) | 14 jours |
| Plus fortes progressions | gains par asset sur 7 jours, avec les 7 jours précédents | 1 jour |
| Systèmes, architectures | déduits du nom des assets (`win`, `linux`, `.dmg`, `arm64`, `x86_64`…) | 1 jour |
| Part de chaque version | part de chaque version dans les téléchargements des 30 jours | 1 jour |
| Adoption des releases | téléchargements dans les 7 et 30 premiers jours après la publication | release vue dès sa publication |
| Assets dormants | aucun téléchargement depuis 30 jours (release d'au moins 30 jours) | 30 jours |
| Étoiles, forks, abonnés | relevés à chaque collecte, variations 7 et 30 jours | 7 jours |

## Trafic

L'onglet **Trafic** reprend l'API *Traffic* de GitHub : vues et clones par jour (avec visiteurs
uniques), sites d'origine et pages les plus vues. Il mesure les visites et clones **du dépôt**, pas
les téléchargements d'assets, et ne donne aucune identité (ni IP, ni navigateur : GitHub ne les
fournit pas).

- GitHub ne garde que **14 jours** ; l'appli enregistre chaque jour à chaque collecte et construit
  ainsi un historique plus long. Pour ne rien perdre, gardez une collecte au moins tous les 3 jours.
- Les sites d'origine et les pages sont des fenêtres glissantes de 14 jours : seule la dernière est conservée.
- Il faut un token avec l'**accès en écriture** aux dépôts : token classique avec le scope `repo`, ou
  fine-grained avec « Contents » et « Administration » en **lecture**. Sans les droits, l'appli le
  signale (« Trafic indisponible pour N dépôt(s) ») et continue de relever les téléchargements.
- Le relevé du trafic se désactive dans les Réglages. Il coûte environ 5 requêtes par dépôt et par collecte.

## Token GitHub

Facultatif. Sans token : 60 requêtes/heure, dépôts publics seulement, pas de trafic. Avec un
token : 5000 requêtes/heure, dépôts privés du compte, et le trafic si les droits ci-dessus sont accordés.
Environ une requête par dépôt à chaque relevé, plus cinq avec le trafic.

## Limites

- Seuls les assets uploadés sur une release sont comptés (pas les archives « Source code »).
- GitHub ne donne qu'un total cumulé : l'historique commence au premier relevé.
- Variables facultatives : `GHDL_OWNER`, `GITHUB_TOKEN`, `GHDL_REFRESH`, `GHDL_API`
  (GitHub Enterprise), `PORT`, `GHDL_DATA`.
