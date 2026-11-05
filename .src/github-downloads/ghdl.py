#!/usr/bin/env python3
"""GitHub Downloads - statistiques de téléchargement des assets de releases GitHub.

Relève les compteurs de tous les dépôts d'un compte (utilisateur ou
organisation), garde un historique dans SQLite et sert un dashboard web.
Le compte, le token et la fréquence se règlent depuis l'interface web.

    python ghdl.py serve                # dashboard (http://localhost:8080)
    python ghdl.py collect [OWNER]      # un relevé en ligne de commande
    python ghdl.py export [--history]   # CSV du dernier relevé (ou de tous)

Variables d'environnement (toutes facultatives) :
    GHDL_DATA     dossier de données (base + config), défaut : dossier du script
    GHDL_DB       chemin de la base SQLite
    GHDL_OWNER    compte par défaut (si rien n'est réglé dans l'interface)
    GITHUB_TOKEN  token par défaut (idem)
    GHDL_REFRESH  fréquence de collecte par défaut en heures (défaut 6, 0 = off)
    GHDL_API      URL de l'API (GitHub Enterprise : https://hote/api/v3)
    PORT          port du serveur (défaut 8080)
"""
import argparse
import csv
import datetime
import io
import json
import os
import re
import sqlite3
import statistics
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from itertools import groupby
from pathlib import Path
from urllib.parse import parse_qs, urlparse

API = os.environ.get("GHDL_API", "https://api.github.com").rstrip("/")
HERE = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("GHDL_DATA", HERE))
DB_PATH = Path(os.environ.get("GHDL_DB", DATA_DIR / "downloads.db"))
CONFIG_PATH = DATA_DIR / "config.json"
OWNER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,38}$")

COLLECT_LOCK = threading.Lock()
STATE = {"collecting": False, "error": None, "warnings": [], "last_attempt": None}

SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshots (
    ts           INTEGER NOT NULL,
    owner        TEXT    NOT NULL,
    repo         TEXT    NOT NULL,
    tag          TEXT    NOT NULL,
    asset        TEXT    NOT NULL,
    downloads    INTEGER NOT NULL,
    size         INTEGER,
    published_at TEXT,
    PRIMARY KEY (ts, owner, repo, tag, asset)
);
CREATE INDEX IF NOT EXISTS idx_snap_owner_ts ON snapshots(owner, ts);

CREATE TABLE IF NOT EXISTS repo_stats (
    ts       INTEGER NOT NULL,
    owner    TEXT    NOT NULL,
    repo     TEXT    NOT NULL,
    stars    INTEGER,
    forks    INTEGER,
    watchers INTEGER,
    issues   INTEGER,
    PRIMARY KEY (ts, owner, repo)
);

CREATE TABLE IF NOT EXISTS traffic_daily (
    owner         TEXT    NOT NULL,
    repo          TEXT    NOT NULL,
    day           TEXT    NOT NULL,
    views         INTEGER NOT NULL DEFAULT 0,
    view_uniques  INTEGER NOT NULL DEFAULT 0,
    clones        INTEGER NOT NULL DEFAULT 0,
    clone_uniques INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (owner, repo, day)
);

-- sites d'origine et pages les plus vues : fenêtre glissante de 14 jours, on ne garde que la dernière
CREATE TABLE IF NOT EXISTS traffic_referrers (
    ts       INTEGER NOT NULL,
    owner    TEXT    NOT NULL,
    repo     TEXT    NOT NULL,
    referrer TEXT    NOT NULL,
    count    INTEGER NOT NULL,
    uniques  INTEGER NOT NULL,
    PRIMARY KEY (owner, repo, referrer)
);

CREATE TABLE IF NOT EXISTS traffic_paths (
    ts      INTEGER NOT NULL,
    owner   TEXT    NOT NULL,
    repo    TEXT    NOT NULL,
    path    TEXT    NOT NULL,
    title   TEXT,
    count   INTEGER NOT NULL,
    uniques INTEGER NOT NULL,
    PRIMARY KEY (owner, repo, path)
);
"""


# --------------------------------------------------------------------------
# Configuration (fichier JSON dans le dossier de données)
# --------------------------------------------------------------------------
def load_config():
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_config(cfg):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(cfg), encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    tmp.replace(CONFIG_PATH)


def public_config():
    """Configuration effective, sans jamais exposer le token."""
    cfg = load_config()
    owner = cfg.get("owner") or os.environ.get("GHDL_OWNER") or ""
    if cfg.get("token"):
        source = "config"
    elif os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN"):
        source = "env"
    else:
        source = None
    refresh = cfg.get("refresh_hours")
    if refresh is None:
        try:
            refresh = float(os.environ.get("GHDL_REFRESH", "6"))
        except ValueError:
            refresh = 6.0
    return {"owner": owner, "token_source": source, "refresh_hours": refresh,
            "include_forks": bool(cfg.get("include_forks", False)),
            "traffic": bool(cfg.get("traffic", True))}


def update_config(body):
    """Valide et enregistre les réglages. Renvoie True si la collecte doit être relancée."""
    cfg = load_config()
    changed = False
    if "owner" in body:
        owner = str(body["owner"]).strip().lstrip("@")
        if not OWNER_RE.match(owner):
            raise ValueError("Nom de compte GitHub invalide.")
        if owner != cfg.get("owner"):
            cfg["owner"] = owner
            changed = True
    if body.get("clear_token"):
        if cfg.pop("token", None) is not None:
            changed = True
    elif body.get("token"):
        tok = str(body["token"]).strip()
        if len(tok) > 255 or any(c.isspace() for c in tok):
            raise ValueError("Token invalide.")
        cfg["token"] = tok
        changed = True
    if "refresh_hours" in body:
        try:
            hours = float(body["refresh_hours"])
        except (TypeError, ValueError):
            raise ValueError("Fréquence invalide.")
        if not 0 <= hours <= 720:
            raise ValueError("Fréquence invalide.")
        cfg["refresh_hours"] = hours
    if "include_forks" in body:
        forks = bool(body["include_forks"])
        if forks != bool(cfg.get("include_forks", False)):
            changed = True
        cfg["include_forks"] = forks
    if "traffic" in body:
        traffic = bool(body["traffic"])
        if traffic and not cfg.get("traffic", True):
            changed = True
        cfg["traffic"] = traffic
    save_config(cfg)
    return changed


# --------------------------------------------------------------------------
# GitHub
# --------------------------------------------------------------------------
def get_token():
    cfg = load_config()
    token = cfg.get("token") or os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token:
        return token.strip()
    try:
        out = subprocess.run(["gh", "auth", "token"], capture_output=True,
                             text=True, timeout=10)
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    return None


def _request(url, token):
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "github-downloads",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, headers=headers)
    try:
        return urllib.request.urlopen(req, timeout=30)
    except urllib.error.HTTPError as e:
        if e.code in (403, 429) and e.headers.get("X-RateLimit-Remaining") == "0":
            reset = int(e.headers.get("X-RateLimit-Reset", "0") or 0)
            wait = max(1, (reset - int(time.time())) // 60)
            raise RuntimeError(
                f"Limite de requêtes GitHub atteinte, nouvel essai possible dans ~{wait} min. "
                "Ajoutez un token dans les réglages pour passer à 5000 requêtes/heure.")
        raise


def api_json(url, token):
    with _request(url, token) as r:
        return json.load(r)


def api_pages(url, token):
    """Itère sur toutes les pages d'un endpoint qui renvoie une liste."""
    while url:
        with _request(url, token) as r:
            data = json.load(r)
            link = r.headers.get("Link", "")
        yield from data
        url = None
        for part in link.split(","):
            if 'rel="next"' in part:
                url = part[part.find("<") + 1:part.find(">")]


