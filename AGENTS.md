# Savr-back — Mémo rapide (pour humains & Cursor)

## Backend (Django / DRF)

### Venv obligatoire

```bash
cd "/path/to/Savr-back"
source venv/bin/activate
```

### Installation

```bash
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
python manage.py migrate
```

### Démarrage

```bash
python manage.py runserver
```

### Chat agent (WebSocket obligatoire)

Le chat mobile utilise `ws/chat/`. Avec Channels installé, `runserver` suffit en dev.
Pour un serveur dédié ASGI :

```bash
daphne -b 0.0.0.0 -p 8000 savr_back.asgi:application
```

Redis doit tourner (channel layer + locks chat). Celery optionnel pour les titres auto.

### Accès depuis un téléphone (Expo Go)

```bash
python manage.py runserver 0.0.0.0:8000
```

Le front doit utiliser `API_BASE_URL=http://<IP_LOCALE>:8000/api` (même IP pour REST et WS).

## Lint & tests

Toute installation de dépendances passe par `sfw` (Socket Firewall) :

```bash
uv venv .venv --python 3.12 && sfw uv pip install --python .venv/bin/python -r requirements-dev.txt
```

Lint / format (ruff, config dans `pyproject.toml`, migrations exclues) :

```bash
make lint        # ruff check + ruff format --check
make fmt         # autofix + format
```

Tests (pytest + pytest-django + xdist, settings `savr_back.settings_test`) — nécessitent un Postgres avec pgvector :

```bash
make test-db                  # Postgres+pgvector éphémère (docker-compose.test.yml, port 55432, tmpfs)
make test                     # suite rapide (exclut @slow), --reuse-db ; N=auto → parallèle (xdist)
make test-all                 # tout, y compris @slow
make test-domain d=shopping   # auth | mealplan | posts | shopping | dietary | chat | imports | recipes | appversion
make test-changed             # tests liés aux fichiers modifiés (vs BASE=origin/main)
make test-fresh               # recrée les bases de test (si une migration existante a été modifiée)
```

