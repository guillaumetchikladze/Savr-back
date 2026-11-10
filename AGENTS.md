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
(sous-ensemble public + badges obtenus `badges`, profil visible requis : suivi approuvé, sans blocage),
`GET /api/badges/me/[?all=1]` (`all=1` : tous les verrouillés avec progression), `GET /api/recap/{year}/`
(à partir de `RECAP_AVAILABLE_FROM`, défaut `12-01` ; staff toujours), `GET /api/recap/{year}/status/`
(`{year, available, available_from}`, sans calcul). `POST /posts/cooked/` renvoie `new_badges`.
Contrat front : `.claude-work/specs/gamification.md` (« API additions »).

- Calcul à la volée depuis `cooked_meal_plans()` (nombre de requêtes constant), cache par utilisateur
  (`stats_cache_token` = empreinte DB du « cuisiné » + version invalidée par miam/invitations, TTL 10 min).
  Récap : `recap.get_recap` met `build_recap` en cache 1 h (clé utilisateur / année / jour / même jeton).
- `top_dishes` (≤ 5 plats-jours, nombre + dernière date ; plats anonymes exclus) ; `cuisines_count` exclut
  « fusion », « autre » et les codes hors liste fermée (comptés dans `autre`).
- Plafonds doux : 4 cuisinés comptés par jour ; un même plat compte une fois par jour pour plat star,
  collections et badges ; 12 invités anonymes max par repas. Ingrédients « de base » exclus de la diversité.
- « Régalés » / « Hôte » / « invité N fois » : le foyer (`household_member_ids()`) n'est jamais un convive.
- Données : `IngredientProfile` (groupe, végétal, de base) et `Recipe.cuisine` (liste fermée `stats/cuisines.py`).
  Sans profil, les stats retombent sur le seed déterministe (`stats/food_groups.py`).

Commandes prod, dans l'ordre (idempotentes, reprenables) :

```bash
python manage.py classify_ingredients --only-missing   # seed + Gemini par lots de 50
python manage.py normalize_cuisines --only-missing     # synonymes + Gemini pour le reste
python manage.py backfill_badges                       # badges déjà mérités, sans push (+ rareté)
```

Tests : `make test-domain d=stats` (LLM mocké via `stats.services.classification._call_llm`).

### Badges loufoques (`stats/services/badges_quirky.py`, spec `.claude-work/specs/badges-quirky.md`)

51 badges en familles (`badge_types.FAMILIES` : même recette, calendrier, heures, défis absurdes, à plusieurs,
matériel) + 5 météo (phase 2), en plus des 28 historiques. Contrat front : fin de la spec (« Contrat front »).

- Registre : `BadgeDef` (`badge_types.py`) = code, titre, emoji, `description` (condition), `phrase` drôle, `family`,
  `secret` + `hint`, `rarity_guess`, `feature_flag`, `async_only`. Copie : tutoiement, jamais culpabilisant, jamais
  « bande » ni nom d'app de référence (test `test_registry_is_big_unique_and_well_written`).
- Évaluation : `compute_stats(user, 'all')` garde repas / plats-jours / ingrédients / convives dans le `StatsResult` ;
  `badge_history.history_of(stats)` les enrichit une fois (heure locale de saisie = 1er `Post.created_at` dans le
  fuseau `NotificationPreference.timezone`, durées `CookingProgress`, « À mon tour », invitations, tablées, cuisine)
  en lectures paresseuses d'une requête chacune. ~19 requêtes constantes pour 20 ou 200 repas (test).
  Un même plat compte une fois par jour ; convives = cuisinier + invités hors foyer + anonymes plafonnés.
- Couleurs (« Arc-en-ciel », « Monochrome orange ») : table statique `stats/badge_colors.py` (`colour_of`).
- Rareté : `BadgeRarity` (beat quotidien `stats-badge-rarity` → `compute_badge_rarity`) = % des cuisiniers actifs
  (≥ 1 « cuisiné » sur `BADGE_RARITY_ACTIVE_DAYS` = 90 j) ; paliers légendaire < 1 % < très rare < 5 % < rare
  < 20 % < commun ; sous `BADGE_RARITY_MIN_ACTIVE_USERS` (20) actifs, palier = estimation du registre. Relu en cache 1 h.
