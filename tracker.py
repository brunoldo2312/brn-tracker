"""
tracker.py — Tracker HTTP para descoberta de peers BRN (v3)
============================================================
v3:
  [FIX] Thread-safe (Lock em PEERS)
  [FIX] Cleanup thread (não depende de /peers ser chamado)
  [FIX] Rate limit por IP em /anunciar
  [FIX] Validação de porta (1-65535)
  [FIX] JSON inválido → 400 explícito (não silencia)
  [FIX] Evicção de peers antigos por TTL
  [NEW] Suporte a uuid/height/work (opcionais, retrocompatível)
  [NEW] /peers?full=1 retorna dicts com metadados
  [NEW] /stats com contadores
  [NEW] CORS habilitado
  [NEW] Thread daemon de cleanup sobe no import (funciona com gunicorn)
  [NEW] Bind default 0.0.0.0 (para deploy)
============================================================
"""
from flask import Flask, request, jsonify
from flask_cors import CORS
import os
import re
import time
import threading
import logging
from collections import defaultdict

# ============================================================
# CONFIG
# ============================================================
PORT              = int(os.environ.get("PORT", "10000"))
TTL               = int(os.environ.get("BRN_TRACKER_TTL", "600"))       # 10 min
CLEANUP_INTERVAL  = int(os.environ.get("BRN_TRACKER_CLEANUP", "60"))    # 1 min
MAX_PEERS         = int(os.environ.get("BRN_TRACKER_MAX_PEERS", "5000"))
RATE_LIMIT        = int(os.environ.get("BRN_TRACKER_RATE", "30"))       # req/min
RATE_WINDOW       = 60

ADDR_RE = re.compile(
    r"^("
    r"(?:\d{1,3}\.){3}\d{1,3}"                    # IPv4
    r"|"
    r"\[[0-9a-fA-F:]{2,45}\]"                     # IPv6 em brackets
    r"|"
    r"[a-zA-Z0-9](?:[a-zA-Z0-9\-\.]*[a-zA-Z0-9])?"  # hostname
    r")"
    r":(\d{1,5})$"
)

# ============================================================
# ESTADO
# ============================================================
app = Flask(__name__)
CORS(app)

_START_TS = time.time()

PEERS = {}                    # addr -> dict
PEERS_LOCK = threading.Lock()

_rate_buckets: dict = defaultdict(list)
_rate_lock = threading.Lock()
_rate_last_cleanup = [time.time()]

_cleanup_started = [False]
_cleanup_lock = threading.Lock()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("tracker")


# ============================================================
# HELPERS
# ============================================================
def _validar_endereco(addr: str) -> bool:
    """Valida 'host:port'. Aceita IPv4, hostname, IPv6 em brackets."""
    if not addr or len(addr) > 255:
        return False
    m = ADDR_RE.match(addr)
    if not m:
        return False
    try:
        porta = int(m.group(2))
        if porta < 1 or porta > 65535:
            return False
    except (ValueError, IndexError):
        return False
    return True


def _rate_cleanup(now: float) -> None:
    """Remove buckets inativos (chamado periodicamente)."""
    if now - _rate_last_cleanup[0] < 300:
        return
    with _rate_lock:
        for ip in list(_rate_buckets.keys()):
            b = _rate_buckets[ip]
            if not b or (now - b[-1]) > 3600:
                del _rate_buckets[ip]
        _rate_last_cleanup[0] = now


def _rate_ok(ip: str) -> bool:
    """Rate limit: RATE_LIMIT req por IP em RATE_WINDOW segundos."""
    agora = time.time()
    with _rate_lock:
        bucket = _rate_buckets[ip]
        bucket[:] = [t for t in bucket if agora - t < RATE_WINDOW]
        if len(bucket) >= RATE_LIMIT:
            return False
        bucket.append(agora)
    _rate_cleanup(agora)
    return True


def _cleanup_loop():
    """Remove peers expirados a cada CLEANUP_INTERVAL segundos."""
    while True:
        time.sleep(CLEANUP_INTERVAL)
        try:
            agora = time.time()
            with PEERS_LOCK:
                expirados = [k for k, v in PEERS.items()
                             if agora - v["last_seen"] > TTL]
                for k in expirados:
                    del PEERS[k]
                restantes = len(PEERS)
            if expirados:
                log.info("cleanup: removidos %d peers (restam %d)",
                         len(expirados), restantes)
        except Exception as e:
            log.error("cleanup erro: %s", e)


def _ensure_cleanup_started() -> None:
    """Sobe cleanup no import — funciona também sob gunicorn/waitress."""
    with _cleanup_lock:
        if _cleanup_started[0]:
            return
        _cleanup_started[0] = True
    threading.Thread(target=_cleanup_loop, daemon=True,
                     name="TrackerCleanup").start()
    log.info("cleanup thread iniciada (TTL=%ds, intervalo=%ds)",
             TTL, CLEANUP_INTERVAL)


_ensure_cleanup_started()


def _short(s: str, n: int = 8) -> str:
    if not s:
        return ""
    return s[:n] + "..." if len(s) > n else s


