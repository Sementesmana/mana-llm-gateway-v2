# mana-llm-gateway-v2

> Proxy custom Flask unificado para Anthropic + OpenAI — Sementes Maná LTDA.
>
> ADR: `ManaVault/08-Decisoes/2026-06-29-gateway-unificado-llm-v2-anthropic-openai.md`
>
> **Status: scaffold local (alpha). NÃO DEPLOYADO.** Coexiste com `mana-llm-gateway` (LiteLLM, v1) que continua em produção.

## Por que existe

1. **Custo OpenAI invisível** — agente-router (Whisper) e agente-agronomo (TTS) chamavam `api.openai.com` direto, fora do cockpit. v2 expõe `/v1/audio/*` no mesmo gateway.
2. **LiteLLM footprint** — ~1 GB de RAM baseline = US$ 144/ano de Railway. v2 alvo: ~80 MB.
3. **Reversibilidade** — cada agente troca via env var. Voltar pra v1 = 2 linhas + restart.

## Como o agente aponta

**Anthropic (idêntico à v1):**
```bash
LLM_GATEWAY_URL=https://mana-llm-gateway-v2-production.up.railway.app/v1
LLM_GATEWAY_KEY=sk-mana-<chave virtual gerada>
```

3 rotas suportadas (espelha LiteLLM):
- SDK base_url: `Anthropic(base_url=LLM_GATEWAY_URL, api_key=LLM_GATEWAY_KEY)`
- HTTP cru: `POST /v1/messages` com header `x-api-key`
- OpenAI compat: `POST /v1/chat/completions` com header `Authorization: Bearer`

**OpenAI Whisper (agente-router):**
```bash
WHISPER_URL=https://mana-llm-gateway-v2-production.up.railway.app/v1/audio/transcriptions
OPENAI_API_KEY=sk-mana-<chave virtual> # mesma chave da gerada acima
```

**OpenAI TTS (agente-agronomo):**
```bash
TTS_URL=https://mana-llm-gateway-v2-production.up.railway.app/v1/audio/speech
OPENAI_API_KEY=sk-mana-<chave virtual>
```

## Reverter pra v1 (LiteLLM)

Trocar `LLM_GATEWAY_URL` e `LLM_GATEWAY_KEY` de volta pros valores antigos no painel Variables do Railway. Restart automático. Sem build, sem deploy.

Pra Whisper/TTS: trocar `WHISPER_URL`/`TTS_URL` de volta pra `api.openai.com/...` e `OPENAI_API_KEY` pra chave real OpenAI.

## Aliases canônicos

| Alias | Modelo real | Provider |
|---|---|---|
| `mana-rapido` | claude-haiku-4-5 | Anthropic |
| `mana-equilibrio` | claude-sonnet-4-6 | Anthropic |
| `mana-juridico` | claude-opus-4-6 | Anthropic |
| `mana-whisper` | whisper-1 | OpenAI |
| `mana-voz` | tts-1 | OpenAI |

## Endpoints

| Rota | Função |
|---|---|
| `POST /v1/messages` | Anthropic HTTP cru (pass-through fiel: cache_control, tools, vision) |
| `POST /v1/chat/completions` | OpenAI-compat → traduz pra Anthropic |
| `POST /v1/audio/transcriptions` | OpenAI Whisper pass-through + log de custo |
| `POST /v1/audio/speech` | OpenAI TTS pass-through + log de custo |
| `POST /key/generate` | Admin — cria chave virtual (Bearer MASTER_KEY) |
| `GET /health/liveliness` | Probe Railway |
| `GET /health/readiness` | DB + config checks |

## Schema gateway_v2 (isolado do v1)

```
gateway_v2.chaves_virtuais (id, alias, hash_chave, agente, limite_diario_usd, ativo, criado_em)
gateway_v2.uso_anthropic   (id, chave_id, agente, endpoint, modelo, tokens_in, tokens_out,
                            cache_write, cache_read, custo_usd, latencia_ms, status_code, created_at)
gateway_v2.uso_openai      (id, chave_id, agente, endpoint, modelo, segundos_audio, chars_in,
                            custo_usd, latencia_ms, status_code, created_at)
```

Schema isolado do `public` e do schema `gateway` do LiteLLM v1 (`LiteLLM_*`). **Zero risco de pisar nas tabelas do v1.** Quando v2 cumprir gate consumidor real (ADR 2026-06-28) e v1 for deprecada, unificação vira novo ADR.

## Deploy (decisões 2026-06-29)

1. **Repo:** `Sementesmana/mana-llm-gateway-v2` — **público** (alinha com decisão das habilidades)
2. **Service Railway:** novo service `mana-llm-gateway-v2` **no mesmo projeto do v1** (compartilha network interno e banco-mana via referência)
3. **Schema banco:** `gateway_v2` (isolado do `gateway` do v1)
4. **Env vars:** `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `DATABASE_URL` (apontando banco-mana), `MASTER_KEY`
5. **Healthcheck:** `/health/liveliness`
6. **Smoke E2E:** `POST /key/generate` → testar `/v1/messages` com mana-rapido → testar `/v1/audio/transcriptions` com áudio de 5s
7. **Gate consumidor real** (ADR 2026-06-28): 1 agente migra → 48h sem regressão → v2 vira `producao`

## TODO antes do deploy

- [ ] Testar pass-through de `cache_control` com PDF longo (agente-documentos)
- [ ] Testar `tools` (web_search) — se algum agente já usa
- [ ] Refinar cálculo de `segundos_audio` no Whisper (hoje é estimativa por tamanho)
- [ ] Rate limit por chave virtual (Fase 2)
- [ ] Streaming SSE no `/v1/messages` (Fase 2 — nenhum agente Maná pede hoje)
- [ ] Testes pytest (mínimo: auth, pass-through, custo)
- [ ] Smoke `init_db()` no Railway (idempotente)

## NÃO FAZER (regra do projeto)

- ❌ Apontar agente em produção pra v2 sem comparação visual + aval do Xayer
- ❌ Mexer no `mana-llm-gateway` v1 enquanto não cumprido gate consumidor real
- ❌ Hardcode de credencial — sempre env var no Railway
- ❌ Deploy via `railway up` — sempre `git push`