- Payloads (`/badges/me/`, `new_badges`, badges d'un autre) : champs historiques + `family`, `family_label`, `phrase`,
  `secret`, `hint`, `rarity_percent`, `rarity_tier`, `rarity_label`, `rarity_text`, `celebration` (normal|rare|secret).
  Secrets non obtenus : absents de l'aperçu, « ??? » + indice (code opaque `secret_…`) dans `?all=1`. `families` :
  compteurs par famille. Carte à partager : `GET /api/badges/<code>/share/` (badge obtenu, sinon 404).
- Anti-spam : un badge dont `achieved_on` a plus de `BADGE_NOTIFY_MAX_AGE_DAYS` (7) jours n'est ni notifié ni renvoyé
  dans `new_badges` (rattrapage silencieux). Rare / secret frais → notif `badge_rare` (« Badge secret débloqué : … »,
  même plafond, même payload historique que `new_badge`) au lieu de `new_badge`. `backfill_badges` : sans notification,
  recalcule la rareté à la fin (`--skip-rarity`).
- Météo (phase 2, `WEATHER_BADGES_ENABLED` défaut False) : ville opt-in `GET|PUT|DELETE /api/weather/city/`
  (`User.city` + coordonnées, migration accounts 0029, jamais de GPS) ; géocodage et météo Open-Meteo
  (`stats/services/weather.py`, archive > 5 jours sinon prévision) **uniquement en tâche** (`geocode_user_city_task`,
  `evaluate_user_badges`) ; cache `WeatherDay` par (lieu arrondi 0,01°, date). Badges `async_only` : jamais évalués
  dans une requête. `PUT` limité à `WEATHER_CITY_UPDATES_PER_USER_DAY` (10) par jour → 429 `rate_limited` ; même ville
  déjà géocodée = aucune tâche.
- Non faits : « Passeport tamponné » (doublon de `cuisines_20`) ; « Ton badge le plus rare » du Récap.
- Tests : `stats/tests/test_stats_badges_quirky.py`, `test_stats_badges_weather.py` (réseau mocké :
  `stats.services.weather._get_json`).

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

## « Le carnet du plat » et « Classiques de tes amis » (`recipes/services/dish_service.py`)

Page d'un plat (historique, versions, amis) et rangée « Classiques » d'Accueil. Contrat front :
`.claude-work/specs/dish-page.md`. Aucun modèle ni migration : tout est calculé.

- **Identité d'un plat — source unique `dish_key()`** : recette → `r-<recipe_id>` (pas de relation de copie
  sur `Recipe` ; l'import d'une URL déjà importée et « À mon tour » réutilisent la même recette) ; sinon plat
  libre → `l-<slug>` (`normalize_label` : `fold`, articles de tête retirés, « aux » → « au », -s/-x final des
  mots > 3 lettres : « Les pâtes au pesto » = « pâte au pesto » → `l-pate-au-pesto`). Une recette l'emporte
  toujours sur le libellé. Un repas à plusieurs recettes appartient à chacune ; le plat principal d'un post
  (`dish`) = recette de couverture, sinon 1re recette du repas, sinon `Post.label`, sinon `custom_label`.
  Les stats (`Meal.dish_key`, 1re fois de l'ardoise) utilisent la même normalisation des libellés.
- `GET /api/dishes/<dish_key>/` (+ `dishes/recipe/<id>/`, `dishes/label/<texte|slug>/`) : `dish`, `image_url`,
  `mine` (fois = jours distincts, 1re/dernière date, durée moyenne **mesurée par le mode cuisine**, version
  préférée = plus de miams), `versions` (mes repas cuisinés, paginés `?page=&page_size=` ≤ 30, `nth_label`
  « 3e fois », post : photo, commentaire, ardoise), `friends` (amis = suivis hors blocages, posts **visibles**
  via `post_visibility.visible_posts` ; compte, ≤ 12 amis, ≤ 10 derniers posts), `classic`, `basket`
  (corps prêt pour `POST /api/basket/` + `in_basket`). Inconnu, ou recette privée d'un autre jamais croisée → 404.
  Requêtes constantes (14 à chaud, test).
- `GET /api/dishes/classics/?week=` : plats cuisinés par ≥ `DISH_CLASSICS_MIN_FRIENDS` (3) amis distincts,
  posts visibles, sur `DISH_CLASSICS_WINDOW_DAYS` (30) jours finissant le dimanche de la semaine (au plus
  aujourd'hui) ; tri amis distincts puis récence, ≤ `DISH_CLASSICS_MAX` (5), avatars ≤ 5 ; cache par
  utilisateur `DISH_CLASSICS_CACHE_SECONDS` (300 s). Feed principal inchangé (app 1.0.9).
- Liens : `dish` ({key, title, recipe_id} ou null) sur `PostSerializer` / `PostListSerializer` (feed, détail,
  réponse de `POST /posts/cooked/`) et l'aperçu `cooked/preview/` ; 0 requête en plus sur le feed (test qui
  compare avec `get_dish` neutralisé). `top_dishes` de `/api/stats/me/` porte `dish_key` (cache stats `v3`).
- Tests : `recipes/tests/test_posts_dish_api.py`.

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

## Densité : « Tes amis avant ton premier plat » (invitations, onboarding social, Suggestions)

Contrat front : `.claude-work/specs/density.md`. Pas d'import de contacts, pas de géolocalisation.

- Liens d'invitation (`accounts/services/invite_service.py`, `/api/invites/`) : `me/` (code actif créé à la
  demande, `url` = `INVITE_BASE_URL/i/<code>`, défaut `https://tchikook.fr`), `me/rotate/`, `redeem/`,
  `<code>/` (aperçu public, débit limité par IP). `POST /api/auth/register/` accepte `invite_code` (un code invalide
  ne bloque jamais l'inscription ; bloc `invite` dans la réponse). Codes 10 car. sans ambiguïté (≈ 50 bits),
  plafond `INVITE_CODE_MAX_USES` (50) par code, limites de débit en cache.
- Utiliser un code : amitié **mutuelle** (`follow_service.make_mutual_friends`), sortie de la liste d'attente
  + `AllowedEmail` si `INVITE_BYPASSES_WAITLIST` (défaut True), notif `invite_joined` (in-app `follow`, lue par
  l'app 1.0.9), trace `InviteRedemption` (1 par paire, `is_acquisition` = arrivée grâce au lien).
- Ami = suivi approuvé (hors blocages) ; ami actif = a cuisiné (`cooked_meal_plans_of_users`) sur 14 jours ;
  cible 3 (`recipes/services/density_service.py`).
- `GET /api/onboarding/social/` : compteurs, `has_given_miam`, `invite`, `invited_by`, `suggested_people`
  (`accounts/feed_suggestions.build_onboarding_people_suggestions` : invitations > te suit > amis d'amis,
  complété par les suggestions existantes du feed), `first_miam_posts` (≤ 3 posts d'amis sans ton miam).
  `display_name` (prénom sinon pseudo, `public_display_name`) sur `invited_by`, chaque personne suggérée et son
  `mutual_preview` ; requêtes constantes quel que soit le nombre de suggestions (avatar seul, jamais tout le
  `UserSerializer` par personne ; `.only()` inclut `first_name`). Test 2 vs 10 : `test_posts_feed_queries_api.py`.
- `GET /api/feed/suggestions/` : endpoint **séparé** (feed principal inchangé pour l'app 1.0.9), posts publiés
  **et** `Post.is_public` (partage communauté opt-in par post, défaut False, via `POST /posts/cooked/` ou
  `PATCH /posts/{id}/update-content/`), hors amis / soi / blocages, `suggested: true` ; vide (`active: false`)
  dès 3 amis actifs. Un post `is_public` s'ouvre (`retrieve`) et accepte miam / « À mon tour » hors amis (sauf blocage).
- Visibilité d'un post : **source unique** `recipes/services/post_visibility.py` (`visible_posts`,
  `get_visible_post_or_404`), appliquée à toute action `/api/posts/{id}/…` ; invisible → **404** (jamais 403).
  `VIEW` (détail, miam + liste, « À mon tour », signaler, repost) = soi, suivi de l'auteur, invité au repas /
  co-cuisiné, `is_public`, ou reposté par un compte suivi ; `FRIENDS` (commentaires : liste, création, likes)
  = idem sans `is_public` ni repost d'ami. Toujours : publié, auteur actif, aucun blocage dans un sens ou l'autre
  (commentaires, miams et co-cuisiniers des comptes bloqués masqués). Écritures (`update`, `destroy`, `publish`,
  `update-content`, photos) : auteur seulement, quels que soient les paramètres. Tests :
  `recipes/tests/test_posts_visibility_api.py`.
- Feed réutilisable : `PostViewSet.feed_list_queryset(request, qs)` (préchargements + annotations, pas de N+1).
  État du viewer (relations de suivi auteurs / co-cuisiniers, invitations → `can_repost`, reposts) chargé en lot
  par page (`recipes/services/post_viewer_context.py`, via `PostPageListSerializer`) : requêtes **constantes**
  quelle que soit la taille de page (feed 14, profil 16, Suggestions 15, détail 13 quel que soit le nombre de
  co-cuisiniers). Tests 2 vs 20 posts : `recipes/tests/test_posts_feed_queries_api.py` (`FEED_QUERIES_DEBUG=1 -s`
  affiche l'origine de chaque requête). Ne jamais appeler `get_follow_relation` / une requête par post dans un serializer de post.
- Mesure : `python manage.py density_report --days 30 [--json]` (% ≥ 3 amis à J7, miams donnés en semaine 1,
  invités vs autres).
- Tests : `accounts/tests/test_auth_invites_api.py`, `recipes/tests/test_posts_density_api.py`.

## « Les tablées » (app `tables`) et « Je l'ai déjà » (`recipes/services/pending_confirmations.py`)

Contrat front : `.claude-work/specs/tables.md`. Jamais de classement ni de compteur individuel.

- Modèles `Table` (nom, emoji, couleur) / `TableMember` (`owner|member`, `invited|accepted`, `share_basket`
  défaut False). `TABLE_MAX_MEMBERS` 8 (invitations en attente comprises), `TABLE_MIN_MEMBERS` 2 (push),
  `TABLES_MAX_PER_USER` 5 (création + acceptation). Invitation = ami·e de l'inviteur (suivi dans un sens ou
  l'autre, sans blocage), consentie (`accept`/`decline`, refus = ligne supprimée) ; owner renomme / retire /
  supprime, tout le monde part (rôle transmis au plus ancien, dernier départ = suppression). Lien d'invitation
  portant une tablée : non fait (V1).
- API `/api/tables/` (liste + invitations), `{id}/` (GET semaine `?week=`, PATCH, DELETE), `invite/`, `accept/`,
  `decline/`, `leave/`, `members/{user_id}/` (DELETE), `me/` (`share_basket`). Non membre → 404.
- Semaine (`tables.services.table_detail_payload`) : `cooked_members` / `member_count` / `headline`
  (`cooked_meal_plans_of_users`), plats (`dish_key`) et cuisines distincts **collectifs**, `latest_post` par
  membre = dernier post **visible par le viewer** (`post_visibility.visible_posts`, DISTINCT ON), rituel
  « Qui cuisine quoi cette semaine ? » = envies non datées des membres `share_basket`.
- Blocage : appartenance inchangée ; les deux membres ne se voient plus dans la tablée (membres, posts, panier),
  comptés dans le collectif ; invitations en attente entre eux annulées (`cancel_invitations_between`).
- Notifs (catégorie `tables`) : `table_invitation`, `table_joined`, `table_weekly` (dimanche, tâche du récap
  `send_weekly_recaps_task` → `retention.send_weekly_recaps_and_tables`, 1/semaine, jamais à 0, plafonnée).
- « Je l'ai déjà » : `GET /api/cooked/pending-confirmations/[?preview=1]` (≤ 3 repas d'aujourd'hui / hier, non
  confirmés, sans post, créneau passé : `scheduled_time` + `COOKED_PENDING_GRACE_MINUTES`, sinon 10:00 / 14:00 /
  21:00, fuseau des préférences), `cooked_payload` prêt pour `POST /posts/cooked/` ; « Pas fait » =
  `POST|DELETE …/{meal_plan|basket_item}/{id}/dismiss/` (`CookedConfirmationDismissal`, l'envie reste au panier).
  Push `cooked_pending` (beat horaire `send_cooked_pending_reminders_task`, 21:00 locale, 1/soir, payload
  compatible `cooked_prompt` pour l'app 1.0.9, clé `cooked_prompt:{meal_plan_id}` partagée avec la push de fin
  de cuisson, qui ignore aussi les « Pas fait »).
- Tests : `tables/tests/test_tables_api.py`, `recipes/tests/test_posts_pending_confirmations_api.py`,
  `notifications/tests/test_notifications_tables_pending.py`.

## « Plan de bataille » : ordre des étapes multi-plats (`recipes/services/battle_plan*.py`)

Repas à ≥ 2 recettes ou batch cooking du panier : dans quel ordre faire les étapes. Contrat front :
`.claude-work/specs/battle-plan.md`. Lecture seule, aucun modèle ni migration.

- `POST /api/battle-plan/` : **une** source parmi `meal_plan_id` (repas accessible : propriétaire ou invité),
  `basket_item_ids` (envies de **son** panier ; plats libres → `skipped`), `recipe_ids` (publiques, à soi ou d'un
  repas où l'on est invité) ; + `servings`, `start_time` **ou** `target_ready_time` (« 20:00 », « 20h30 » ou ISO ;
  défaut : `MealPlan.scheduled_time`), `polish` (défaut true). Inaccessible → 404 ; 2 sources / aucune → 400
  `invalid_source` ; ≤ `MAX_RECIPES` (8).
- **Déterministe** (`battle_plan.build_plan`, pur, testable sans base). Données : `Step.order/title/instruction`,
  `has_timer`/`timer_duration`, `step_ingredients`, `Recipe.prep_time/cook_time`. Pas de champ actif / passif,
  équipement ni température : **déduits du texte** (durées « 1 h 30 », « 20 à 25 min », « une nuit » ; « 180 °C »,
  « th. 6 » ; mots-clés four / repos / refroidir / mariner / mijoter / lever / frigo ; « en remuant », « louche par
  louche » = actif). Durée absente → estimée depuis `prep_time` / `cook_time` (`estimated: true`).
- Contraintes : étapes d'une recette dans l'ordre (« Pendant ce temps… » et préchauffage : début → début), un seul
  cuisinier (actifs sans chevauchement), un four (températures différentes à ±10 °C → jamais ensemble + `conflicts`).
  Liste par priorité « plus long chemin restant » (le gâteau qui doit refroidir part en premier), puis chaque recette
  est décalée au plus tard (tout prêt ensemble), préchauffage juste avant sa cuisson. Repos / marinade ≥ 4 h,
  « une nuit », marinade ≥ 2 h → `day_before` (hors `total_duration_min`). `sequential_duration_min` = recettes
  enchaînées. Prépa commune (`shared_prep`) : ingrédient non basique taillé (émincer, hacher, râper…) dans ≥ 2
  recettes, quantités additionnées à l'échelle des portions.
- Reformulation LLM optionnelle (`battle_plan_polish`, `BATTLE_PLAN_LLM_ENABLED`, défaut False, forcé False en test) :
  endpoint OpenAI-compatible + `CHAT_AI_MODEL`, délai `BATTLE_PLAN_LLM_TIMEOUT_SECONDS` (6 s), sans nouvel essai ;
  ne reçoit que les textes numérotés (jamais l'ordre ni l'horaire), même nombre exigé, aucun chiffre inventé, sinon
  repli déterministe. Cache par (recettes + `updated_at` + portions, textes) `BATTLE_PLAN_POLISH_CACHE_SECONDS`.
- `has_battle_plan` (≥ 2 recettes, cache de prefetch, 0 requête) sur les repas : détail, liste complète, `timeline`,
  `by_date`, `by_week` / `by_dates` / `bulk` (pas en `minimal`). Tool agent v2 `battle_plan` (`card_type:
  battle_plan`, sans reformulation : le chat reformule déjà).
- Tests : `recipes/tests/test_mealplan_battle_plan_api.py` (mock LLM : `battle_plan_polish._call_llm`),
  `chat/tests/test_chat_agent_v2.py`.

## Sous-recettes « simplifié / compliqué » (`recipes/services/sub_recipe_service.py`)

Une ligne d'ingrédient (« pâte brisée » d'une quiche) peut pointer vers une recette : on l'achète (comportement
historique) ou on la fait maison. La bibliothèque devient un graphe. Contrat front : `.claude-work/specs/sub-recipes.md`.

- Modèle (migration additive 0082) : `RecipeIngredient.sub_recipe` (FK nullable, `SET_NULL`), `default_mode`
  (`buy` | `make`, défaut `buy`), `sub_recipe_ratio` (fournées de la sous-recette à ses portions de base pour la
  parente à ses portions de base ; 1 par défaut, 0.5 = moitié). Choix par préparation : `RecipeBatch.sub_recipe_modes`
  `{"<recipe_ingredient_id>": "make" | "buy"}` (lignes imbriquées comprises), absent → `default_mode`.
- Garde-fous (`validate_link`) : pas de A→A ni de boucle A→B→A (400 `cycle`), au plus `MAX_DEPTH` = 3 niveaux en
  comptant ceux au-dessus de la parente (400 `too_deep`). Expansion toujours bornée (chemin + profondeur).
- API : `PUT|PATCH|DELETE /api/recipe-ingredients/{id}/sub-recipe/` (auteur de la recette ; recette invisible → 404,
  d'un autre → 403 ; sous-recette publique ou à soi, sinon 404) ; `GET …/sub-recipe-suggestions/` (titres proches :
  trigrammes + titre normalisé, sans LLM, hors boucles) ; `GET|POST|PUT /api/recipe-batches/{id}/sub-recipe-modes/`
  (batch à soi / de ses repas / de ses envies ; PUT remplace) ; `GET|POST /api/basket/{id}/sub-recipe-modes/` (batch
  créé à la demande). Le panier expose `sub_recipe_modes`, `RecipeBatchLightSerializer` aussi.
- Courses : `add_batch_ingredients_to_list` passe par `expand_ingredients` (« faire maison » → ingrédients de la
  sous-recette × ratio du repas × `sub_recipe_ratio`, récursif, fusion habituelle par ingrédient + groupe d'unités).
  Changer un choix recalcule le batch dans sa liste (`resync_batch_shopping`, coches gardées, plafonnées).
- Plan de bataille : sous-recettes faites maison ajoutées avant leur parente (`with_sub_recipes`, ≤ 16 recettes) ;
  dépendance fin → début vers l'étape de la parente qui cite la ligne (sinon sa 1re étape), respectée par
  `_align_to_finish`. Payload additif : `recipes[].role|parent_recipe_id|line_id`, `steps[].waits_for_recipe_ids`.
  `POST /api/battle-plan/` accepte `sub_recipe_modes` (surcharge). `has_battle_plan` inchangé (≥ 2 recettes du repas).
  Mode cuisine (`/recipes/{id}/steps/`, `CookingProgress`) inchangé : le front passe par le plan de bataille.
- Lecture : détail `GET /api/recipes/{id}/` → `sub_recipes` (ligne, résumé, `default_mode`, `extra_time_min`,
  `extra_time_label` « + 25 min si tu la fais »), `total_time_if_made`, `difficulty_if_made` (tout fait maison).
  Lignes d'ingrédients : `sub_recipe` (résumé ou null), `default_mode`, `sub_recipe_ratio`. Chat : `search_recipes`
  renvoie `sub_recipes` (indice « tu peux l'acheter ou la faire maison »).
- Édition (`PUT /recipes/{id}/edit/`, révision) : `replace_recipe_ingredients` garde en place (id, lien, choix) les
  lignes liées dont l'ingrédient reste. Génération IA : ne remplit pas encore les liens (piste : après création,
  `suggest_sub_recipes` sur chaque ligne et lien si titre identique).
- Tests : `recipes/tests/test_recipe_sub_recipes_api.py`.

## « Ta cuisine » : équipements (`recipes/services/equipment_*.py`)

Onboarding « Ta cuisine », badge « Il te manque : un four », « Adapter pour chez moi ». Contrat front :
`.claude-work/specs/equipment.md`. Tout est additif (app 1.0.9 intacte).

- Catalogue pur `equipment_catalog.py` : 17 clés stables (`four`, `plaques` avec feux 1–6 + induction, `micro_ondes`,
  `robot_cuiseur`, `airfryer`, `barbecue_plancha`…), libellés, icônes Ionicons, `hard` (four, plaques, barbecue, gaufrier),
  alternatives ; « Tout le classique » = four, plaques 4 feux, micro-ondes, mixeur plongeant. `GET /api/equipment/catalog/`.
- Cuisine **par utilisateur** (`User.kitchen_equipment` + `kitchen_equipment_set_at`, migration accounts 0028) ;
  `GET|PUT /api/equipment/`, `has_set_equipment` (aussi dans le profil, soi seulement). Pas renseignée → cuisine du compte
  du foyer (lien accepté) le plus récent (`source: household`, 1 requête), sinon **inconnue** : aucun filtre ni badge.
  Évaluée une fois par requête : `equipment_service.request_kitchen(request)` / `viewer_kitchen(context)`.
- Recette (migration recipes 0083) : `required_equipment` (null = non calculé) = `required_equipment_override` (manuel) sinon
  inférence (`equipment_inference.infer_equipment`, mots-clés titre + étapes par phrase ; four / plaques / frigo partagés avec
  le plan de bataille via `step_resource`) ∪ `equipment_ai_hints` (champ `required_equipment` de `RecipeFormalized`, même passe
  LLM à l'import / génération). `equipment_alternatives` : four → airfryer seulement ≤ 4 portions. Recalcul par signaux
  (`recipes/equipment_signals.py` : `Step` save/delete, `Recipe` save hors `update_fields` sans rapport) regroupés dans
  `deferred_equipment_refresh()` (création, édition, révision IA, pipeline). Backfill : `python manage.py
  infer_recipe_equipment --only-missing` (idempotent, par lots, sans réseau).
- Lecture : `missing_equipment` (+ `missing_equipment_label`, `can_adapt_equipment`) sur `RecipeDetailSerializer`,
  `RecipeLightSerializer` (listes, recherches, repas) ; posts (feed, détail) : union des recettes du post + `adapt_recipe_id`,
  0 requête par post (feed 14, 15 si cuisine du foyer ; test `test_feed_missing_equipment_is_constant…`). Détail recette :
  `required_equipment`, `adapted_from`, `adapted_for_equipment`, `my_adaptation` (1 requête si quelque chose manque).
- Recherche (`search_fuzzy`, `search_semantic`, tool `search_recipes`) : `exclude_unfeasible` retire les recettes des autres
  qui demandent un équipement « dur » absent sans alternative (ses recettes restent ; `?equipment=any`) ; manques « mous »
  déclassés (`rank_penalty`, après les conflits de préférences). `EQUIPMENT_SEARCH_MODE` = exclude | rank | off.
- Plan de bataille : `battle_plan_service.kitchen_resources` → `bp.KitchenResources` (feux = capacité des étapes `hob`,
  2 fours = 2 températures en parallèle, pas de four + airfryer → `alternative_resource: airfryer`, une fournée à la fois) ;
  payload `kitchen`, `equipment_warnings`. Cuisine inconnue = comportement historique (un four, feux sans limite).
- « Adapter pour chez moi » (`equipment_adaptation.py`) : `POST /api/recipes/<id>/adapt-equipment/` **synchrone** (un appel
  OpenAI-compatible `CHAT_AI_MODEL`, `EQUIPMENT_ADAPT_LLM_TIMEOUT_SECONDS` 8 s, sans nouvel essai → jamais > ~10 s), copie
  privée liée (`adapted_from`, `adapted_for_equipment`), ingrédients copiés, étapes / temps réécrits, index de recherche repris
  de l'original ; idempotente par (recette, user, empreinte des équipements) = contrainte unique
  `recipe_unique_equipment_adaptation`. Réponse invalide / erreur / délai → 502 `adaptation_failed`, rien de créé.
  Appels LLM limités par utilisateur (`EQUIPMENT_ADAPTATIONS_PER_USER_HOUR`, défaut 10) → 429 `rate_limited` ;
  une adaptation existante est renvoyée sans LLM ni limite.
  `EQUIPMENT_ADAPT_ENABLED` (défaut True). Agent v2 : contexte « Cuisine… / Tu n'as pas : un four », règle du prompt, tool
  `adapt_recipe_equipment` (carte `recipe_adaptation`).
- Tests : `recipes/tests/test_recipe_equipment_api.py` (mock LLM : `equipment_adaptation._call_llm`),
  `recipes/tests/test_posts_feed_queries_api.py`, `chat/tests/test_chat_equipment.py`.

## Liens qui ouvrent l'app : aperçus publics et `share_url` (`recipes/views_public.py`)

Contrat : `.claude-work/specs/deep-links.md` §1. Le site (`tchikook-landing`, fonctions Vercel) rend `/i/<code>`,
`/r/<id>`, `/p/<id>`, `/u/<username>` avec balises Open Graph à partir de ces endpoints.

- `GET /api/public/recipes/<id>/`, `GET /api/public/posts/<id>/`, `GET /api/public/users/<username>/` : sans
  authentification (un jeton est ignoré, lecteur toujours anonyme), débit limité par IP (`PREVIEW_LIMIT_PER_IP`
  = 60/h, les trois confondus ; même cache que les invitations).
- IP client (`core/client_ip.get_client_ip`, toutes les limites par IP) : entrée de `X-Forwarded-For` ajoutée par
  le proxy de confiance = N-ième en partant de la **droite** (`TRUSTED_PROXY_COUNT`, défaut 1 = Caddy ; 0 =
  `REMOTE_ADDR`), jamais la plus à gauche (falsifiable). Ne jamais publier le port 8000 de Django.
- Accès serveur du site (`core/client_ip.public_rate_limited`, aperçus publics + `GET /api/invites/<code>/`) :
  vues de l'API, toutes les pages rendues par Vercel viennent de quelques IP. Le site envoie
  `X-Tchikook-Preview-Key` (= `PUBLIC_PREVIEW_SERVER_KEY`, comparaison à temps constant ; vide = désactivé) et
  `X-Tchikook-Visitor-IP` (IP du visiteur) → limite **par visiteur** (mêmes plafonds, compteur `<bucket>:visitor`
  séparé) sous un plafond global de la clé `PUBLIC_PREVIEW_SERVER_LIMIT_PER_HOUR` (5000/h). Clé absente ou
  fausse → limite par IP habituelle. Côté site : `TCHIKOOK_PREVIEW_KEY` (même valeur), runbook
  `Savr/docs/runbooks/deep-links.md`.
- Recette : `is_public` + auteur actif, sinon 404 ; au plus 5 ingrédients (noms), pas d'étapes. Post : publié +
  auteur actif, sinon 404 ; `is_public` → aperçu (titre, extrait, 1re photo, auteur, recette liée si publique),
  sinon teaser `{id, url, app_url, teaser: true, message}` sans contenu ni auteur. Profil : actif et validé, sinon
  404 ; `dishes_count` = repas cuisinés (`cooked_meal_plans`). Jamais d'email ni de préférences.
- `share_url` (recettes, posts, profil `UserSerializer`) et `url` : `core/public_links.py`, base
  `PUBLIC_WEB_BASE_URL` (vide → `INVITE_BASE_URL`, défaut `https://tchikook.fr`) ; `app_url` = `tchikook://recipe|post|user/<id>`.
  L'aperçu d'invitation expose aussi `inviter.display_name` (prénom sinon pseudo), comme le bloc `invite.inviter`
  des réponses de `POST /api/invites/redeem/` et `POST /api/auth/register/` (`public_display_name`).
- Copie : jamais le mot « bande » dans un texte visible (test `notifications/tests/test_notifications_copy.py`).
- Tests : `recipes/tests/test_recipe_public_previews_api.py`.

## Liste de courses hors-ligne : idempotence (`core/idempotency.py`)

Le front met en file les actions faites en magasin sans réseau et les rejoue. Contrat front : section
« Contrat front » de `.claude-work/status/*/ITEM-20261109-1300-bg03.md`.

- Décorateur réutilisable `@idempotent` (`core.idempotency`, sous `@action` le cas échéant) : avec
  `X-Idempotency-Key` (ou `Idempotency-Key`, ≤ 255 car.), une réponse **2xx** est mémorisée 48 h
  (`core.IdempotencyRecord` : user, clé, méthode, chemin, sha256 corps + query, statut, corps JSON) et
  rejouée telle quelle (en-tête `Idempotent-Replayed: true`) sans ré-appliquer. Même clé + autre
  méthode / chemin / corps → **409** `idempotency_key_reused`. Exception ou 4xx/5xx : rien n'est mémorisé.
  Doublons concurrents : insertion d'abord sur la contrainte unique (user, key), le second attend puis rejoue.
  Purge : beat `core-purge-idempotency-records` (toutes les heures) ; une clé expirée est réutilisable.
  Sur `update`, ne décorer que `update` (PATCH passe par `update`, sinon double réservation).
- Appliqué à : `shopping-list-items` `check/`, `uncheck/`, création, suppression, PUT/PATCH,
  `set_manual_quantity/`, `change_unit/` ; `shopping-lists` PUT/PATCH (nom), `archive/`, `unarchive/`.
- Idempotent même sans clé : `check/` = « mettre à coché » (ligne déjà cochée non réécrite, le premier
  `checked_by` reste), `uncheck/` = « mettre à décoché » ; `DELETE /shopping-list-items/{id}/` d'une ligne
  disparue → **204** (ligne existante d'une liste dont on n'est pas membre → 404) ; création avec `client_id`
  optionnel (≤ 64 car., unique par liste, `ShoppingListItemClientRef`) → un rejeu renvoie la ligne existante
  (200) sans ré-additionner la quantité.
- Archivage : `POST /shopping-lists/{id}/unarchive/` et `archive/` avec `{"is_archived": true}` (idempotents).
  `archive/` **sans corps = bascule, DÉPRÉCIÉ** (gardé pour l'app 1.0.9). Les listes archivées ne sont
  masquées que du `GET /shopping-lists/` (`include_archived=true`) : les actions de détail les atteignent.
- Tests : `recipes/tests/test_shopping_offline_idempotency_api.py`.

## Fiche produit des courses (`recipes/services/ingredient_info.py`)

« Toucher un article → ce que c'est ». Contrat front : section « Contrat front » de
`.claude-work/status/*/ITEM-20261110-0010-shp2.md`.

- Modèle `IngredientInfo` (migration recipes 0084, OneToOne `Ingredient.info`, **partagé** par tous) : `what`, `uses`,
  `aisle` (rayon, termes de supermarché français), `choose`, `keep`, `substitutes` (liste), `source` (llm | manual),
  `model`, `generated_at`. Admin : toute modification humaine passe la fiche en `manual`.
- `GET /api/ingredients/<id>/info/` (tout utilisateur validé) et `GET /api/shopping-list-items/<id>/info/` (membre
  de la liste, sinon 404). En base → 200 `cached: true` (1 lecture). Sinon génération **synchrone** : un appel
  OpenAI-compatible `INGREDIENT_INFO_MODEL` (défaut `gemini-3.5-flash`) ; nom de modèle refusé par le fournisseur
  (`openai.NotFoundError`, ou 400 qui cite le modèle) → avertissement en log, refus retenu 1 h en cache, **un**
  nouvel essai avec `CHAT_AI_MODEL` dans le temps restant. Budget total `INGREDIENT_INFO_LLM_TIMEOUT_SECONDS` (8 s),
  `max_retries=0`.
- Validation stricte (`parse_response`) : JSON objet, `what` 20–450 car., `uses` / `aisle` requis, longueurs
  bornées, ≤ 5 substitutions ; refus des allégations santé / régime (`FORBIDDEN_RE` : calories, kcal, maigrir /
  perte de poids, santé, « bon pour la santé / le cœur… », régime (sauf « régime de bananes »), vitamines /
  antioxydants, détox, termes médicaux), « bande », Strava. Faits culinaires neutres acceptés (« source de
  protéines végétales », « riche en protéines »). Le prompt demande toujours d'éviter la nutrition. Invalide, erreur, délai → **503 `info_unavailable`** +
  `Retry-After`, rien n'est stocké, pas de nouvel appel pour cet ingrédient pendant
  `INGREDIENT_INFO_FAILURE_COOLDOWN_SECONDS` (600 s). `INGREDIENT_INFO_LLM_ENABLED=False` : cache seul.
- Concurrence : verrou consultatif Postgres par ingrédient (`pg_try_advisory_lock`, inter-processus) ; une requête
  concurrente attend la ligne (≤ budget) au lieu d'appeler le LLM ; unicité `ingredient` + relecture sur
  `IntegrityError`. Débit par utilisateur (cache, par processus comme les invitations) :
  `INGREDIENT_INFO_GENERATIONS_PER_USER_HOUR` (30) générations, `INGREDIENT_INFO_READS_PER_USER_HOUR` (300) lectures
  → 429 `rate_limited`.
- Pré-génération : `python manage.py pregenerate_ingredient_info --limit 200 [--batch-size 20] [--sync] [--dry-run]`
  (ingrédients sans fiche les plus utilisés : lignes de recettes + de courses ; lots Celery
  `generate_ingredient_info_batch_task`, idempotent).
- Tests : `recipes/tests/test_shopping_ingredient_info_api.py` (mock : `ingredient_info._call_llm`, verrou :
  `_try_lock` / `_wait_for_info`).

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
  (→ `basket_service.items_for_shopping` + `add_to_shopping_list`), `remove_from_basket`, `undo_log_cooked`,
  `battle_plan` (→ `battle_plan_service`, « je fais X et Y ce soir, dans quel ordre ? »).
  Les 11 tools v1 restent exposés (app 1.0.9). Résultats : `card_type` + `message` + `undo` (`tool_schemas.py`).
- « Tout actionnable » (ag03, `chat/services/planning_tools.py`, sync et testables ; wrappers dans `async_tools.py`) :
  `import_recipe_from_url` (lien collé → job `process_recipe_import_from_url`, carte `recipe_import` : `status`,
  `status_path` = `GET /api/recipes/formalize/status/{request_id}/` à interroger, actions « Voir la recette » /
  « Ajouter à ma semaine » ; déjà importée → `success` + `recipe_id` sans job, `request_id` vide ; même lien en
  cours (15 min) → même job), `schedule_basket_item` (→ `basket_service.schedule_item`, carte `meal_scheduled`,
  undo `DELETE|POST /api/basket/{id}/schedule/`), `move_meal` (repas entier → `update_composer_slot`, l'envie
  liée suit la semaine via `basket_service.follow_meal_plan_week` ; un seul plat → `schedule_item` ; carte
  `meal_moved`, undo `PATCH /api/meal-plans/{id}/update-composer-slot/`), `remove_meal` (les plats redeviennent
  des envies sans date, repas vide supprimé ; refusé si invité·es, cuisiné ou photos ; carte `meal_removed`,
  undo par plat `POST /api/basket/{id}/schedule/`), `invite_to_meal` (ami·es seulement, nom ou prénom →
  `propose_meal_invitation` : seule action avec confirmation, carte `meal_invitation` + PendingAction comme
  `send_invitation_proposal`), `invite_to_table` (ami·es seulement → `tables.services.invite`, carte
  `table_invitation`, undo `DELETE /api/tables/{id}/members/{user_id}/` si créateur·rice). Toujours en
  vérifiant l'appartenance (envie / repas / tablée de l'utilisateur). `UndoHint.body` (nouveau, optionnel) :
  corps JSON à rejouer. Le profil liste les tablées (`#id`). Prompt : lien de recette collé → import direct ;
  réversible = sans confirmation. Cartes : `.claude-work/specs/agent-v2.md` § « Tout actionnable ».
- Tracing SDK Agents : désactivé par défaut (`chat/services/tracing.py`, appelé au démarrage et à l'import de
  `agent_factory`) ; opt-in via `AGENT_TRACING_ENABLED=true` (les traces contiennent des données utilisateur).
- Tests : `chat/tests/test_chat_agent_v2.py`, `chat/tests/test_chat_agent_actions.py` (aller-retour tool
  calling avec `chat.completions.create` mocké, sans réseau ; undo rejoués via l'API). Ne pas utiliser
  `django_db(transaction=True)` : il vide les données seedées par migration de la base réutilisée
  (`make test-fresh` pour réparer).

## Historique du chat : « Tes conversations » (`chat/services/history.py`, `conversation_title.py`)

Contrat front : section « Contrat front » de `.claude-work/status/*/ITEM-20261110-0020-ai02.md`.

- Existant avant : `GET|POST /api/chat/conversations/` (vides comprises, aperçu en N+1), `GET conversations/<id>/`,
  `GET conversations/<id>/messages/` (`offset` / `page_size`), titre Celery = 80 premiers caractères. Inchangé pour
  l'app 1.0.9 (mêmes champs), sauf : conversations supprimées masquées, requêtes constantes.
- `GET /api/chat/sessions/` : paginé (`page`, `page_size` ≤ 100), `-updated_at`, **seulement ses conversations avec
  au moins un message utilisateur**, non supprimées ; `id, title, title_source, created_at, updated_at,
  last_message_at, last_message_role, last_message_preview (≤ 120 car., JSON d'outils retiré), message_count`
  (user + assistant). Une requête pour la page (sous-requêtes ; test 3 vs 13). `POST` crée une conversation vide.
- `GET|PATCH|DELETE /api/chat/sessions/<id>/` (aussi sur `conversations/<id>/`, payload historique). PATCH
  `{"title"}` : 1–120 car. sinon 400 `invalid_title`, `title_source=user` (jamais réécrit), `updated_at` inchangé.
  PUT → 405. Autre utilisateur / supprimée → 404.
- **Suppression douce** : `Conversation.deleted_at` (migration chat 0004) ; masquée partout tout de suite (listes,
  messages, feedback, WS `join_conversation` / `user_message` → `not_found`, confirmation d'action), ce qui évite
  les écritures orphelines d'un stream ou d'une tâche en cours ; purge définitive (cascade messages / feedback /
  actions) après `CHAT_DELETED_RETENTION_DAYS` (30) par le beat `chat-purge-deleted-conversations` (04:50).
  Toujours filtrer avec `Conversation.objects.alive()`.
- Reprise : `GET /api/chat/sessions/<id>/messages/?latest=1&page_size=50` (les derniers, ordre chronologique) puis
  `?before=<id du plus ancien>` pour remonter ; `has_more` ajouté (aussi sur l'ancien endpoint).
- Titres (`title_source` default → auto → llm | user) : au 1er message, le consumer pose tout de suite
  `smart_title` (salutation retirée, 1re phrase ≥ 12 car., coupe au mot ≤ 60 car., URL seule → « Recette de
  site.fr ») et envoie `conversation_title_updated` (avec `conversation_id`). Après le 1er échange, la tâche
  `generate_conversation_title` (publiée par `core.celery_dispatch.publish`, jamais bloquante) fait **un** appel
  `CHAT_AI_MODEL` (`CHAT_TITLE_LLM_TIMEOUT_SECONDS` 5 s, sans nouvel essai, JSON validé : 3–60 car., ni
  santé / calories / « bande » / URL) ; échec → titre du message gardé. `CHAT_TITLE_LLM_ENABLED` (défaut True,
  False en test ; sans clé d'API : ignoré). Tours suivants : aucun appel LLM.
- Tests : `chat/tests/test_chat_history_api.py` (mock : `chat.services.conversation_title._call_llm`).

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
  18:00 locale), récap du dimanche (19:00) suivi de la push des tablées, rappel « Je l'ai déjà » (21:00). Le beat tourne comme service Docker dédié `celery-beat` (jamais `worker -B`, jamais plus d'une instance) ; en local :
  `celery -A savr_back beat -l info`.
- Tests : `make test-domain d=notifications` (mocker `notifications.services.send_expo_push_notifications`).

## E-mails et tâches depuis une requête (`emails`, `core/celery_dispatch.py`)

- Aucun appel réseau dans `register` / `login` / `password-reset/request` / `invites/redeem` : e-mails via
  `emails.services.enqueue_email` (ligne `EmailQueue` + `send_queued_email` sur `emails_urgent|high|normal|low`,
  publiée **au commit**) ; push « invite_joined » via `notify(..., defer_push=True)` → `notifications.tasks.send_push_task`.
- Sans MS_GRAPH_* : `enqueue_email` ne crée rien et renvoie None (aucune tentative réseau).
- Publier depuis une requête : `core.celery_dispatch.enqueue_on_commit(task, args=…, queue=…)` (ou `publish`) —
  publication bornée, broker absent → log, jamais d'exception. La tâche doit être `ignore_result=True` : sinon
  `apply_async` s'abonne au backend Redis et un Redis absent coûte ~19 s dans la requête.
- Aucun `.delay()` / `.apply_async()` direct depuis une requête, un service appelé par une vue ou un consumer :
  `publish` / `enqueue_on_commit`, ou `publish_tracked` (renvoie l'`task_id` généré en amont, None si le broker
  refuse) quand l'id est stocké (`RecipeImportRequest.task_id`, `meal_time_photo_reminder_task_id`).
- `CELERY_TASK_IGNORE_RESULT = True` (aucun `AsyncResult` lu : le suivi passe par `RecipeImportRequest`,
  `GET /recipes/formalize/status/{request_id}/`) ; `CELERY_BROKER_CONNECTION_TIMEOUT` et
  `CELERY_BROKER_TRANSPORT_OPTIONS` (socket_connect_timeout 1 s) bornent la connexion au broker.
- Import / génération de recette (formalize, `import_from_url`, `generate_from_idea`) : broker indisponible →
  demande marquée `error` (`error_message='queue_unavailable'`) et réponse **503** `{"error": "queue_unavailable"}`
  (jamais 500) ; outils chat : `ValueError` lisible par l'agent.
- Clients HTTP : délais explicites (connexion, lecture) — Graph/MSAL (5, 15), Expo (3, 10).
- Tests : `accounts/tests/test_auth_emails_api.py` (`celery_delay` + `django_capture_on_commit_callbacks`).
