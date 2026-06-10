"""Tools async exposés à l'agent — wrappers database_sync_to_async."""

from channels.db import database_sync_to_async
from django.db import close_old_connections

from agents import RunContextWrapper, function_tool

from chat.services.agent_context import AgentContext
from chat.services.tool_schemas import (
    AddRecipeResult,
    AddShoppingListItemResult,
    CreateMealPlanSlotResult,
    GetMealPlansResult,
    GetShoppingListItemsResult,
    ImportJobStarted,
    MutationProposal,
    SearchRecipesResult,
)
from recipes.services.invitation_service import (
    _normalize_meal_plan_ids,
    propose_meal_invitation,
    resolve_complices_by_name,
)
from recipes.services.meal_plan_service import (
    add_recipes_to_meal_plan,
    create_meal_plan_slot as create_meal_plan_slot_sync,
    get_meal_plans_for_user,
    propose_meal_deletion_data,
)
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
async def propose_meal_deletion(
    ctx: RunContextWrapper[AgentContext],
    meal_plan_id: int,
    recipe_batch_id: int,
) -> MutationProposal:
    """Propose de retirer une recette d'un créneau (nécessite confirmation utilisateur)."""
    user = ctx.context.user
    return await _run_sync(
        propose_meal_deletion_data, user, meal_plan_id, recipe_batch_id
    )


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

    return await _run_sync(
        propose_meal_invitation, user, normalized_meal_plan_ids, resolved_invitee_ids
    )


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
    return await _run_sync(
        _start_import_from_url, user, url, ctx.context.conversation_id
    )


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
    return await _run_sync(
        _start_generate_from_idea, user, idea, servings, ctx.context.conversation_id
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
        raise ValueError(
            f'Cette recette est déjà importée (id={existing.id}, titre="{existing.title}").'
        )

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
MUTATION_TOOL_NAMES = frozenset({
    'propose_meal_deletion',
    'send_invitation_proposal',
})
