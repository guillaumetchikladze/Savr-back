"""Tools async exposés à l'agent — wrappers database_sync_to_async."""

from agents import RunContextWrapper, function_tool
from channels.db import database_sync_to_async
from django.db import close_old_connections

from chat.services import kitchen_tools
from chat.services.agent_context import AgentContext
from chat.services.tool_schemas import (
    AddRecipeResult,
    AddShoppingListItemResult,
    BasketRemovalResult,
    BasketShoppingResult,
    BattlePlanResult,
    CreateMealPlanSlotResult,
    GetMealPlansResult,
    GetShoppingListItemsResult,
    ImportJobStarted,
    LogCookedResult,
    MutationProposal,
    PlanWeekResult,
    SearchRecipesResult,
    UndoCookedResult,
)
from recipes.services.invitation_service import (
    _normalize_meal_plan_ids,
    propose_meal_invitation,
    resolve_complices_by_name,
)
from recipes.services.meal_plan_service import (
    add_recipes_to_meal_plan,
    get_meal_plans_for_user,
    propose_meal_deletion_data,
)
from recipes.services.meal_plan_service import (
    create_meal_plan_slot as create_meal_plan_slot_sync,
)
from recipes.services.recipe_revision_service import propose_recipe_revision_data
from recipes.services.recipe_search_service import search_recipes_for_user
from recipes.services.shopping_list_service import (
    add_item_to_shopping_list,
    get_shopping_list_items_for_user,
)


async def _run_sync(fn, *args, **kwargs):
    result = await database_sync_to_async(fn)(*args, **kwargs)
    close_old_connections()
    return result


@function_tool(strict_mode=False)
async def search_recipes(
    ctx: RunContextWrapper[AgentContext],
    query: str,
    limit: int = 5,
) -> SearchRecipesResult:
    """Recherche des recettes par mots-clés (sémantique + trigram)."""
    user = ctx.context.user
    return await _run_sync(search_recipes_for_user, user, query, limit=limit)


@function_tool(strict_mode=False)
async def get_meal_plans(
    ctx: RunContextWrapper[AgentContext],
    start_date: str,
    end_date: str,
) -> GetMealPlansResult:
    """Liste les repas planifiés accessibles entre deux dates (YYYY-MM-DD)."""
    user = ctx.context.user
    plans = await _run_sync(get_meal_plans_for_user, user, start_date, end_date)
    return GetMealPlansResult(
        start_date=start_date,
        end_date=end_date,
        count=len(plans),
        meal_plans=plans,
    )


@function_tool(strict_mode=False)
async def propose_recipe_revision(
    ctx: RunContextWrapper[AgentContext],
    recipe_id: int,
    instruction: str,
    scope: str = 'full',
) -> MutationProposal:
    """Propose des modifications IA sur une recette (nécessite confirmation utilisateur)."""
    user = ctx.context.user
    return await _run_sync(propose_recipe_revision_data, user, recipe_id, instruction, scope)


@function_tool(strict_mode=False)
async def propose_meal_deletion(
    ctx: RunContextWrapper[AgentContext],
    meal_plan_id: int,
    recipe_batch_id: int,
) -> MutationProposal:
    """Propose de retirer une recette d'un créneau (nécessite confirmation utilisateur)."""
    user = ctx.context.user
    return await _run_sync(propose_meal_deletion_data, user, meal_plan_id, recipe_batch_id)


@function_tool(strict_mode=False)
async def send_invitation_proposal(
    ctx: RunContextWrapper[AgentContext],
    meal_plan_ids: list[int] | None = None,
    meal_plan_id: int | None = None,
    invitee_ids: list[int] | None = None,
    invitee_usernames: list[str] | None = None,
) -> MutationProposal:
    """Propose d'inviter des amis à un ou plusieurs repas (nécessite confirmation utilisateur)."""
    user = ctx.context.user
    raw_meal_plan_ids = list(meal_plan_ids or [])
    if meal_plan_id is not None:
        raw_meal_plan_ids.append(meal_plan_id)
    normalized_meal_plan_ids = _normalize_meal_plan_ids(raw_meal_plan_ids)
    if not normalized_meal_plan_ids:
        raise ValueError('Indique au moins un repas (meal_plan_ids).')

    resolved_invitee_ids: list[int] = []
    seen_invitee_ids: set[int] = set()
    for raw_id in invitee_ids or []:
        invitee_id = int(raw_id)
        if invitee_id in seen_invitee_ids:
            continue
        seen_invitee_ids.add(invitee_id)
        resolved_invitee_ids.append(invitee_id)

    if invitee_usernames:
        complices = await _run_sync(resolve_complices_by_name, user, invitee_usernames)
        for complice in complices:
            if complice.id in seen_invitee_ids:
                continue
            seen_invitee_ids.add(complice.id)
            resolved_invitee_ids.append(complice.id)

    if not resolved_invitee_ids:
        raise ValueError('Aucun ami trouvé (invitee_ids ou invitee_usernames requis).')

    return await _run_sync(propose_meal_invitation, user, normalized_meal_plan_ids, resolved_invitee_ids)


