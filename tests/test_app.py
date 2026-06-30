"""
Testes smoke do mana-llm-gateway-v2 — sem rede.

Não testa pass-through real Anthropic/OpenAI (precisaria mock ou sandbox).
Testa: aliases, cálculo de custo, autenticação, health.
"""
from __future__ import annotations

import os
import sys

# Garante import do app sem precisar de DATABASE_URL real
os.environ.setdefault("DATABASE_URL", "")
os.environ.setdefault("ANTHROPIC_API_KEY", "test")
os.environ.setdefault("OPENAI_API_KEY", "test")
os.environ.setdefault("MASTER_KEY", "test-master")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app import (  # noqa: E402
    ALIASES_ANTHROPIC,
    ALIASES_OPENAI,
    CUSTOS_ANTHROPIC,
    CUSTOS_OPENAI,
    _custo_anthropic,
    app,
)


# ──────────────────────────────────────────────────────────────────────
# Aliases canônicos
# ──────────────────────────────────────────────────────────────────────
def test_aliases_anthropic_existem():
    assert ALIASES_ANTHROPIC["mana-rapido"] == "claude-haiku-4-5"
    assert ALIASES_ANTHROPIC["mana-equilibrio"] == "claude-sonnet-4-6"
    assert ALIASES_ANTHROPIC["mana-juridico"] == "claude-opus-4-6"


def test_aliases_openai_existem():
    assert ALIASES_OPENAI["mana-whisper"] == "whisper-1"
    assert ALIASES_OPENAI["mana-voz"] == "tts-1"


# ──────────────────────────────────────────────────────────────────────
# Cálculo de custo
# ──────────────────────────────────────────────────────────────────────
def test_custo_anthropic_haiku_input_only():
    # 1M tokens de input no haiku = $0.80
    custo = _custo_anthropic("claude-haiku-4-5", {"input_tokens": 1_000_000, "output_tokens": 0})
    assert abs(custo - 0.80) < 0.001


def test_custo_anthropic_opus_input_output():
    # 1M input + 1M output no opus = $15 + $75 = $90
    custo = _custo_anthropic(
        "claude-opus-4-6",
        {"input_tokens": 1_000_000, "output_tokens": 1_000_000},
    )
    assert abs(custo - 90.0) < 0.001


def test_custo_anthropic_cache_hit():
    # 1M cache_read no sonnet = $0.30
    custo = _custo_anthropic(
        "claude-sonnet-4-6",
        {"cache_read_input_tokens": 1_000_000},
    )
    assert abs(custo - 0.30) < 0.001


def test_custo_anthropic_modelo_desconhecido_usa_haiku():
    # Modelo sem preço cadastrado cai no haiku (mais barato; conservador)
    custo = _custo_anthropic("claude-futuro-xx", {"input_tokens": 1_000_000})
    assert abs(custo - 0.80) < 0.001


def test_custos_anthropic_todos_tem_4_campos():
    for modelo, precos in CUSTOS_ANTHROPIC.items():
        assert "in" in precos, f"{modelo} sem preço 'in'"
        assert "out" in precos, f"{modelo} sem preço 'out'"
        assert "cache_write" in precos, f"{modelo} sem preço 'cache_write'"
        assert "cache_read" in precos, f"{modelo} sem preço 'cache_read'"


def test_custos_openai_whisper_tem_preco():
    assert CUSTOS_OPENAI["whisper-1"]["por_minuto"] == 0.006


def test_custos_openai_tts_tem_preco():
    assert CUSTOS_OPENAI["tts-1"]["por_1m_chars"] == 15.00


# ──────────────────────────────────────────────────────────────────────
# Autenticação
# ──────────────────────────────────────────────────────────────────────
def test_messages_sem_auth_retorna_401():
    cli = app.test_client()
    r = cli.post("/v1/messages", json={"model": "mana-rapido", "messages": []})
    # Sem DATABASE_URL e sem chave, retorna 401
    assert r.status_code == 401


def test_chat_completions_sem_auth_retorna_401():
    cli = app.test_client()
    r = cli.post("/v1/chat/completions", json={"model": "mana-rapido", "messages": []})
    assert r.status_code == 401


def test_whisper_sem_auth_retorna_401():
    cli = app.test_client()
    r = cli.post("/v1/audio/transcriptions")
    assert r.status_code == 401


def test_tts_sem_auth_retorna_401():
    cli = app.test_client()
    r = cli.post("/v1/audio/speech", json={"input": "ola", "model": "tts-1"})
    assert r.status_code == 401


def test_key_generate_sem_master_retorna_401():
    cli = app.test_client()
    r = cli.post("/key/generate", json={"alias": "agente-teste"})
    assert r.status_code == 401


def test_key_generate_master_errado_retorna_401():
    cli = app.test_client()
    r = cli.post(
        "/key/generate",
        json={"alias": "agente-teste"},
        headers={"Authorization": "Bearer master-errado"},
    )
    assert r.status_code == 401


# ──────────────────────────────────────────────────────────────────────
# Health
# ──────────────────────────────────────────────────────────────────────
def test_liveliness_responde_200():
    cli = app.test_client()
    r = cli.get("/health/liveliness")
    assert r.status_code == 200
    assert b"alive" in r.data.lower()


def test_readiness_responde():
    cli = app.test_client()
    r = cli.get("/health/readiness")
    # 200 se tudo ok, 503 se db ou key faltando
    assert r.status_code in (200, 503)
    data = r.get_json()
    assert "db" in data
    assert "anthropic" in data
    assert "openai" in data


# ──────────────────────────────────────────────────────────────────────
# Endpoints admin compat LiteLLM (consumidos pelo agente-monitor)
# ──────────────────────────────────────────────────────────────────────
def test_key_list_sem_master_retorna_401():
    cli = app.test_client()
    r = cli.get("/key/list")
    assert r.status_code == 401


def test_key_list_master_correto_retorna_200():
    cli = app.test_client()
    r = cli.get(
        "/key/list",
        headers={"Authorization": "Bearer test-master"},
    )
    # Sem DATABASE_URL real (modo teste) retorna lista vazia
    assert r.status_code == 200
    data = r.get_json()
    assert "keys" in data
    assert "total_pages" in data


def test_model_info_sem_master_retorna_401():
    cli = app.test_client()
    r = cli.get("/model/info")
    assert r.status_code == 401


def test_model_info_master_correto_lista_aliases():
    cli = app.test_client()
    r = cli.get("/model/info", headers={"Authorization": "Bearer test-master"})
    assert r.status_code == 200
    data = r.get_json()
    assert "data" in data
    aliases = [m["model_name"] for m in data["data"]]
    # Tem todos os aliases canônicos
    assert "mana-rapido" in aliases
    assert "mana-equilibrio" in aliases
    assert "mana-juridico" in aliases
    assert "mana-whisper" in aliases
    assert "mana-voz" in aliases
    # Modelo real correto pra haiku
    haiku = next(m for m in data["data"] if m["model_name"] == "mana-rapido")
    assert haiku["litellm_params"]["model"] == "claude-haiku-4-5"


def test_spend_logs_sem_master_retorna_401():
    cli = app.test_client()
    r = cli.get("/spend/logs")
    assert r.status_code == 401


def test_spend_logs_master_correto_retorna_200():
    cli = app.test_client()
    r = cli.get("/spend/logs", headers={"Authorization": "Bearer test-master"})
    # Sem DATABASE_URL retorna lista vazia, mas 200
    assert r.status_code == 200
    data = r.get_json()
    assert isinstance(data, list)