# ============================================================
# ROTAS
# ============================================================
@app.route("/anunciar", methods=["POST"])
def anunciar():
    ip = request.remote_addr or "?"

    if not _rate_ok(ip):
        return jsonify({"ok": False, "msg": "rate limit"}), 429

    try:
        data = request.get_json(force=True, silent=False) or {}
    except Exception as e:
        return jsonify({"ok": False, "msg": f"JSON invalido: {e}"}), 400

    if not isinstance(data, dict):
        return jsonify({"ok": False, "msg": "body deve ser objeto JSON"}), 400

    addr = (data.get("address") or "").strip()
    if not _validar_endereco(addr):
        return jsonify({
            "ok": False,
            "msg": "address invalido (formato host:port)",
        }), 400

    uuid   = (data.get("uuid") or data.get("node_id") or "").strip()[:64]
    try:
        height = int(data.get("height") or data.get("h") or 0)
    except (TypeError, ValueError):
        height = 0
    try:
        work = int(data.get("work") or data.get("cumulative_work") or 0)
    except (TypeError, ValueError):
        work = 0

    agora = time.time()
    with PEERS_LOCK:
        if len(PEERS) >= MAX_PEERS and addr not in PEERS:
            return jsonify({"ok": False, "msg": "tracker cheio"}), 503

        entry = PEERS.get(addr)
        if entry:
            entry["last_seen"] = agora
            entry["hits"] = entry.get("hits", 0) + 1
            if uuid:
                entry["uuid"] = uuid
            if height > 0:
                entry["height"] = height
            if work > 0:
                entry["work"] = work
        else:
            PEERS[addr] = {
                "last_seen":  agora,
                "first_seen": agora,
                "hits":       1,
                "uuid":       uuid,
                "height":     height,
                "work":       work,
            }
        total = len(PEERS)

    log.info("anunciar: %s uuid=%s h=%d (de %s) — total=%d",
             addr, _short(uuid) or "-", height, ip, total)

    return jsonify({
        "ok": True,
        "total": total,
        "ttl": TTL,
        "server_ts": int(agora),
    })


@app.route("/peers", methods=["GET"])
def peers():
    agora = time.time()
    with PEERS_LOCK:
        ativos = {
            k: dict(v) for k, v in PEERS.items()
            if agora - v["last_seen"] <= TTL
        }

    # Saída retrocompatível: lista de strings + info extra opcional
    lista_ips = list(ativos.keys())

    # Se o cliente pedir detalhes (?full=1), devolve lista de dicts
    full = request.args.get("full", "0") == "1"
    if full:
        peers_out = [
            {
                "address":   addr,
                "uuid":      v.get("uuid", ""),
                "height":    v.get("height", 0),
                "work":      v.get("work", 0),
                "last_seen": int(v.get("last_seen", 0)),
                "hits":      v.get("hits", 0),
            }
            for addr, v in ativos.items()
        ]
    else:
        peers_out = lista_ips

    return jsonify({
        "peers":     peers_out,
        "count":     len(ativos),
        "ttl":       TTL,
        "server_ts": int(agora),
    })


@app.route("/stats", methods=["GET"])
def stats():
    agora = time.time()
    with PEERS_LOCK:
        total  = len(PEERS)
        ativos = sum(1 for v in PEERS.values()
                     if agora - v["last_seen"] <= TTL)
    with _rate_lock:
        n_buckets = len(_rate_buckets)
    return jsonify({
        "total_registrados": total,
        "ativos":            ativos,
        "ttl":               TTL,
        "max_peers":         MAX_PEERS,
        "rate_limit":        RATE_LIMIT,
        "rate_buckets":      n_buckets,
        "uptime_s":          int(agora - _START_TS),
        "server_ts":         int(agora),
    })


@app.route("/health", methods=["GET"])
def health():
    return jsonify({
        "ok": True,
        "ts": time.time(),
        "uptime_s": int(time.time() - _START_TS),
    })


@app.route("/", methods=["GET"])
def raiz():
    agora = time.time()
    with PEERS_LOCK:
        ativos = sum(1 for v in PEERS.values()
                     if agora - v["last_seen"] <= TTL)
    return (
        f"BRN Tracker v3 — {ativos} peers ativos (TTL={TTL}s)\n"
        f"Endpoints:\n"
        f"  POST /anunciar        {{\"address\": \"ip:porta\", "
        f"\"uuid\": \"...\", \"height\": N}}\n"
        f"  GET  /peers           lista de strings\n"
        f"  GET  /peers?full=1    lista com metadados\n"
        f"  GET  /stats           estatísticas\n"
        f"  GET  /health          healthcheck\n"
    ), 200, {"Content-Type": "text/plain; charset=utf-8"}


# ============================================================
# MAIN (só para dev local — em prod use gunicorn/waitress)
# ============================================================
if __name__ == "__main__":
    log.info("🚀 BRN Tracker v3 — http://0.0.0.0:%d", PORT)
    log.info("   TTL=%ds | Cleanup=%ds | Max=%d | Rate=%d/min",
             TTL, CLEANUP_INTERVAL, MAX_PEERS, RATE_LIMIT)
    app.run(host="0.0.0.0", port=PORT, debug=False, threaded=True)