@function_tool(strict_mode=False)
async def create_meal_plan_slot(
    ctx: RunContextWrapper[AgentContext],
    date: str,
    meal_time: str,
    custom_label: str | None = None,
    scheduled_time: str | None = None,
) -> CreateMealPlanSlotResult:
    """Crée un créneau vide dans le planning (déjeuner, dîner, etc.) avant d'y ajouter des recettes."""
    user = ctx.context.user
    return await _run_sync(
        create_meal_plan_slot_sync,
        user,
        date,
        meal_time,
        custom_label,
        scheduled_time,
    )


@function_tool(strict_mode=False)
async def add_recipe_to_meal_plan(
    ctx: RunContextWrapper[AgentContext],
    meal_plan_id: int,
    recipe_ids: list[int],
) -> AddRecipeResult:
    """Ajoute des recettes à un créneau de planning (exécution immédiate)."""
    user = ctx.context.user
    return await _run_sync(add_recipes_to_meal_plan, user, meal_plan_id, recipe_ids)


@function_tool(strict_mode=False)
async def import_recipe_from_url(
    ctx: RunContextWrapper[AgentContext],
    url: str,
) -> ImportJobStarted:
    """Lance l'import asynchrone d'une recette depuis une URL externe."""
    user = ctx.context.user
    return await _run_sync(_start_import_from_url, user, url, ctx.context.conversation_id)


@function_tool(strict_mode=False)
async def get_shopping_list_items(
    ctx: RunContextWrapper[AgentContext],
    shopping_list_id: int,
    include_purchased: bool = False,
) -> GetShoppingListItemsResult:
    """Liste les articles restant à acheter dans une liste (temps réel, exclut le déjà coché)."""
    user = ctx.context.user
    return await _run_sync(
        get_shopping_list_items_for_user,
        user,
        shopping_list_id,
        include_purchased=include_purchased,
    )


@function_tool(strict_mode=False)
async def add_shopping_list_item(
    ctx: RunContextWrapper[AgentContext],
    shopping_list_id: int,
    ingredient_name: str,
    quantity: float = 1,
    unit: str = 'piece',
) -> AddShoppingListItemResult:
    """Ajoute un article à une liste de courses (exécution immédiate)."""
    user = ctx.context.user
    return await _run_sync(
        add_item_to_shopping_list,
        user,
        shopping_list_id,
        ingredient_name,
        quantity=quantity,
        unit=unit,
    )


@function_tool(strict_mode=False)
async def generate_recipe_from_idea(
    ctx: RunContextWrapper[AgentContext],
    idea: str,
    servings: int = 4,
) -> ImportJobStarted:
    """Génère une recette à partir d'une idée ou d'un concept (asynchrone)."""
    user = ctx.context.user
    return await _run_sync(_start_generate_from_idea, user, idea, servings, ctx.context.conversation_id)