def list_repos(owner, token, include_forks):
    """Dépôts du compte (privés inclus si le token appartient au compte)."""
    me = None
    if token:
        try:
            me = api_json(f"{API}/user", token)["login"]
        except urllib.error.HTTPError:
            me = None
    if me and me.lower() == owner.lower():
        url = f"{API}/user/repos?affiliation=owner&per_page=100"
    else:
        kind = api_json(f"{API}/users/{owner}", token).get("type")
        if kind == "Organization":
            url = f"{API}/orgs/{owner}/repos?type=all&per_page=100"
        else:
            url = f"{API}/users/{owner}/repos?type=owner&per_page=100"
    for repo in api_pages(url, token):
        if repo.get("fork") and not include_forks:
            continue
        yield repo


def http_error_message(e):
    if e.code == 401:
        return "Token refusé par GitHub (401) : vérifiez-le dans les réglages."
    if e.code == 404:
        return "Compte ou dépôt introuvable (404) : vérifiez le nom du compte."
    if e.code == 403:
        return "Accès refusé par GitHub (403) : droits du token insuffisants ou limite atteinte."
    return f"Erreur GitHub HTTP {e.code}."


# --------------------------------------------------------------------------
# Base de données
# --------------------------------------------------------------------------
def db_connect():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def last_snapshot_ts(owner):
    conn = db_connect()
    try:
        return conn.execute("SELECT MAX(ts) AS m FROM snapshots WHERE owner=?",
                            (owner,)).fetchone()["m"]
    finally:
        conn.close()


def _try_json(url, token):
    """Appel GitHub qui ne lève jamais : (données, état, message). État : ok, denied, failed ou limit."""
    try:
        return api_json(url, token), "ok", None
    except RuntimeError as e:
        return None, "limit", str(e)
    except urllib.error.HTTPError as e:
        return None, ("denied" if e.code in (403, 404) else "failed"), None
    except (urllib.error.URLError, OSError, ValueError):
        return None, "failed", None


TRAFFIC_PARTS = (("clones", "clones"), ("referrers", "popular/referrers"), ("paths", "popular/paths"))


def collect_extras(repos, token, stats, quiet=False):
    """Phase facultative : nombre d'abonnés et trafic (vues, clones, sites d'origine, pages).

    Ne lève jamais d'erreur : un échec ici ne doit pas faire perdre le relevé des
    téléchargements, déjà enregistré. Renvoie (trafic par dépôt, avertissements).
    """
    traffic, warnings = {}, []
    denied = failed = 0
    limit = None
    for repo in repos:
        name, base = repo["name"], f"{API}/repos/{repo['full_name']}"
        detail, kind, msg = _try_json(base, token)
        if kind == "limit":
            limit = msg
            break
        if kind == "ok" and isinstance(detail, dict) and name in stats:
            stats[name]["watchers"] = detail.get("subscribers_count")
        views, kind, msg = _try_json(f"{base}/traffic/views", token)
        if kind == "limit":
            limit = msg
            break
        if kind != "ok" or not isinstance(views, dict):
            if kind == "denied":
                denied += 1
            else:
                failed += 1
            continue
        entry = {"views": views.get("views") or [], "clones": None, "referrers": None, "paths": None}
        traffic[name] = entry
        part_failed = False
        for key, part in TRAFFIC_PARTS:
            data, kind, msg = _try_json(f"{base}/traffic/{part}", token)
            if kind == "limit":
                limit = msg
                break
            if kind != "ok":
                part_failed = True
                continue
            if key == "clones":
                entry[key] = data.get("clones") or [] if isinstance(data, dict) else None
            else:
                entry[key] = data if isinstance(data, list) else None
        if part_failed:
            failed += 1
        if limit:
            break
        if not quiet:
            print(f"  trafic {name}", file=sys.stderr)
    n = len(repos)
    if denied:
        warnings.append(f"Trafic indisponible pour {denied} dépôt(s) sur {n} : le token doit avoir l'accès en "
                        "écriture au dépôt (permission « Administration » en lecture pour un token fine-grained).")
    if failed:
        warnings.append(f"Trafic incomplet pour {failed} dépôt(s) (erreur temporaire de GitHub).")
    if limit:
        warnings.append(limit)
    return traffic, warnings


def _day(stamp):
    """'2026-11-04T00:00:00Z' -> '2026-11-04'."""
    return str(stamp)[:10]