Règles de la suite :
- aucun appel réseau : `conftest.py` bloque toute connexion hors base de test (le test échoue s'il tente un accès) ; les clés externes (Gemini, embeddings, Apify, S3, MS Graph) sont vidées par `settings_test`. Mocker le LLM / scraper au niveau du module appelant (`recipes.tasks.formalize_recipe`, …).
- Celery n'exécute rien implicitement : appeler la tâche directement (`process_recipe_import_from_url(str(id))`) ou capturer les `.delay()` avec la fixture `celery_delay`.
- fixtures partagées (`conftest.py`) : `make_user` (validé par défaut), `user`, `api.as_user(u)`, `make_recipe`, `make_meal_plan`, `make_shopping_list`, `follow`.
- un fichier par domaine (`test_<domaine>_*.py`) → marqueur de domaine appliqué automatiquement.
- tests API orientés règles métier et permissions, pas de CRUD trivial.

## « J'ai cuisiné » et définition de « cuisiné » (stats)

Source unique : `recipes/services/cooked_service.py` → `cooked_meal_plans(user, start, end)`.

- **Cuisiné = `MealPlan` possédé par l'utilisateur avec `confirmed=True`** ; date = `MealPlan.date`,
  contenu = recettes liées (`meal_plan_recipe_batches`) et/ou plat libre (`Post.label` / `custom_label`).
  Le `Post` est optionnel (log privé possible, `is_published=False`). Les invités ne « cuisinent » pas le repas de l'hôte.
- Chemins qui posent `confirmed=True` : `POST /posts/cooked/` (canonique, 1 tap, photo optionnelle),
  `POST /meal-plans/{id}/publish-post/`, `POST /recipe-batches/{id}/publish-post/`, `POST /posts/{id}/publish/`,
  fin du mode cuisine (`complete_cooking`, `cooking-progress/{id}/complete/`), `POST /meal-plans/{id}/confirm/`.
  `unmark_cooking` et `DELETE /posts/{id}/?undo_cooked=true` défont la trace.
- Ne jamais copier `confirmed` lors d'une duplication de repas (planifier ≠ cuisiner).

## Stats, badges et Récap (app `stats`)

Endpoints : `GET /api/stats/me/?period=week|month|year|all&year=2026`, `GET /api/stats/users/{id}/`
(sous-ensemble public, profil visible requis), `GET /api/badges/me/`, `GET /api/recap/{year}/`
(à partir de `RECAP_AVAILABLE_FROM`, défaut `12-01` ; staff toujours). `POST /posts/cooked/` renvoie `new_badges`.

- Calcul à la volée depuis `cooked_meal_plans()` (nombre de requêtes constant), cache par utilisateur
  (empreinte DB du « cuisiné » + version invalidée par miam/invitations, TTL 10 min).
- Plafonds doux : 4 cuisinés comptés par jour ; un même plat compte une fois par jour pour plat star,
  collections et badges ; 12 invités anonymes max par repas. Ingrédients « de base » exclus de la diversité.
- « Régalés » / « Hôte » / « invité N fois » : le foyer (`household_member_ids()`) n'est jamais un convive.
- Données : `IngredientProfile` (groupe, végétal, de base) et `Recipe.cuisine` (liste fermée `stats/cuisines.py`).
  Sans profil, les stats retombent sur le seed déterministe (`stats/food_groups.py`).

Commandes prod, dans l'ordre (idempotentes, reprenables) :

```bash
python manage.py classify_ingredients --only-missing   # seed + Gemini par lots de 50
python manage.py normalize_cuisines --only-missing     # synonymes + Gemini pour le reste
python manage.py backfill_badges                       # badges déjà mérités, sans push
```

Tests : `make test-domain d=stats` (LLM mocké via `stats.services.classification._call_llm`).

## « L'ardoise » : post enrichi (`stats/services/highlights.py`)

Les posts exposent `cuisine` ({code, label, flag, country} ou null) et `highlights` (≤ 3 puces `{kind, value, label, icon}`)
dans le feed, le détail et la réponse de `POST /posts/cooked/`. Aperçu sans écriture : `POST /api/posts/cooked/preview/`
(même corps). Contrat front : `.claude-work/specs/ardoise.md`.

- Puces positives seulement, jamais de calories ni de score santé (liste fermée `KINDS`) ; rareté d'abord
  (1re fois, nouveaux ingrédients, palier de série), puis végétaux / convives / durée mesurée par le mode cuisine.
- Calcul **à la date du post** (historique `cooked_meal_plans` strictement antérieur) ; instantané `Post.highlights`
  calculé paresseusement (feed : en lot, ≤ 7 requêtes constantes ; à chaud 0) et remis à null par les signaux `stats`
  (invités, recettes du repas, fin du mode cuisine). Changer les règles → incrémenter `SNAPSHOT_VERSION`.
- Backfill optionnel en prod : `python manage.py backfill_post_highlights`. Tests : `stats/tests/test_stats_highlights.py`.

## Panier « Cette semaine » et « À mon tour » (`recipes/services/basket_service.py`)

Envies **sans date** (recette ou plat libre) par semaine (lundi, fuseau utilisateur) : modèle `BasketItem`,
séparé de `MealPlan` (dont `date`/`meal_time` restent obligatoires, app 1.0.9 intacte). Contrat front :
`.claude-work/specs/basket.md`.

- API `/api/basket/` : liste par `?week=`, ajout, PATCH, suppression, `reorder/`, `{id}/schedule/` (POST/DELETE),
  `carry-over/`, `shopping-list/`. Statut dérivé (`todo`, `planned`, `cooked`, `carried_over`, `not_done`), jamais
  stocké ; rien n'est supprimé ni reporté automatiquement.
- Dater = rejoindre / créer un vrai `MealPlan` (créneau standard libre, sinon `other` + UUID : pas de conflit
  d'unicité). Cuisiner = `POST /posts/cooked/` avec `basket_item_id` (« cuisiné » reste `MealPlan.confirmed`) ;
  un plat logué sans id coche l'envie non datée correspondante de la semaine. `undo_cooked` restaure l'envie.
- Courses : `shopping_list_service.link_batch_to_shopping_list` (même fusion que repas → liste), idempotent ;
  l'envie garde son `RecipeBatch` (protégé des suppressions d'orphelins par `undo_cooked` / retrait de recette).
- « À mon tour » : `POST|DELETE /api/posts/{id}/my-turn/` → envie avec `inspired_by_post` + notif `a_mon_tour`
  (1 par ami et par post, agrégée). Posts : `my_turn_count`, `has_my_turn` (annotations du `PostViewSet`, 0 requête
  par post), `inspired_by`. Envie cuisinée → `Post.inspired_by_post` + notif `a_mon_tour_cooked` à l'auteur.
- Tests : `recipes/tests/test_mealplan_basket_api.py`, `recipes/tests/test_posts_my_turn_api.py`.

## Foyer + invités (`accounts/services/household_service.py`, `recipes/services/diners_service.py`)

Le foyer est réglé une fois et inclus par défaut dans chaque repas / envie, sans invitation. Contrat front :
`.claude-work/specs/household.md`.

- `HouseholdMember` : entrée **compte** (ami, consentie `pending` → `accepted`, puis réciproque) ou **nominative**
  (« Jules », `is_child`, partagée avec le foyer). API `/api/household/` (GET/PUT déclaratif, `members/`,
  `requests/{id}/accept|decline/`) ; notifs `household_request` / `household_accepted`.
- Convives = 1 + foyer non exclu (`excluded_household_ids` sur `MealPlan` / `BasketItem`) + `guest_count`
  + invitations acceptées / en attente (dédupliquées avec le foyer). **Source unique** :
  `diners_service` via `recipes.utils.get_meal_plan_people_count` (portions qui suivent, `total_servings`,
  courses) ; ne jamais recalculer `1 + participants + guest_count` ailleurs. Payloads : `diners`, `servings_suggested`.
- Envie non datée : `servings` vide = ses convives ; `POST /api/basket/diners/` = en-tête « Pour N » de la semaine.
- Foyer jamais compté comme convive (stats, badge « Hôte », ardoise) ; lien modifié → signaux `stats`.
- Tests : `recipes/tests/test_mealplan_household_api.py`, `stats/tests/test_stats_*` (foyer).

## Agent IA v2 (app `chat`)

Contrat front : `.claude-work/specs/agent-v2.md`. Modèle : `CHAT_AI_MODEL` (défaut `gemini-2.5-flash`, endpoint
OpenAI-compatible `GEMINI_OPENAI_BASE_URL`), indépendant de `AI_MODEL` (pipeline recettes).

- Prompt : `chat/services/agent_factory.py` (`AGENT_INSTRUCTIONS`, `AGENT_TOOLS`). Idées neuves d'abord (saison,
  différentes des plats récents), réutilise une recette proche (`search_recipes`) sinon plat libre / génération,
  agit sans confirmation pour le réversible, préférences strictes, jamais de calories.