@function_tool(strict_mode=False)
async def plan_week(
    ctx: RunContextWrapper[AgentContext],
    recipe_ids: list[int] | None = None,
    labels: list[str] | None = None,
    week: str | None = None,
    date: str | None = None,
    meal_time: str | None = None,
    servings: int | None = None,
    allow_conflicts: bool = False,
) -> PlanWeekResult:
    """Ajoute des plats au panier « Cette semaine » (sans date par défaut ; exécution immédiate, annulable).

    Args:
        recipe_ids: recettes existantes (ids issus de search_recipes ou d'une génération terminée).
        labels: plats libres sans fiche recette (ex. « Soupe de potiron »).
        week: n'importe quel jour (YYYY-MM-DD) de la semaine visée ; défaut : semaine en cours.
        date: YYYY-MM-DD seulement si l'utilisateur donne un jour précis : les plats sont alors datés.
        meal_time: avec date : breakfast, lunch ou dinner (défaut dinner).
        servings: portions voulues (défaut : convives du foyer).
        allow_conflicts: true seulement si l'utilisateur insiste pour un plat en conflit avec ses préférences.
    """
    user = ctx.context.user
    return await _run_sync(
        kitchen_tools.plan_week_for_user,
        user,
        recipe_ids=recipe_ids,
        labels=labels,
        week=week,
        date=date,
        meal_time=meal_time,
        servings=servings,
        allow_conflicts=allow_conflicts,
    )


@function_tool(strict_mode=False)
async def remove_from_basket(
    ctx: RunContextWrapper[AgentContext],
    basket_item_ids: list[int],
) -> BasketRemovalResult:
    """Retire des envies du panier de la semaine (annule un plan_week ; ids basket_item_id)."""
    user = ctx.context.user
    return await _run_sync(kitchen_tools.remove_basket_items, user, basket_item_ids)


@function_tool(strict_mode=False)
async def log_cooked(
    ctx: RunContextWrapper[AgentContext],
    recipe_id: int | None = None,
    label: str | None = None,
    basket_item_id: int | None = None,
    date: str | None = None,
    meal_time: str | None = None,
    comment: str | None = None,
    publish: bool | None = None,
) -> LogCookedResult:
    """Note « J'ai cuisiné X » (exécution immédiate, annulable) et renvoie l'ardoise et les nouveaux badges.

    Args:
        recipe_id: recette cuisinée si connue (sinon label).
        label: nom du plat libre (« pâtes au pesto »).
        basket_item_id: envie du panier cuisinée, si elle y figure (#id du contexte).
        date: YYYY-MM-DD ou « hier » ; défaut aujourd'hui. Jamais dans le futur.
        meal_time: breakfast, lunch (« ce midi ») ou dinner (« hier soir ») ; défaut selon l'heure.
        comment: commentaire de l'utilisateur, seulement s'il en donne un.
        publish: laisser vide (réglage de l'utilisateur) sauf demande explicite de publier ou de garder privé.
    """
    user = ctx.context.user
    return await _run_sync(
        kitchen_tools.log_cooked_for_user,
        user,
        recipe_id=recipe_id,
        label=label,
        basket_item_id=basket_item_id,
        date=date,
        meal_time=meal_time,
        comment=comment,
        publish=publish,
    )


@function_tool(strict_mode=False)
async def undo_log_cooked(
    ctx: RunContextWrapper[AgentContext],
    post_id: int | None = None,
) -> UndoCookedResult:
    """Annule un « J'ai cuisiné » noté par erreur (post_id de log_cooked ou du profil ; vide = le dernier log)."""
    user = ctx.context.user
    return await _run_sync(kitchen_tools.undo_cooked_for_user, user, post_id)


@function_tool(strict_mode=False)
async def add_to_shopping_list_from_basket(
    ctx: RunContextWrapper[AgentContext],
    basket_item_ids: list[int] | None = None,
    week: str | None = None,
    shopping_list_id: int | None = None,
    name: str | None = None,
) -> BasketShoppingResult:
    """Met les ingrédients des envies du panier (avec recette) dans une liste de courses (exécution immédiate).

    Args:
        basket_item_ids: envies précises ; défaut : toutes les envies ouvertes avec recette de la semaine.
        week: n'importe quel jour (YYYY-MM-DD) de la semaine ; défaut : semaine en cours.
        shopping_list_id: liste existante ; défaut : nouvelle liste « Courses de la semaine ».
        name: nom de la nouvelle liste, si l'utilisateur en donne un.
    """
    user = ctx.context.user
    return await _run_sync(
        kitchen_tools.basket_to_shopping_list,
        user,
        basket_item_ids=basket_item_ids,
        week=week,
        shopping_list_id=shopping_list_id,
        name=name,
    )