def store_traffic(conn, ts, owner, repo, t):
    """Enregistre le trafic d'un dépôt : les jours sont mis à jour, les listes remplacées."""
    upsert = ("INSERT INTO traffic_daily(owner,repo,day,{a},{b}) VALUES (?,?,?,?,?) "
              "ON CONFLICT(owner,repo,day) DO UPDATE SET {a}=excluded.{a}, {b}=excluded.{b}")
    for key, a, b in (("views", "views", "view_uniques"), ("clones", "clones", "clone_uniques")):
        sql = upsert.format(a=a, b=b)
        for e in t.get(key) or []:
            try:
                conn.execute(sql, (owner, repo, _day(e["timestamp"]), int(e["count"]), int(e["uniques"])))
            except (KeyError, TypeError, ValueError):
                continue
    if t.get("referrers") is not None:
        conn.execute("DELETE FROM traffic_referrers WHERE owner=? AND repo=?", (owner, repo))
        for e in t["referrers"][:50]:
            try:
                conn.execute("INSERT OR REPLACE INTO traffic_referrers VALUES (?,?,?,?,?,?)",
                             (ts, owner, repo, str(e["referrer"])[:200], int(e["count"]), int(e["uniques"])))
            except (KeyError, TypeError, ValueError):
                continue
    if t.get("paths") is not None:
        conn.execute("DELETE FROM traffic_paths WHERE owner=? AND repo=?", (owner, repo))
        for e in t["paths"][:50]:
            try:
                conn.execute("INSERT OR REPLACE INTO traffic_paths VALUES (?,?,?,?,?,?,?)",
                             (ts, owner, repo, str(e["path"])[:300], str(e.get("title") or "")[:200],
                              int(e["count"]), int(e["uniques"])))
            except (KeyError, TypeError, ValueError):
                continue


def collect(owner, include_forks=False, quiet=False, traffic=True):
    """Relevé complet. Phase 1 : releases et étoiles/forks (enregistrés tout de suite).
    Phase 2, facultative et avec un token : abonnés et trafic, qui ne peut pas la remettre en cause."""
    token = get_token()
    ts = int(time.time())
    rows, stats, repos = [], {}, []
    for repo in list_repos(owner, token, include_forks):
        repos.append(repo)
        name = repo["name"]
        stats[name] = {"stars": repo.get("stargazers_count"), "forks": repo.get("forks_count"),
                       "watchers": None, "issues": repo.get("open_issues_count")}
        url = f"{API}/repos/{repo['full_name']}/releases?per_page=100"
        for rel in api_pages(url, token):
            for a in rel.get("assets", []):
                rows.append((ts, owner, name, rel["tag_name"], a["name"],
                             a["download_count"], a["size"], rel.get("published_at")))
        if not quiet:
            print(f"  {name}", file=sys.stderr)
    conn = db_connect()
    with conn:
        conn.executemany("INSERT OR REPLACE INTO snapshots VALUES (?,?,?,?,?,?,?,?)", rows)
        conn.executemany("INSERT OR REPLACE INTO repo_stats VALUES (?,?,?,?,?,?,?)",
                         [(ts, owner, n, st["stars"], st["forks"], None, st["issues"])
                          for n, st in stats.items()])
    conn.close()

    warnings = []
    if traffic and token and repos:
        try:
            data, warnings = collect_extras(repos, token, stats, quiet)
            conn = db_connect()
            try:
                with conn:
                    for n, st in stats.items():
                        if st["watchers"] is not None:
                            conn.execute("UPDATE repo_stats SET watchers=? WHERE ts=? AND owner=? AND repo=?",
                                         (st["watchers"], ts, owner, n))
                    for n, t in data.items():
                        store_traffic(conn, ts, owner, n, t)
            finally:
                conn.close()
        except Exception as e:  # noqa: BLE001
            warnings.append(f"Collecte du trafic interrompue ({type(e).__name__}).")
    if not quiet:
        total = sum(r[5] for r in rows)
        print(f"{len(repos)} dépôts, {len(rows)} assets, {total} téléchargements au total.", file=sys.stderr)
        for w in warnings:
            print(f"  ! {w}", file=sys.stderr)
    return {"assets": len(rows), "repos": len(repos), "warnings": warnings}


def run_collect(owner, include_forks, traffic=True):
    """Collecte protégée par un verrou ; mémorise l'état, les avertissements et l'éventuelle erreur."""
    if not COLLECT_LOCK.acquire(blocking=False):
        return False
    STATE.update(collecting=True, error=None, warnings=[])
    try:
        result = collect(owner, include_forks, quiet=True, traffic=traffic)
        STATE["warnings"] = result["warnings"]
    except urllib.error.HTTPError as e:
        STATE["error"] = http_error_message(e)
    except urllib.error.URLError as e:
        STATE["error"] = f"GitHub injoignable : {e.reason}"
    except Exception as e:  # noqa: BLE001
        STATE["error"] = str(e) if isinstance(e, RuntimeError) else f"{type(e).__name__}: {e}"
    finally:
        STATE.update(collecting=False, last_attempt=int(time.time()))
        COLLECT_LOCK.release()
    return True


def start_collect_async():
    cfg = public_config()
    if not cfg["owner"] or COLLECT_LOCK.locked():
        return False
    threading.Thread(target=run_collect, args=(cfg["owner"], cfg["include_forks"], cfg["traffic"]),
                     daemon=True).start()
    return True


def scheduler():
    """Relance une collecte quand le dernier relevé est plus vieux que la fréquence réglée."""
    while True:
        try:
            cfg = public_config()
            hours = cfg["refresh_hours"]
            if cfg["owner"] and hours and hours > 0 and not COLLECT_LOCK.locked():
                last = last_snapshot_ts(cfg["owner"])
                attempt = STATE["last_attempt"]
                due = last is None or time.time() - last >= hours * 3600
                calm = attempt is None or time.time() - attempt >= 900  # 15 min entre essais
                if due and calm:
                    run_collect(cfg["owner"], cfg["include_forks"], cfg["traffic"])
        except Exception as e:  # noqa: BLE001
            print(f"planificateur : {e}", file=sys.stderr)
        time.sleep(60)


