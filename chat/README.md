# Chat Agent — Protocole WebSocket

## Connexion mobile

1. `Savr/.env` : `API_BASE_URL=http://<IP_LAN>:8000/api`
2. Backend : `python manage.py runserver 0.0.0.0:8000` (venv activé)
3. Redis local actif (`CHANNEL_REDIS_URL`)
4. Même IP pour REST (`/api/chat/`) et WS (`/ws/chat/`)

Le mobile dérive l'URL WS automatiquement : `http://IP:8000/api` → `ws://IP:8000/ws/chat/`.

## Endpoint

`ws/chat/?token=<JWT_ACCESS_TOKEN>`

## Client → Server

| action | Champs | Description |
|--------|--------|-------------|
| `join_conversation` | `conversation_id` | Rejoindre une room |
| `user_message` | `conversation_id`, `content` | Envoyer un message |
| `confirm_action` | `action_id` | Confirmer une mutation |
| `cancel_action` | `action_id` | Annuler une mutation |

## Server → Client

Champs communs : `turn_id`, `stream_phase` (`text` | `tool` | `mutation` | `complete`)

| type | Règle |
|------|-------|
| `text_block_start` | Début bloc texte |
| `assistant_delta` | Uniquement si `stream_phase=text` |
| `text_block_end` | Fin bloc avant tool/mutation |
| `tool_running` | Suspend les deltas |
| `tool_done` | Fin tool |
| `mutation_card` | Fin du tour agent |
| `action_executed` | Post-confirmation backend |
| `import_job_started` | Import URL lancé |
| `message_complete` | Fin tour → Celery titre |
| `conversation_title_updated` | Titre mis à jour |
| `error` | Erreur |

## Sécurité mutations

Les tools `execute_*` ne sont **pas** exposés au LLM. Flux :
1. Agent appelle `propose_*` → `PendingAction` en DB
2. Utilisateur confirme via WS → exécution service sync directe
3. `Message(system)` + `action_executed`

## Dépendances

- `openai-agents>=0.2.4` (fix streaming Gemini / logprobs)
- `openai>=1.0.0`

## Spike

```bash
python manage.py test_agent_stream --parallel 2
```

## Locks & rate limit

- Redis lock `chat_stream_{conversation_id}` : 1 stream actif
- Rate limit `chat_rate_{user_id}` : 10 messages/min
- `PendingAction` expiration : 24h
