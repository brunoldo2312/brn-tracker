# BRN Tracker

Tracker HTTP público para descoberta de peers da rede BRN.

## Endpoints

| Método | Endpoint | Descrição |
|---|---|---|
| POST | `/anunciar` | Nó se anuncia (JSON: `address`, `uuid`, `height`, `work`) |
| GET  | `/peers` | Lista de endereços (`["ip:port", ...]`) |
| GET  | `/peers?full=1` | Lista detalhada com metadados |
| GET  | `/stats` | Estatísticas do tracker |
| GET  | `/health` | Healthcheck |
| GET  | `/` | Info em texto |

## Como um nó usa

1. A cada 120s, o nó faz `POST /anunciar` com seu IP público + porta
2. A cada 120s, o nó faz `GET /peers` para descobrir outros nós
3. Conexões P2P (blocos, txs) são **diretas entre os nós**, o tracker só ajuda a descobrir

## Deploy no Render (grátis)

1. Fork este repo
2. New + → Web Service
3. Runtime: Python 3
4. Build: `pip install -r requirements.txt`
5. Start: `gunicorn tracker:app --workers 1 --threads 8 --timeout 60 --bind 0.0.0.0:$PORT`

## Configuração (variáveis de ambiente)

- `PORT` — porta (default 10000)
- `BRN_TRACKER_TTL` — tempo de vida de um peer em segundos (default 600)
- `BRN_TRACKER_CLEANUP` — intervalo de cleanup (default 60)
- `BRN_TRACKER_MAX_PEERS` — máximo de peers (default 5000)
- `BRN_TRACKER_RATE` — rate limit por IP/min (default 30)

## Testar localmente

```bash
pip install -r requirements.txt
python tracker.py