def build_payload(owner, fallback=False):
    conn = db_connect()
    if not owner and fallback:
        row = conn.execute("SELECT owner FROM snapshots ORDER BY ts DESC LIMIT 1").fetchone()
        owner = row["owner"] if row else None
    base = {"owner": owner, "last_ts": None, "prev_ts": None, "assets": []}
    if not owner:
        conn.close()
        return base
    last = conn.execute("SELECT MAX(ts) AS m FROM snapshots WHERE owner=?", (owner,)).fetchone()["m"]
    if last is None:
        conn.close()
        return base
    prev = conn.execute("SELECT MAX(ts) AS m FROM snapshots WHERE owner=? AND ts<?",
                        (owner, last)).fetchone()["m"]
    prev_map = {}
    if prev:
        for r in conn.execute("SELECT repo,tag,asset,downloads FROM snapshots WHERE owner=? AND ts=?",
                              (owner, prev)):
            prev_map[(r["repo"], r["tag"], r["asset"])] = r["downloads"]
    assets = []
    for r in conn.execute("""SELECT repo,tag,asset,downloads,size,published_at
                             FROM snapshots WHERE owner=? AND ts=?""", (owner, last)):
        d = dict(r)
        key = (r["repo"], r["tag"], r["asset"])
        d["delta"] = r["downloads"] - prev_map[key] if key in prev_map else None
        assets.append(d)
    conn.close()
    base.update(last_ts=last, prev_ts=prev, assets=assets)
    return base


def _epoch(iso):
    """Date ISO 8601 de GitHub -> timestamp, ou None si absente ou illisible."""
    try:
        return datetime.datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()
    except (AttributeError, ValueError):
        return None


def iter_snapshots(rows):
    """Parcourt les relevés (lignes triées par ts) : produit (ts, compteurs, gains, dates de publication).

    `compteurs` et `gains` sont indexés par (dépôt, version, asset). Le gain est la différence
    des compteurs depuis le relevé précédent ; le premier relevé sert de base (gain nul).
    Un asset d'une release publiée entre deux relevés compte pour tout son total ; un asset
    ancien qui apparaît (dépôt ou token ajouté, forks activés) ne compte pas, ce n'est pas un
    téléchargement nouveau ; un compteur qui baisse est considéré comme remis à zéro.
    """
    prev, prev_ts, pubs = None, None, {}
    for ts, chunk in groupby(rows, key=lambda r: r["ts"]):
        cur = {}
        for r in chunk:
            key = (r["repo"], r["tag"], r["asset"])
            cur[key] = r["downloads"]
            if key not in pubs:
                pubs[key] = _epoch(r["published_at"])
        gains = {}
        for key, d in cur.items():
            if prev is None:
                gains[key] = 0
                continue
            before = prev.get(key)
            if before is None:
                pub = pubs.get(key)
                gains[key] = d if pub is not None and pub > prev_ts else 0
            else:
                gains[key] = d if d < before else d - before
        yield ts, cur, gains, pubs
        prev, prev_ts = cur, ts


def group_name(by, repo_filter, repo, tag, asset):
    if by == "repo":
        return repo
    if by == "tag":
        return tag if repo_filter else f"{repo} · {tag}"
    if by == "asset":
        return asset if repo_filter else f"{repo} · {asset}"
    return "Total"


def series_payload(owner, by="total", repo=None, days=30, top=6):
    """Courbes d'évolution regroupées par total / dépôt / version / asset.

    Chaque série donne le cumul (`values`) et le gain depuis le relevé précédent
    (`deltas`), calculé par `iter_snapshots`. Au-delà de 7 groupes, les plus petits sont
    réunis dans « Autres ».
    """
    empty = {"by": by, "repo": repo, "ts": [], "series": []}
    if not owner:
        return empty
    conn = db_connect()
    try:
        where, params = "owner=?", [owner]
        if repo:
            where += " AND repo=?"
            params.append(repo)
        query = f"SELECT ts, repo, tag, asset, downloads, published_at FROM snapshots WHERE {where}"
        qp = list(params)
        if days and days > 0:
            cutoff = int(time.time()) - days * 86400
            # le relevé juste avant la fenêtre sert de base au premier gain affiché
            base_ts = conn.execute(f"SELECT MAX(ts) AS m FROM snapshots WHERE {where} AND ts<=?",
                                   params + [cutoff]).fetchone()["m"]
            if base_ts is not None:
                query += " AND ts>=?"
                qp.append(base_ts)
        rows = conn.execute(query + " ORDER BY ts", qp).fetchall()
    finally:
        conn.close()

    ts_list, groups = [], {}
    for ts, cur, gains, _ in iter_snapshots(rows):
        idx = len(ts_list)
        ts_list.append(ts)
        vals, dels = {}, {}
        for key, d in cur.items():
            name = group_name(by, repo, *key)
            vals[name] = vals.get(name, 0) + d
            dels[name] = dels.get(name, 0) + gains[key]
        for name, g in groups.items():
            g["values"].append(vals.get(name))
            g["deltas"].append(dels.get(name, 0))
        for name in vals.keys() - groups.keys():
            groups[name] = {"values": [None] * idx + [vals[name]], "deltas": [0] * idx + [dels[name]]}

    def last_value(g):
        return next((v for v in reversed(g["values"]) if v is not None), 0)

    ranked = sorted(groups.items(), key=lambda kv: -last_value(kv[1]))
    limit = top if len(ranked) > top + 1 else len(ranked)
    series = [{"name": n, "values": g["values"], "deltas": g["deltas"]} for n, g in ranked[:limit]]
    rest = ranked[limit:]
    if rest:
        n = len(ts_list)
        values = [None if all(g["values"][i] is None for _, g in rest)
                  else sum(g["values"][i] or 0 for _, g in rest) for i in range(n)]
        deltas = [sum(g["deltas"][i] for _, g in rest) for i in range(n)]
        series.append({"name": "Autres", "values": values, "deltas": deltas})
    return {"by": by, "repo": repo, "ts": ts_list, "series": series}


