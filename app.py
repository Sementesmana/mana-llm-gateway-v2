"""
mana-llm-gateway-v2 — Proxy custom Flask unificado (Anthropic + OpenAI).

ADR 2026-06-29-gateway-unificado-llm-v2-anthropic-openai.md

Princípios:
- Coexiste com LiteLLM (v1); agente escolhe v1 ou v2 via env var.
- Reversibilidade total: trocar URL no agente = voltar pra v1 em 30s.
- Footprint mínimo: Flask + httpx + psycopg2-binary. Sem ORM.
- Pass-through fiel: cache_control, tools, vision passam intactos.

Schema banco-mana: gateway_v2 (isolado do gateway do LiteLLM v1).
Decisões 2026-06-29: repo público, schema isolado, mesmo projeto Railway do v1.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import time
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import httpx
import psycopg2
from flask import Flask, Response, jsonify, request, stream_with_context
from psycopg2.extras import RealDictCursor

# ────────────────────────────────────────────────────────────────────────────
# Config (env vars — nada hardcode)
# ────────────────────────────────────────────────────────────────────────────
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
OPENAI_API_KEY    = os.environ.get("OPENAI_API_KEY", "")
DATABASE_URL      = os.environ.get("DATABASE_URL", "")
MASTER_KEY        = os.environ.get("MASTER_KEY", "")  # admin pra /key/generate

ANTHROPIC_BASE = "https://api.anthropic.com"
OPENAI_BASE    = "https://api.openai.com"

# Aliases canônicos Maná (espelha config.yaml do LiteLLM)
ALIASES_ANTHROPIC = {
    "mana-rapido":     "claude-haiku-4-5",
    "mana-equilibrio": "claude-sonnet-4-6",
    "mana-juridico":   "claude-opus-4-6",
}
ALIASES_OPENAI = {
    "mana-whisper": "whisper-1",
    "mana-voz":     "tts-1",
}

# Tabela de custo (USD por 1M tokens / por minuto de áudio)
# Atualizar quando preços mudarem. Fonte: anthropic.com/pricing + openai.com/pricing
CUSTOS_ANTHROPIC = {
    "claude-opus-4-6":   {"in": 15.00, "out": 75.00, "cache_write": 18.75, "cache_read": 1.50},
    "claude-sonnet-4-6": {"in":  3.00, "out": 15.00, "cache_write":  3.75, "cache_read": 0.30},
    "claude-haiku-4-5":  {"in":  0.80, "out":  4.00, "cache_write":  1.00, "cache_read": 0.08},
}
CUSTOS_OPENAI = {
    "whisper-1": {"por_minuto": 0.006},
    "tts-1":     {"por_1m_chars": 15.00},
}

app = Flask(__name__)
log = logging.getLogger("mana-llm-gateway-v2")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


# ────────────────────────────────────────────────────────────────────────────
# DB helpers (psycopg2 puro — sem ORM)
# ────────────────────────────────────────────────────────────────────────────
def _clean_db_url(url: str) -> str:
    """Remove ?schema=... da DATABASE_URL — esse parâmetro é só do Prisma
    (LiteLLM v1 usa Prisma). psycopg2 não entende e dá ProgrammingError.
    O schema correto é forçado via search_path no options."""
    if not url:
        return url
    parsed = urlparse(url)
    query_pairs = [(k, v) for k, v in parse_qsl(parsed.query) if k.lower() != "schema"]
    return urlunparse(parsed._replace(query=urlencode(query_pairs)))


def _db():
    """Conexão Postgres com search_path=gateway_v2,public.
    DATABASE_URL passa pelo strip de ?schema= (parâmetro Prisma)."""
    return psycopg2.connect(
        _clean_db_url(DATABASE_URL),
        options="-c search_path=gateway_v2,public",
    )


def init_db():
    """Cria schema + tabelas se não existirem. Idempotente."""
    if not DATABASE_URL:
        log.warning("DATABASE_URL ausente — banco desabilitado (proxy ainda funciona, mas sem log)")
        return
    ddl = """
    CREATE SCHEMA IF NOT EXISTS gateway_v2;

    CREATE TABLE IF NOT EXISTS gateway_v2.chaves_virtuais (
        id           SERIAL PRIMARY KEY,
        alias        TEXT NOT NULL,                -- ex: "agente-router"
        hash_chave   TEXT NOT NULL UNIQUE,         -- sha256 da chave (nunca a chave em plaintext)
        agente       TEXT NOT NULL,
        limite_diario_usd NUMERIC(10, 4),
        ativo        BOOLEAN NOT NULL DEFAULT TRUE,
        criado_em    TIMESTAMPTZ NOT NULL DEFAULT NOW()
    );

    CREATE TABLE IF NOT EXISTS gateway_v2.uso_anthropic (
        id           BIGSERIAL PRIMARY KEY,
        chave_id     INTEGER REFERENCES gateway_v2.chaves_virtuais(id),
        agente       TEXT NOT NULL,
        endpoint     TEXT NOT NULL,               -- /v1/messages | /v1/chat/completions
        modelo       TEXT NOT NULL,
        tokens_in    INTEGER NOT NULL DEFAULT 0,
        tokens_out   INTEGER NOT NULL DEFAULT 0,
        cache_write  INTEGER NOT NULL DEFAULT 0,
        cache_read   INTEGER NOT NULL DEFAULT 0,
        custo_usd    NUMERIC(10, 6) NOT NULL DEFAULT 0,
        latencia_ms  INTEGER,
        status_code  INTEGER,
        created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
    );

    CREATE TABLE IF NOT EXISTS gateway_v2.uso_openai (
        id              BIGSERIAL PRIMARY KEY,
        chave_id        INTEGER REFERENCES gateway_v2.chaves_virtuais(id),
        agente          TEXT NOT NULL,
        endpoint        TEXT NOT NULL,           -- /v1/audio/transcriptions | /v1/audio/speech
        modelo          TEXT NOT NULL,
        segundos_audio  NUMERIC(10, 2),
        chars_in        INTEGER,
        custo_usd       NUMERIC(10, 6) NOT NULL DEFAULT 0,
        latencia_ms     INTEGER,
        status_code     INTEGER,
        created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
    );

    CREATE INDEX IF NOT EXISTS idx_v2_uso_anthropic_agente_dia
        ON gateway_v2.uso_anthropic (agente, created_at DESC);
    CREATE INDEX IF NOT EXISTS idx_v2_uso_openai_agente_dia
        ON gateway_v2.uso_openai (agente, created_at DESC);
    """
    with _db() as con, con.cursor() as cur:
        cur.execute(ddl)
        con.commit()
    log.info("schema gateway_v2 inicializado (isolado do gateway do LiteLLM v1)")


def _autenticar(req) -> dict | None:
    """Resolve a chave virtual (Bearer ou x-api-key). Retorna registro da chave ou None."""
    chave = (
        (req.headers.get("Authorization", "") or "").replace("Bearer ", "").strip()
        or req.headers.get("x-api-key", "").strip()
    )
    if not chave:
        return None
    h = hashlib.sha256(chave.encode()).hexdigest()
    if not DATABASE_URL:
        # Modo dev sem banco: aceita qualquer chave não vazia
        return {"id": None, "alias": "dev", "agente": "dev", "ativo": True}
    with _db() as con, con.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            "SELECT id, alias, agente, ativo FROM gateway_v2.chaves_virtuais WHERE hash_chave=%s",
            (h,),
        )
        row = cur.fetchone()
    if not row or not row["ativo"]:
        return None
    return dict(row)


def _custo_anthropic(modelo: str, usage: dict) -> float:
    """Calcula custo a partir do bloco usage retornado pela Anthropic."""
    preco = CUSTOS_ANTHROPIC.get(modelo) or CUSTOS_ANTHROPIC.get("claude-haiku-4-5")
    tin    = usage.get("input_tokens", 0) or 0
    tout   = usage.get("output_tokens", 0) or 0
    cwrite = usage.get("cache_creation_input_tokens", 0) or 0
    cread  = usage.get("cache_read_input_tokens", 0) or 0
    return (
        tin    * preco["in"]          / 1_000_000
        + tout * preco["out"]         / 1_000_000
        + cwrite * preco["cache_write"] / 1_000_000
        + cread  * preco["cache_read"]  / 1_000_000
    )


# ────────────────────────────────────────────────────────────────────────────
# Routes — ANTHROPIC pass-through (3 variantes do ADR 2026-06-13)
# ────────────────────────────────────────────────────────────────────────────
@app.route("/v1/messages", methods=["POST"])
def messages():
    """HTTP cru Anthropic. Espelha api.anthropic.com/v1/messages."""
    auth = _autenticar(request)
    if not auth:
        return jsonify({"error": "unauthorized"}), 401

    body = request.get_json(force=True, silent=True) or {}
    modelo_pedido = body.get("model", "")
    modelo_real = ALIASES_ANTHROPIC.get(modelo_pedido, modelo_pedido)
    body["model"] = modelo_real

    t0 = time.time()
    try:
        with httpx.Client(timeout=120) as c:
            r = c.post(
                f"{ANTHROPIC_BASE}/v1/messages",
                json=body,
                headers={
                    "x-api-key": ANTHROPIC_API_KEY,
                    "anthropic-version": request.headers.get("anthropic-version", "2023-06-01"),
                    "content-type": "application/json",
                },
            )
        latencia = int((time.time() - t0) * 1000)
        data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        usage = data.get("usage", {}) if isinstance(data, dict) else {}
        custo = _custo_anthropic(modelo_real, usage) if r.status_code == 200 else 0

        if DATABASE_URL and auth.get("id"):
            with _db() as con, con.cursor() as cur:
                cur.execute(
                    """INSERT INTO gateway_v2.uso_anthropic
                       (chave_id, agente, endpoint, modelo, tokens_in, tokens_out,
                        cache_write, cache_read, custo_usd, latencia_ms, status_code)
                       VALUES (%s, %s, '/v1/messages', %s, %s, %s, %s, %s, %s, %s, %s)""",
                    (
                        auth["id"], auth["agente"], modelo_real,
                        usage.get("input_tokens", 0), usage.get("output_tokens", 0),
                        usage.get("cache_creation_input_tokens", 0),
                        usage.get("cache_read_input_tokens", 0),
                        custo, latencia, r.status_code,
                    ),
                )
                con.commit()

        return Response(r.content, status=r.status_code, content_type=r.headers.get("content-type"))
    except Exception as e:
        log.exception("erro proxy /v1/messages")
        return jsonify({"error": str(e)}), 500


@app.route("/v1/chat/completions", methods=["POST"])
def chat_completions():
    """Compat OpenAI → Anthropic. Tradução mínima do shape."""
    auth = _autenticar(request)
    if not auth:
        return jsonify({"error": "unauthorized"}), 401

    body = request.get_json(force=True, silent=True) or {}
    modelo_pedido = body.get("model", "")
    modelo_real = ALIASES_ANTHROPIC.get(modelo_pedido, modelo_pedido)

    # Tradução shape OpenAI → Anthropic
    mensagens = body.get("messages", [])
    system = "\n".join(m["content"] for m in mensagens if m.get("role") == "system")
    user_msgs = [m for m in mensagens if m.get("role") != "system"]
    anth_body = {
        "model": modelo_real,
        "max_tokens": body.get("max_tokens", 4096),
        "messages": user_msgs,
    }
    if system:
        anth_body["system"] = system

    t0 = time.time()
    with httpx.Client(timeout=120) as c:
        r = c.post(
            f"{ANTHROPIC_BASE}/v1/messages",
            json=anth_body,
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
        )
    latencia = int((time.time() - t0) * 1000)
    data = r.json() if r.status_code == 200 else {"error": r.text}

    # Tradução shape Anthropic → OpenAI
    if r.status_code == 200 and isinstance(data, dict):
        texto = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
        usage = data.get("usage", {})
        custo = _custo_anthropic(modelo_real, usage)
        if DATABASE_URL and auth.get("id"):
            with _db() as con, con.cursor() as cur:
                cur.execute(
                    """INSERT INTO gateway_v2.uso_anthropic
                       (chave_id, agente, endpoint, modelo, tokens_in, tokens_out, custo_usd, latencia_ms, status_code)
                       VALUES (%s, %s, '/v1/chat/completions', %s, %s, %s, %s, %s, %s)""",
                    (auth["id"], auth["agente"], modelo_real,
                     usage.get("input_tokens", 0), usage.get("output_tokens", 0),
                     custo, latencia, 200),
                )
                con.commit()
        return jsonify({
            "id": data.get("id", "msg_xxx"),
            "object": "chat.completion",
            "model": modelo_pedido,
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": texto},
                "finish_reason": data.get("stop_reason", "stop"),
            }],
            "usage": {
                "prompt_tokens": usage.get("input_tokens", 0),
                "completion_tokens": usage.get("output_tokens", 0),
                "total_tokens": usage.get("input_tokens", 0) + usage.get("output_tokens", 0),
            },
        })
    return Response(r.content, status=r.status_code, content_type=r.headers.get("content-type"))


# ────────────────────────────────────────────────────────────────────────────
# Routes — OPENAI pass-through (Whisper + TTS) — FECHA O GAP DE OBSERVABILIDADE
# ────────────────────────────────────────────────────────────────────────────
@app.route("/v1/audio/transcriptions", methods=["POST"])
def whisper():
    """Whisper STT — recebe multipart com áudio, devolve transcrição.
    Substitui chamada direta do agente-router.
    """
    auth = _autenticar(request)
    if not auth:
        return jsonify({"error": "unauthorized"}), 401

    if "file" not in request.files:
        return jsonify({"error": "campo 'file' ausente"}), 400

    audio = request.files["file"]
    modelo = request.form.get("model", "whisper-1")
    modelo_real = ALIASES_OPENAI.get(modelo, modelo)

    t0 = time.time()
    files = {"file": (audio.filename or "audio.ogg", audio.stream, audio.mimetype or "audio/ogg")}
    data = {k: v for k, v in request.form.items() if k != "model"}
    data["model"] = modelo_real

    with httpx.Client(timeout=120) as c:
        r = c.post(
            f"{OPENAI_BASE}/v1/audio/transcriptions",
            headers={"Authorization": f"Bearer {OPENAI_API_KEY}"},
            files=files, data=data,
        )
    latencia = int((time.time() - t0) * 1000)

    # Não temos duração real sem decodificar; estima por tamanho (rough)
    audio.stream.seek(0, 2)
    size_bytes = audio.stream.tell()
    audio.stream.seek(0)
    # Ogg/Opus rough: ~16KB/s; ajustar quando tiver dado real
    segundos_est = size_bytes / 16000
    custo = (segundos_est / 60) * CUSTOS_OPENAI["whisper-1"]["por_minuto"]

    if DATABASE_URL and auth.get("id"):
        with _db() as con, con.cursor() as cur:
            cur.execute(
                """INSERT INTO gateway_v2.uso_openai
                   (chave_id, agente, endpoint, modelo, segundos_audio, custo_usd, latencia_ms, status_code)
                   VALUES (%s, %s, '/v1/audio/transcriptions', %s, %s, %s, %s, %s)""",
                (auth["id"], auth["agente"], modelo_real, segundos_est, custo, latencia, r.status_code),
            )
            con.commit()

    return Response(r.content, status=r.status_code, content_type=r.headers.get("content-type"))


@app.route("/v1/audio/speech", methods=["POST"])
def tts():
    """TTS — texto → MP3. Substitui chamada direta do agente-agronomo."""
    auth = _autenticar(request)
    if not auth:
        return jsonify({"error": "unauthorized"}), 401

    body = request.get_json(force=True, silent=True) or {}
    modelo_pedido = body.get("model", "tts-1")
    modelo_real = ALIASES_OPENAI.get(modelo_pedido, modelo_pedido)
    body["model"] = modelo_real
    texto = body.get("input", "")
    chars = len(texto)

    t0 = time.time()
    with httpx.Client(timeout=120) as c:
        r = c.post(
            f"{OPENAI_BASE}/v1/audio/speech",
            headers={
                "Authorization": f"Bearer {OPENAI_API_KEY}",
                "Content-Type": "application/json",
            },
            json=body,
        )
    latencia = int((time.time() - t0) * 1000)
    custo = (chars / 1_000_000) * CUSTOS_OPENAI["tts-1"]["por_1m_chars"]

    if DATABASE_URL and auth.get("id"):
        with _db() as con, con.cursor() as cur:
            cur.execute(
                """INSERT INTO gateway_v2.uso_openai
                   (chave_id, agente, endpoint, modelo, chars_in, custo_usd, latencia_ms, status_code)
                   VALUES (%s, %s, '/v1/audio/speech', %s, %s, %s, %s, %s)""",
                (auth["id"], auth["agente"], modelo_real, chars, custo, latencia, r.status_code),
            )
            con.commit()

    return Response(r.content, status=r.status_code, content_type=r.headers.get("content-type"))


# ────────────────────────────────────────────────────────────────────────────
# Admin — chaves virtuais e probes
# ────────────────────────────────────────────────────────────────────────────
@app.route("/key/generate", methods=["POST"])
def gerar_chave():
    """Cria chave virtual nova. Auth via MASTER_KEY no Bearer."""
    if request.headers.get("Authorization", "") != f"Bearer {MASTER_KEY}":
        return jsonify({"error": "unauthorized"}), 401
    body = request.get_json(force=True, silent=True) or {}
    alias = body.get("alias") or body.get("key_alias")
    agente = body.get("agente", alias or "desconhecido")
    if not alias:
        return jsonify({"error": "alias obrigatório"}), 400

    # Gera chave pseudoaleatória
    chave_plain = "sk-mana-" + hmac.new(
        MASTER_KEY.encode(), f"{alias}-{time.time()}".encode(), hashlib.sha256
    ).hexdigest()[:40]
    h = hashlib.sha256(chave_plain.encode()).hexdigest()
    with _db() as con, con.cursor() as cur:
        cur.execute(
            """INSERT INTO gateway_v2.chaves_virtuais (alias, hash_chave, agente, limite_diario_usd)
               VALUES (%s, %s, %s, %s) RETURNING id""",
            (alias, h, agente, body.get("limite_diario_usd")),
        )
        chave_id = cur.fetchone()[0]
        con.commit()
    return jsonify({"id": chave_id, "alias": alias, "key": chave_plain, "agente": agente})


@app.route("/health/liveliness", methods=["GET"])
def liveliness():
    return "I'm alive!", 200


@app.route("/health/readiness", methods=["GET"])
def readiness():
    """Readiness real: ping banco + ping providers."""
    checks = {"db": False, "anthropic": False, "openai": False}
    if DATABASE_URL:
        try:
            with _db() as con, con.cursor() as cur:
                cur.execute("SELECT 1")
                checks["db"] = True
        except Exception as e:
            log.warning("db check fail: %s", e)
    # Não pingamos providers no readiness pra não gastar token; só validamos config
    checks["anthropic"] = bool(ANTHROPIC_API_KEY)
    checks["openai"] = bool(OPENAI_API_KEY)
    return jsonify(checks), 200 if all(checks.values()) else 503


# ────────────────────────────────────────────────────────────────────────────
# Boot
# ────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    init_db()
    porta = int(os.environ.get("PORT", "8080"))
    app.run(host="0.0.0.0", port=porta, debug=False)
else:
    init_db()