- Contexte par tour : `session_context.build_session_context_prompt` (dates + « Profil culinaire » : saison
  `seasonality.py`, préférences + exceptions, foyer, panier avec `#id`, 10 derniers cuisinés avec `post id`,
  nouveautés du mois via `get_stats_payload`). Requêtes DB : le consumer l'appelle via `database_sync_to_async`
  une fois par tour. Sections plafonnées (`MAX_PROFILE_CHARS`) ; une section en erreur est omise.
- Tools v2 (`chat/services/kitchen_tools.py`, sync et testables ; wrappers dans `async_tools.py`) :
  `plan_week` (→ `basket_service.add_item` / `schedule_item`, conflits allergie / régime / goûts + allergies du
  foyer ignorés sauf `allow_conflicts`), `log_cooked` (→ `cooked_service.log_cooked`, `publish` par défaut =
  `auto_publish_cooked`, renvoie ardoise + `new_badges`), `add_to_shopping_list_from_basket`
  (→ `basket_service.items_for_shopping` + `add_to_shopping_list`), `remove_from_basket`, `undo_log_cooked`.
  Les 11 tools v1 restent exposés (app 1.0.9). Résultats : `card_type` + `message` + `undo` (`tool_schemas.py`).
- Tests : `chat/tests/test_chat_agent_v2.py` (aller-retour tool calling avec `chat.completions.create` mocké,
  sans réseau). Ne pas utiliser `django_db(transaction=True)` : il vide les données seedées par migration
  de la base réutilisée (`make test-fresh` pour réparer).

## Notifications (app `notifications`)

Registre unique : `notifications/registry.py` (un type = catégorie, modèles de titre/corps en français,
champs `data`, lien profond, regroupement, transactionnel ou non). **Aucune chaîne de notification
ailleurs** : passer par `notifications.services.notify(type, user, actor=…, post=…)` (in-app + push)
ou `push(type, user, …)` (push seule). Spec et contrat front : `.claude-work/specs/notifications.md`.

- Ton : tutoiement, phrases courtes, au plus un emoji, pas de « ! », jamais de culpabilisation
  (« tu vas perdre ta série », « ça fait longtemps » : interdit).
- Règles d'envoi : préférences par catégorie (`GET/PATCH /api/notifications/preferences/`), heures calmes
  (22:00–08:00 locale par défaut), plafond `NOTIFICATIONS_DAILY_CAP` (défaut 2) pour les non transactionnelles,
  idempotence via `dedupe_key`. L'in-app est toujours créée ; les réglages ne coupent que la push.
- Suivi : chaque push = une `NotificationDelivery` (`data.notification_id`) ; `POST /api/notifications/opened/`
  pose `opened_at`. Taux d'ouverture : `python manage.py notification_open_rates --days 30`.
- Rétention (Celery beat, toutes les heures, `CELERY_BEAT_SCHEDULE`) : digest des posts des amis (≤ 1/jour,
  18:00 locale) et récap du dimanche (19:00). Le beat tourne comme service Docker dédié `celery-beat` (jamais `worker -B`, jamais plus d'une instance) ; en local :
  `celery -A savr_back beat -l info`.
- Tests : `make test-domain d=notifications` (mocker `notifications.services.send_expo_push_notifications`).