def csv_safe(value):
    """Neutralise l'injection de formules (=, +, -, @) à l'ouverture dans un tableur."""
    text = "" if value is None else str(value)
    return "'" + text if text[:1] in ("=", "+", "-", "@") else text


def csv_latest(owner, repo=None, delimiter=","):
    """CSV du dernier relevé : un asset par ligne."""
    payload = build_payload(owner)
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=delimiter, lineterminator="\n")
    w.writerow(["repo", "tag", "asset", "downloads", "size", "published_at"])
    rows = 0
    for a in sorted(payload["assets"], key=lambda x: -x["downloads"]):
        if repo and a["repo"] != repo:
            continue
        w.writerow([csv_safe(a["repo"]), csv_safe(a["tag"]), csv_safe(a["asset"]),
                    a["downloads"], a["size"], a["published_at"] or ""])
        rows += 1
    return buf.getvalue(), rows


def csv_history(owner, repo=None, delimiter=","):
    """CSV de tous les relevés : un asset par relevé et par ligne."""
    conn = db_connect()
    try:
        query, params = "SELECT ts, repo, tag, asset, downloads FROM snapshots WHERE owner=?", [owner]
        if repo:
            query += " AND repo=?"
            params.append(repo)
        buf = io.StringIO()
        w = csv.writer(buf, delimiter=delimiter, lineterminator="\n")
        w.writerow(["date_utc", "repo", "tag", "asset", "downloads"])
        rows = 0
        for r in conn.execute(query + " ORDER BY ts, repo, tag, asset", params):
            when = datetime.datetime.fromtimestamp(r["ts"], datetime.timezone.utc)
            w.writerow([when.strftime("%Y-%m-%dT%H:%M:%SZ"), csv_safe(r["repo"]),
                        csv_safe(r["tag"]), csv_safe(r["asset"]), r["downloads"]])
            rows += 1
        return buf.getvalue(), rows
    finally:
        conn.close()


# Plateformes déduites du nom des assets
_OS_RE = {
    "Android": re.compile(r"^android$"),
    "iOS": re.compile(r"^ios$"),
    "macOS": re.compile(r"^(mac|macos|macosx|osx|darwin)$"),
    "Windows": re.compile(r"^win(dows)?(32|64)?$"),
    "Linux": re.compile(r"^(linux(32|64)?|gnu|musl|ubuntu|debian)$"),
}
_FMT_RE = {
    "Android": re.compile(r"^(apk|aab)$"),
    "iOS": re.compile(r"^ipa$"),
    "macOS": re.compile(r"^(dmg|pkg)$"),
    "Windows": re.compile(r"^(exe|msi|msix)$"),
    "Linux": re.compile(r"^(appimage|deb|rpm|snap|flatpak)$"),
}
_ARCH_RE = (
    ("arm64", re.compile(r"(?<![a-z0-9])(arm64|aarch64|armv8)(?![a-z0-9])")),
    ("x64", re.compile(r"(?<![a-z0-9])(x86[_-]64|x64|amd64|win64|linux64)(?![a-z0-9])")),
    ("x86", re.compile(r"(?<![a-z0-9])(x86|i[3-6]86|win32|ia32)(?![a-z0-9])")),
    ("arm", re.compile(r"(?<![a-z0-9])(armv7|armv6|armhf|arm)(?![a-z0-9])")),
    ("universel", re.compile(r"(?<![a-z0-9])universal2?(?![a-z0-9])")),
)
UNKNOWN_OS, UNKNOWN_ARCH = "Non précisé", "Non précisée"


def classify(name):
    """(système, architecture) déduits du nom d'un asset."""
    low = name.lower()
    tokens = [t for t in re.split(r"[^a-z0-9]+", low) if t]
    order = ("Android", "iOS", "macOS", "Windows", "Linux")
    system = UNKNOWN_OS
    for rx_map in (_OS_RE, _FMT_RE):      # d'abord le nom du système, puis le format de paquet
        system = next((o for o in order if any(rx_map[o].match(t) for t in tokens)), UNKNOWN_OS)
        if system != UNKNOWN_OS:
            break
    arch = next((a for a, rx in _ARCH_RE if rx.search(low)), UNKNOWN_ARCH)
    return system, arch


def _iso_day(n):
    """Numéro de jour depuis 1970 -> 'AAAA-MM-JJ'."""
    return (datetime.date(1970, 1, 1) + datetime.timedelta(days=n)).isoformat()


def is_spike(value, median, mad):
    """Pic inhabituel : nettement au-dessus de la médiane (écart robuste, ou seuil fixe si MAD nul)."""
    if value < median + 5:
        return False
    if mad > 0:
        return 0.6745 * (value - median) / mad >= 3.5
    return value >= max(3 * median, median + 10)


