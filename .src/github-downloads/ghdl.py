#!/usr/bin/env python3
"""GitHub Downloads - statistiques de téléchargement des assets de releases GitHub.

Relève les compteurs de tous les dépôts d'un compte (utilisateur ou
organisation), garde un historique dans SQLite et sert un dashboard web.
Le compte, le token et la fréquence se règlent depuis l'interface web.

    python ghdl.py serve                # dashboard (http://localhost:8080)
    python ghdl.py collect [OWNER]      # un relevé en ligne de commande
    python ghdl.py export [--out f.csv] # CSV du dernier relevé

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
import json
import os
import re
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

API = os.environ.get("GHDL_API", "https://api.github.com").rstrip("/")
HERE = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("GHDL_DATA", HERE))
DB_PATH = Path(os.environ.get("GHDL_DB", DATA_DIR / "downloads.db"))
CONFIG_PATH = DATA_DIR / "config.json"
OWNER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,38}$")

COLLECT_LOCK = threading.Lock()
STATE = {"collecting": False, "error": None, "last_attempt": None}

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
            "include_forks": bool(cfg.get("include_forks", False))}


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


def collect(owner, include_forks=False, quiet=False):
    token = get_token()
    ts = int(time.time())
    rows = []
    n_repos = 0
    for repo in list_repos(owner, token, include_forks):
        n_repos += 1
        name = repo["name"]
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
    conn.close()
    if not quiet:
        total = sum(r[5] for r in rows)
        print(f"{n_repos} dépôts, {len(rows)} assets, {total} téléchargements au total.",
              file=sys.stderr)
    return len(rows)


def run_collect(owner, include_forks):
    """Collecte protégée par un verrou ; mémorise l'état et l'éventuelle erreur."""
    if not COLLECT_LOCK.acquire(blocking=False):
        return False
    STATE.update(collecting=True, error=None)
    try:
        collect(owner, include_forks, quiet=True)
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
    threading.Thread(target=run_collect, args=(cfg["owner"], cfg["include_forks"]),
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
                    run_collect(cfg["owner"], cfg["include_forks"])
        except Exception as e:  # noqa: BLE001
            print(f"planificateur : {e}", file=sys.stderr)
        time.sleep(60)


def build_payload(owner, fallback=False):
    conn = db_connect()
    if not owner and fallback:
        row = conn.execute("SELECT owner FROM snapshots ORDER BY ts DESC LIMIT 1").fetchone()
        owner = row["owner"] if row else None
    base = {"owner": owner, "last_ts": None, "prev_ts": None, "assets": [], "history": []}
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
    history = [dict(r) for r in conn.execute(
        """SELECT ts, repo, SUM(downloads) AS downloads FROM snapshots
           WHERE owner=? GROUP BY ts, repo ORDER BY ts""", (owner,))]
    conn.close()
    base.update(last_ts=last, prev_ts=prev, assets=assets, history=history)
    return base


def api_payload():
    cfg = public_config()
    payload = build_payload(cfg["owner"])
    payload["config"] = cfg
    payload["status"] = dict(STATE)
    return payload


def export_csv(out):
    payload = build_payload(None, fallback=True)
    with open(out, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["repo", "tag", "asset", "downloads", "size", "published_at"])
        for a in sorted(payload["assets"], key=lambda x: -x["downloads"]):
            w.writerow([a["repo"], a["tag"], a["asset"], a["downloads"], a["size"], a["published_at"]])
    print(f"{len(payload['assets'])} lignes écrites dans {out}", file=sys.stderr)


# --------------------------------------------------------------------------
# Serveur web
# --------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="application/json"):
        if not isinstance(body, bytes):
            body = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
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

    def do_GET(self):
        path = self.path.split("?")[0]
        if path in ("/", "/index.html"):
            self._send(200, (HERE / "index.html").read_bytes(), "text/html; charset=utf-8")
        elif path == "/api/data":
            self._send(200, api_payload())
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

    s = sub.add_parser("serve", help="lance le dashboard web")
    s.add_argument("--host", default="0.0.0.0")
    s.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8080")))

    e = sub.add_parser("export", help="exporte le dernier relevé en CSV")
    e.add_argument("--out", default="downloads.csv")

    args = p.parse_args()
    if args.cmd == "collect":
        cfg = public_config()
        owner = args.owner or cfg["owner"]
        if not owner:
            p.error("indiquez le compte (argument OWNER ou variable GHDL_OWNER)")
        try:
            collect(owner, args.include_forks or cfg["include_forks"])
        except urllib.error.HTTPError as err:
            sys.exit(http_error_message(err))
        except RuntimeError as err:
            sys.exit(str(err))
    elif args.cmd == "serve":
        serve(args.host, args.port)
    elif args.cmd == "export":
        export_csv(args.out)


if __name__ == "__main__":
    main()