@function_tool(strict_mode=False)
async def battle_plan(
    ctx: RunContextWrapper[AgentContext],
    recipe_ids: list[int] | None = None,
    basket_item_ids: list[int] | None = None,
    meal_plan_id: int | None = None,
    target_ready_time: str | None = None,
    start_time: str | None = None,
    servings: int | None = None,
) -> BattlePlanResult:
    """Plan de bataille : dans quel ordre cuisiner plusieurs plats (étapes entremêlées, four partagé, la veille).

    Lecture seule. Donne UNE source : recipe_ids (recettes), basket_item_ids (envies du panier, batch cooking)
    ou meal_plan_id (repas planifié).

    Args:
        recipe_ids: recettes à cuisiner ensemble (ids issus de search_recipes).
        basket_item_ids: envies du panier (#id du contexte) pour une session de batch cooking.
        meal_plan_id: repas planifié à plusieurs recettes.
        target_ready_time: heure du repas « HH:MM » si l'utilisateur la donne (« pour manger à 20 h »).
        start_time: heure de début « HH:MM » si l'utilisateur la donne (exclusif avec target_ready_time).
        servings: portions, si l'utilisateur les précise.
    """
    user = ctx.context.user
    return await _run_sync(
        kitchen_tools.battle_plan_for_chat,
        user,
        recipe_ids=recipe_ids,
        basket_item_ids=basket_item_ids,
        meal_plan_id=meal_plan_id,
        servings=servings,
        start_time=start_time,
        target_ready_time=target_ready_time,
    )


def _chat_payload(conversation_id: int | None, base: dict) -> dict:
    if conversation_id:
        base['chat_context'] = {'conversation_id': conversation_id}
    return base


def _start_import_from_url(
    user,
    url: str,
    conversation_id: int | None = None,
) -> ImportJobStarted:
    from django.db.models import Q

    from recipes.models import Recipe, RecipeImportRequest
    from recipes.tasks import process_recipe_import_from_url
    from recipes.utils import canonicalize_import_url

    url = (url or '').strip()
    if not url:
        raise ValueError('URL requise.')

    canonical_url = canonicalize_import_url(url)
    candidate_urls = {canonical_url, url}
    if canonical_url.endswith('/'):
        candidate_urls.add(canonical_url.rstrip('/'))
    else:
        candidate_urls.add(canonical_url + '/')

    existing = (
        Recipe.objects.filter(Q(is_public=True) | Q(created_by=user))
        .filter(import_source_url__in=list(candidate_urls))
        .order_by('-created_at')
        .first()
    )
    if existing:
        raise ValueError(f'Cette recette est déjà importée (id={existing.id}, titre="{existing.title}").')

    import_request = RecipeImportRequest.objects.create(
        user=user,
        payload=_chat_payload(
            conversation_id,
            {'url': url, 'source_type': 'imported', 'job_type': 'import'},
        ),
        status=RecipeImportRequest.STATUS_PENDING,
    )
    task = process_recipe_import_from_url.delay(str(import_request.id))
    import_request.task_id = task.id
    import_request.save(update_fields=['task_id'])

    return ImportJobStarted(request_id=str(import_request.id), url=url, job_type='import')


def _start_generate_from_idea(
    user,
    idea: str,
    servings: int | None = None,
    conversation_id: int | None = None,
) -> ImportJobStarted:
    from recipes.models import RecipeImportRequest
    from recipes.tasks import process_recipe_generate_from_idea

    idea = (idea or '').strip()
    if len(idea) < 5:
        raise ValueError('Décrivez votre idée en au moins quelques mots.')

    import_request = RecipeImportRequest.objects.create(
        user=user,
        payload=_chat_payload(
            conversation_id,
            {
                'idea_text': idea,
                'servings': servings,
                'job_type': 'generate',
                'source_type': 'generated',
            },
        ),
        status=RecipeImportRequest.STATUS_PENDING,
    )
    task = process_recipe_generate_from_idea.delay(str(import_request.id))
    import_request.task_id = task.id
    import_request.save(update_fields=['task_id'])

    return ImportJobStarted(
        request_id=str(import_request.id),
        idea_text=idea,
        job_type='generate',
    )


# Tools de mutation — identifiés par le consumer pour arrêter le stream
MUTATION_TOOL_NAMES = frozenset(
    {
        'propose_meal_deletion',
        'send_invitation_proposal',
        'propose_recipe_revision',
    }
)