def analytics_payload(owner, repo=None, tz=0):
    """Analyses calculées à partir des relevés enregistrés.

    Les fenêtres (7 et 30 jours) sont mesurées à partir du dernier relevé ; `tz` est le décalage
    du navigateur en minutes à l'est d'UTC, pour regrouper les gains par jour local.
    """
    if not owner:
        return {"ready": False}
    conn = db_connect()
    try:
        where, params = "owner=?", [owner]
        if repo:
            where += " AND repo=?"
            params.append(repo)
        rows = conn.execute("SELECT ts, repo, tag, asset, downloads, published_at FROM snapshots "
                            f"WHERE {where} ORDER BY ts", params).fetchall()
        stat_rows = conn.execute("SELECT ts, repo, stars, forks, watchers, issues FROM repo_stats "
                                 f"WHERE {where} ORDER BY ts", params).fetchall()
    finally:
        conn.close()
    if not rows:
        return {"ready": False}

    DAY = 86400
    snaps = list(iter_snapshots(rows))
    ts_list = [x[0] for x in snaps]
    first_ts, last_ts = ts_list[0], ts_list[-1]
    last_cur, pubs = snaps[-1][1], snaps[-1][3]
    span = (last_ts - first_ts) / DAY

    def window(start, end):
        """Gains par asset sur les relevés pris dans l'intervalle ]start, end]."""
        acc = {}
        for ts, _, gains, _ in snaps:
            if start < ts <= end:
                for k, g in gains.items():
                    if g:
                        acc[k] = acc.get(k, 0) + g
        return acc

    w7 = window(last_ts - 7 * DAY, last_ts)
    p7 = window(last_ts - 14 * DAY, last_ts - 7 * DAY)
    w30 = window(last_ts - 30 * DAY, last_ts)
    tot7, totp7, tot30 = sum(w7.values()), sum(p7.values()), sum(w30.values())

    # --- derniers relevés par jour local, derniers gains par asset
    def local_day(ts):
        return int((ts + tz * 60) // DAY)

    day_totals, day_keys, last_gain_ts = {}, {}, {}
    for ts, _, gains, _ in snaps:
        d = local_day(ts)
        for k, g in gains.items():
            if g:
                day_totals[d] = day_totals.get(d, 0) + g
                dk = day_keys.setdefault(d, {})
                dk[k] = dk.get(k, 0) + g
                last_gain_ts[k] = ts
    first_day, last_day = local_day(first_ts), local_day(last_ts)
    days = [{"day": _iso_day(d), "downloads": day_totals.get(d, 0), "partial": d in (first_day, last_day)}
            for d in range(max(first_day, last_day - 29), last_day + 1)]

    # --- pics : jours complets uniquement (le premier et le dernier jour sont partiels)
    cand = list(range(max(first_day + 1, last_day - 60), last_day))
    spikes, spikes_ready = [], len(cand) >= 14
    if spikes_ready:
        vals = [day_totals.get(d, 0) for d in cand]
        med = statistics.median(vals)
        mad = statistics.median([abs(v - med) for v in vals])
        for d, v in zip(cand, vals):
            if d >= last_day - 30 and is_spike(v, med, mad):
                top = max(day_keys.get(d, {}).items(), key=lambda kv: kv[1], default=None)
                spikes.append({"day": _iso_day(d), "downloads": v, "typical": round(med, 1),
                               "top": None if top is None else
                               {"repo": top[0][0], "tag": top[0][1], "asset": top[0][2], "downloads": top[1]}})
        spikes = sorted(spikes, key=lambda x: -x["downloads"])[:5]

    # --- version la plus récente de chaque dépôt et part des téléchargements récents
    latest_tag = {}
    for (r, t, a) in last_cur:
        pub = pubs.get((r, t, a))
        if pub is not None and (r not in latest_tag or pub > latest_tag[r][1]):
            latest_tag[r] = (t, pub)
    on_latest = sum(g for (r, t, a), g in w30.items() if r in latest_tag and latest_tag[r][0] == t)

    rhythm = {
        "avg7": round(tot7 / min(7, span), 2) if span >= 1 else None,
        "total7": tot7,
        "prev7": totp7 if span >= 14 else None,
        "change_pct": round((tot7 - totp7) / totp7 * 100, 1) if span >= 14 and totp7 > 0 else None,
        "total30": tot30,
        "share_latest": round(on_latest / tot30 * 100, 1) if tot30 > 0 else None,
    }

    # --- plus fortes progressions
    movers = sorted(
        ({"repo": k[0], "tag": k[1], "asset": k[2], "gain7": g,
          "prev7": p7.get(k, 0) if span >= 14 else None, "total": last_cur[k]}
         for k, g in w7.items() if k in last_cur),
        key=lambda x: -x["gain7"])[:10]

    # --- plateformes
    os_agg, arch_agg = {}, {}
    for key, total in last_cur.items():
        system, arch = classify(key[2])
        for agg, name in ((os_agg, system), (arch_agg, arch)):
            e = agg.setdefault(name, {"name": name, "total": 0, "recent": 0})
            e["total"] += total
            e["recent"] += w30.get(key, 0)

    def ranked(agg):
        return sorted(agg.values(), key=lambda e: (e["name"] in (UNKNOWN_OS, UNKNOWN_ARCH), -e["total"]))

    platforms = {"os": ranked(os_agg), "arch": ranked(arch_agg)}

    # --- part de chaque version sur 30 jours (5 plus récentes par dépôt, le reste regroupé)
    per_rt = {}
    for (r, t, a), total in last_cur.items():
        e = per_rt.setdefault((r, t), {"total": 0, "recent": 0, "pub": None})
        e["total"] += total
        e["recent"] += w30.get((r, t, a), 0)
        pub = pubs.get((r, t, a))
        if pub is not None:
            e["pub"] = max(pub, e["pub"] or 0)
    by_repo_rt = {}
    for (r, t), e in per_rt.items():
        by_repo_rt.setdefault(r, []).append((t, e))
    versions = []
    for r, lst in by_repo_rt.items():
        lst.sort(key=lambda x: x[1]["pub"] or 0, reverse=True)
        recent_total = sum(e["recent"] for _, e in lst)
        tags = []
        for t, e in lst[:5]:
            tags.append({"tag": t, "published": e["pub"], "recent": e["recent"], "total": e["total"],
                         "latest": r in latest_tag and latest_tag[r][0] == t})
        rest = lst[5:]
        if rest:
            tags.append({"tag": f"{len(rest)} version(s) plus ancienne(s)", "published": None,
                         "recent": sum(e["recent"] for _, e in rest),
                         "total": sum(e["total"] for _, e in rest), "latest": False})
        for t in tags:
            t["share"] = round(t["recent"] / recent_total * 100, 1) if recent_total > 0 else None
        versions.append({"repo": r, "recent_total": recent_total, "tags": tags})
    versions.sort(key=lambda v: -v["recent_total"])

    # --- releases : rythme et adoption
    idx_of = {ts: i for i, ts in enumerate(ts_list)}
    rel_series, rel_w7 = {}, {}
    for ts, cur, _, _ in snaps:
        acc = {}
        for (r, t, a), d in cur.items():
            acc[(r, t)] = acc.get((r, t), 0) + d
        for rt, c in acc.items():
            rel_series.setdefault(rt, []).append((ts, c))
    for (r, t, a), g in w7.items():
        rel_w7[(r, t)] = rel_w7.get((r, t), 0) + g

    def cum_at(series, limit):
        value = None
        for ts, c in series:
            if ts > limit:
                break
            value = c
        return value

    releases = []
    for (r, t), e in per_rt.items():
        pub, series = e["pub"], rel_series[(r, t)]
        first_seen = series[0][0]
        i = idx_of[first_seen]
        # adoption mesurable seulement si la release a été vue dès sa publication
        born = pub is not None and i > 0 and pub > ts_list[i - 1]

        def after(days_n):
            if born and last_ts >= pub + days_n * DAY and first_seen <= pub + days_n * DAY:
                return cum_at(series, pub + days_n * DAY)
            return None

        age = (last_ts - pub) / DAY if pub is not None else None
        releases.append({"repo": r, "tag": t, "published": pub,
                         "age_days": None if age is None else round(age, 1), "total": e["total"],
                         "per_day": None if age is None else round(e["total"] / max(age, 1), 2),
                         "d7": after(7), "d30": after(30), "last7": rel_w7.get((r, t), 0)})
    releases.sort(key=lambda x: x["published"] or 0, reverse=True)

    # --- assets dormants (30 jours sans téléchargement), seulement si l'historique couvre 30 jours
    dormant = None
    if span >= 30:
        dormant = []
        for key, total in last_cur.items():
            pub = pubs.get(key)
            if w30.get(key, 0) == 0 and (pub is None or last_ts - pub >= 30 * DAY):
                last_active = last_gain_ts.get(key)
                dormant.append({"repo": key[0], "tag": key[1], "asset": key[2], "total": total,
                                "last_active": None if last_active is None else _iso_day(local_day(last_active))})
        dormant.sort(key=lambda x: -x["total"])
        dormant = dormant[:30]

    # --- étoiles, forks, abonnés, tickets
    stats_by_repo = {}
    for r in stat_rows:
        stats_by_repo.setdefault(r["repo"], []).append(r)
    repos = []
    for name, lst in stats_by_repo.items():
        last = lst[-1]

        def delta(field, days_n):
            base = next((x for x in reversed(lst) if x["ts"] <= last["ts"] - days_n * DAY), None)
            if base is None or base[field] is None or last[field] is None:
                return None
            return last[field] - base[field]

        repos.append({"repo": name, "stars": last["stars"], "forks": last["forks"],
                      "watchers": last["watchers"], "issues": last["issues"],
                      "stars_7": delta("stars", 7), "stars_30": delta("stars", 30),
                      "forks_7": delta("forks", 7), "forks_30": delta("forks", 30)})
    repos.sort(key=lambda x: -(x["stars"] or 0))

    return {"ready": True, "span_days": round(span, 1), "snapshots": len(snaps),
            "first_ts": first_ts, "last_ts": last_ts, "rhythm": rhythm, "days": days,
            "spikes": spikes, "spikes_ready": spikes_ready, "movers": movers, "platforms": platforms,
            "versions": versions, "releases": releases[:60], "dormant": dormant, "repos": repos}


def traffic_payload(owner, repo=None, days=30):
    """Vues et clones par jour, sites d'origine et pages les plus vues (14 derniers jours)."""
    empty = {"available": False, "days": [], "totals": {"views": 0, "view_uniques": 0, "clones": 0,
                                                       "clone_uniques": 0},
             "repos": [], "referrers": [], "paths": [], "last_ts": None, "stored_days": 0}
    if not owner:
        return empty
    conn = db_connect()
    try:
        where, params = "owner=?", [owner]
        if repo:
            where += " AND repo=?"
            params.append(repo)
        query, qp = ("SELECT repo, day, views, view_uniques, clones, clone_uniques "
                     f"FROM traffic_daily WHERE {where}"), list(params)
        if days > 0:
            start = datetime.datetime.now(datetime.timezone.utc).date() - datetime.timedelta(days=days - 1)
            query += " AND day>=?"
            qp.append(start.isoformat())
        daily = conn.execute(query + " ORDER BY day", qp).fetchall()
        stored = conn.execute(f"SELECT COUNT(DISTINCT day) AS n FROM traffic_daily WHERE {where}",
                              params).fetchone()["n"]
        refs = conn.execute("SELECT repo, referrer, count, uniques, ts FROM traffic_referrers "
                            f"WHERE {where}", params).fetchall()
        paths = conn.execute("SELECT repo, path, title, count, uniques, ts FROM traffic_paths "
                             f"WHERE {where}", params).fetchall()
    finally:
        conn.close()
    if not (stored or refs or paths):
        return empty

    keys = ("views", "view_uniques", "clones", "clone_uniques")
    per_day, per_repo = {}, {}
    totals = dict.fromkeys(keys, 0)
    for r in daily:
        d = per_day.setdefault(r["day"], dict.fromkeys(keys, 0))
        pr = per_repo.setdefault(r["repo"], dict.fromkeys(keys, 0))
        for k in keys:
            d[k] += r[k]
            pr[k] += r[k]
            totals[k] += r[k]
    day_list = []
    if per_day:
        a = datetime.date.fromisoformat(min(per_day))
        b = datetime.date.fromisoformat(max(per_day))
        for i in range((b - a).days + 1):
            day = (a + datetime.timedelta(days=i)).isoformat()
            day_list.append({"day": day, **per_day.get(day, dict.fromkeys(keys, 0))})

    ref_agg = {}
    for r in refs:
        e = ref_agg.setdefault(r["referrer"], {"referrer": r["referrer"], "count": 0, "uniques": 0})
        e["count"] += r["count"]
        e["uniques"] += r["uniques"]
    path_agg = {}
    for r in paths:
        e = path_agg.setdefault(r["path"], {"path": r["path"], "title": r["title"] or "", "count": 0, "uniques": 0})
        e["count"] += r["count"]
        e["uniques"] += r["uniques"]
    stamps = [r["ts"] for r in list(refs) + list(paths)]
    return {"available": True, "days": day_list, "totals": totals,
            "repos": sorted(({"repo": n, **v} for n, v in per_repo.items()), key=lambda x: -x["views"]),
            "referrers": sorted(ref_agg.values(), key=lambda x: -x["count"])[:15],
            "paths": sorted(path_agg.values(), key=lambda x: -x["count"])[:15],
            "last_ts": max(stamps) if stamps else None, "stored_days": stored}


def api_payload():
    cfg = public_config()
    payload = build_payload(cfg["owner"])
    payload["config"] = cfg
    payload["status"] = dict(STATE)
    return payload


def export_csv(out, history=False):
    owner = public_config()["owner"] or build_payload(None, fallback=True)["owner"]
    if not owner:
        sys.exit("Aucune donnée à exporter : lancez d'abord une collecte.")
    text, rows = csv_history(owner) if history else csv_latest(owner)
    with open(out, "w", newline="", encoding="utf-8") as f:
        f.write(text)
    print(f"{rows} lignes écrites dans {out}", file=sys.stderr)


# --------------------------------------------------------------------------
# Serveur web
# --------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="application/json", headers=None):
        if not isinstance(body, bytes):
            body = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _json_body(self):
        if "application/json" not in (self.headers.get("Content-Type") or ""):
            self._send(415, {"error": "Content-Type application/json requis."})
            return None
        length = int(self.headers.get("Content-Length") or 0)
        if length > 20000:
            self._send(413, {"error": "Requête trop volumineuse."})
            return None
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(body, dict):
                raise ValueError
            return body
        except ValueError:
            self._send(400, {"error": "JSON invalide."})
            return None

    def _series(self, query):
        by = (query.get("by") or ["total"])[0]
        if by not in ("total", "repo", "tag", "asset"):
            by = "total"
        try:
            days = max(0, min(int((query.get("days") or ["30"])[0]), 3650))
        except ValueError:
            days = 30
        repo = (query.get("repo") or [""])[0][:100] or None
        return series_payload(public_config()["owner"], by, repo, days)

    def _analytics(self, query):
        try:
            tz = max(-840, min(int((query.get("tz") or ["0"])[0]), 840))
        except ValueError:
            tz = 0
        repo = (query.get("repo") or [""])[0][:100] or None
        return analytics_payload(public_config()["owner"], repo, tz)

    def _traffic(self, query):
        try:
            days = max(0, min(int((query.get("days") or ["30"])[0]), 3650))
        except ValueError:
            days = 30
        repo = (query.get("repo") or [""])[0][:100] or None
        return traffic_payload(public_config()["owner"], repo, days)

    def _export(self, query):
        owner = public_config()["owner"]
        if not owner:
            return self._send(400, {"error": "Aucun compte configuré."})
        repo = (query.get("repo") or [""])[0][:100] or None
        history = (query.get("history") or [""])[0] in ("1", "true")
        # point-virgule + BOM : s'ouvre correctement dans Excel en français
        text, _ = csv_history(owner, repo, ";") if history else csv_latest(owner, repo, ";")
        safe_owner = re.sub(r"[^A-Za-z0-9._-]", "_", owner)
        kind = "historique" if history else "releases"
        name = f"github-downloads-{safe_owner}-{kind}-{datetime.date.today().isoformat()}.csv"
        self._send(200, text.encode("utf-8-sig"), "text/csv; charset=utf-8",
                   {"Content-Disposition": f'attachment; filename="{name}"'})

    def do_GET(self):
        url = urlparse(self.path)
        path, query = url.path, parse_qs(url.query)
        if path in ("/", "/index.html"):
            self._send(200, (HERE / "index.html").read_bytes(), "text/html; charset=utf-8")
        elif path == "/api/data":
            self._send(200, api_payload())
        elif path == "/api/series":
            self._send(200, self._series(query))
        elif path == "/api/analytics":
            self._send(200, self._analytics(query))
        elif path == "/api/traffic":
            self._send(200, self._traffic(query))
        elif path == "/api/export.csv":
            self._export(query)
        elif path == "/healthz":
            self._send(200, {"ok": True})
        else:
            self._send(404, b"not found", "text/plain")

    def do_POST(self):
        path = self.path.split("?")[0]
        if path == "/api/refresh":
            if not public_config()["owner"]:
                return self._send(400, {"error": "Aucun compte configuré."})
            return self._send(202, {"started": start_collect_async()})
        if path == "/api/config":
            body = self._json_body()
            if body is None:
                return
            try:
                restart = update_config(body)
            except ValueError as e:
                return self._send(400, {"error": str(e)})
            started = start_collect_async() if restart else False
            return self._send(200, {"config": public_config(), "started": started})
        self._send(404, b"not found", "text/plain")

    def log_message(self, *args):
        pass


def serve(host, port):
    threading.Thread(target=scheduler, daemon=True).start()
    print(f"Dashboard sur http://{host}:{port}", file=sys.stderr, flush=True)
    ThreadingHTTPServer((host, port), Handler).serve_forever()


# --------------------------------------------------------------------------
def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("collect", help="prend un relevé des compteurs")
    c.add_argument("owner", nargs="?")
    c.add_argument("--include-forks", action="store_true")
    c.add_argument("--no-traffic", action="store_true", help="ne relève pas le trafic ni les abonnés")

    s = sub.add_parser("serve", help="lance le dashboard web")
    s.add_argument("--host", default="0.0.0.0")
    s.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8080")))

    e = sub.add_parser("export", help="exporte le dernier relevé en CSV")
    e.add_argument("--out", default="downloads.csv")
    e.add_argument("--history", action="store_true",
                   help="exporte tous les relevés et pas seulement le dernier")

    args = p.parse_args()
    if args.cmd == "collect":
        cfg = public_config()
        owner = args.owner or cfg["owner"]
        if not owner:
            p.error("indiquez le compte (argument OWNER ou variable GHDL_OWNER)")
        try:
            collect(owner, args.include_forks or cfg["include_forks"],
                    traffic=cfg["traffic"] and not args.no_traffic)
        except urllib.error.HTTPError as err:
            sys.exit(http_error_message(err))
        except RuntimeError as err:
            sys.exit(str(err))
    elif args.cmd == "serve":
        serve(args.host, args.port)
    elif args.cmd == "export":
        export_csv(args.out, args.history)


if __name__ == "__main__":
    main()
