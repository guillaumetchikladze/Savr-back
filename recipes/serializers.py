from rest_framework import serializers
from django.conf import settings
from django.db import models
from .models import (
    Category,
    Recipe,
    Step,
    Ingredient,
    RecipeIngredient,
    StepIngredient,
    MealPlan,
    MealPlanRecipeBatch,
    MealInvitation,
    CookingProgress,
    Timer,
    Post,
    PostPhoto,
    PostComment,
    ShoppingList,
    ShoppingListItem,
    ShoppingListInvitation,
    Collection,
    CollectionRecipe,
    CollectionMember,
    RecipeImportRequest,
    RecipeBatch,
    PostCommentLike,
)
from django.contrib.auth import get_user_model
from django.db.models import Q
from .utils import get_accessible_meal_plan_filter, meal_plan_slot_api_fields
from decimal import Decimal

# Helpers
def compute_meal_plan_servings_with_ratio(meal_plan):
    """
    Retourne (people_count, breakdown, people_count).
    breakdown : liste de { meal_plan_id, recipe_batch_id, base_servings, portions }.
    """
    from .utils import get_meal_plan_people_count, get_batch_portions
    people_count = get_meal_plan_people_count(meal_plan)
    try:
        mprbs = meal_plan.meal_plan_recipe_batches.all()
    except Exception:
        mprbs = []
    breakdown = []
    for mprb in mprbs:
        portions = get_batch_portions(meal_plan, mprb, people_count=people_count)
        breakdown.append({
            'meal_plan_id': meal_plan.id,
            'recipe_batch_id': mprb.recipe_batch_id,
            'base_servings': people_count,
            'portions': portions,
        })
    if not breakdown:
        breakdown.append({
            'meal_plan_id': meal_plan.id,
            'recipe_batch_id': None,
            'base_servings': people_count,
            'portions': people_count,
        })
    return people_count, breakdown, people_count
User = get_user_model()

class UserLightSerializer(serializers.ModelSerializer):
    avatar_url = serializers.SerializerMethodField()
    display_name = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = ['id', 'username', 'avatar_url', 'display_name']

    def get_display_name(self, obj):
        u = (getattr(obj, 'username', None) or '').strip()
        if u:
            return u
        em = (getattr(obj, 'email', None) or '').strip()
        if em and '@' in em:
            return em.split('@', 1)[0]
        return f'#{obj.pk}'
    
    def get_avatar_url(self, obj):
        """Retourner l'URL de l'avatar avec presigned URL si disponible"""
        if not obj.avatar_url:
            return None
        
        # Si l'URL contient un chemin S3 (avatars/...), générer une presigned URL
        # Sinon, retourner l'URL telle quelle (peut être une URL externe)
        try:
            from savr_back.settings import build_presigned_get_url
            import re
            
            # Extraire le chemin depuis l'URL S3
            # Formats possibles:
            # - http://host/bucket/avatars/2/file.jpg
            # - https://bucket.s3.region.amazonaws.com/avatars/2/file.jpg
            # - http://192.168.1.51:9000/savr/avatars/2/file.jpg
            
            if 'avatars/' in obj.avatar_url:
                # Chercher le pattern /bucket/avatars/... ou /avatars/...
                # On cherche après le dernier / qui précède "avatars"
                match = re.search(r'/(?:[^/]+/)?(avatars/.+)$', obj.avatar_url)
                if match:
                    image_path = match.group(1)
                    presigned_url = build_presigned_get_url(image_path)
                    if presigned_url:
                        return presigned_url
                
                # Si la regex ne fonctionne pas, essayer de trouver directement "avatars/"
                idx = obj.avatar_url.find('avatars/')
                if idx != -1:
                    image_path = obj.avatar_url[idx:]
                    presigned_url = build_presigned_get_url(image_path)
                    if presigned_url:
                        return presigned_url
            
            # Si c'est une URL externe (pas S3, pas d'avatars), retourner telle quelle
            if obj.avatar_url.startswith('http') and 'avatars/' not in obj.avatar_url:
                return obj.avatar_url
            
            # Par défaut, essayer de générer une presigned URL avec l'URL complète
            # (peut fonctionner si c'est déjà un chemin relatif)
            return build_presigned_get_url(obj.avatar_url) if obj.avatar_url else None
        except Exception as e:
            # En cas d'erreur, retourner l'URL originale
            import traceback
            print(f"Error generating presigned URL for avatar in UserLightSerializer: {e}")
            print(traceback.format_exc())
            return obj.avatar_url


class CategoryParentSerializer(serializers.ModelSerializer):
    class Meta:
        model = Category
        fields = ['id', 'name', 'slug', 'display_order']


class CategorySerializer(serializers.ModelSerializer):
    parent = CategoryParentSerializer(read_only=True)
    parent_id = serializers.IntegerField(read_only=True)

    class Meta:
        model = Category
        fields = ['id', 'name', 'slug', 'display_order', 'parent', 'parent_id']


class IngredientSerializer(serializers.ModelSerializer):
    category = CategorySerializer(read_only=True)
    category_id = serializers.PrimaryKeyRelatedField(
        queryset=Category.objects.all(),
        source='category',
        write_only=True,
        required=False,
        allow_null=True
    )
    
    class Meta:
        model = Ingredient
        fields = ['id', 'name', 'category', 'category_id']


class RecipeIngredientSerializer(serializers.ModelSerializer):
    ingredient = IngredientSerializer(read_only=True)
    ingredient_id = serializers.PrimaryKeyRelatedField(
        queryset=Ingredient.objects.all(),
        source='ingredient',
        write_only=True
    )
    unit_display = serializers.CharField(source='get_unit_display', read_only=True)
    
    class Meta:
        model = RecipeIngredient
        fields = ['id', 'ingredient', 'ingredient_id', 'quantity', 'unit', 'unit_display']


class StepIngredientSerializer(serializers.ModelSerializer):
    ingredient = IngredientSerializer(read_only=True)
    unit_display = serializers.CharField(source='get_unit_display', read_only=True)
    
    class Meta:
        model = StepIngredient
        fields = ['id', 'ingredient', 'quantity', 'unit', 'unit_display']


class StepSerializer(serializers.ModelSerializer):
    step_ingredients = StepIngredientSerializer(many=True, read_only=True)
    
    class Meta:
        model = Step
        fields = ['id', 'order', 'title', 'instruction', 'tip', 'has_timer', 'timer_duration', 'step_ingredients']


class RecipeDetailSerializer(serializers.ModelSerializer):
    """
    Serializer léger pour retrieve - charge seulement les données essentielles
    Les steps et ingrédients détaillés sont chargés via des endpoints séparés
    """
    created_by_username = serializers.CharField(source='created_by.username', read_only=True)
    meal_type_display = serializers.CharField(source='get_meal_type_display', read_only=True)
    difficulty_display = serializers.CharField(source='get_difficulty_display', read_only=True)
    source_type_display = serializers.CharField(source='get_source_type_display', read_only=True)
    is_favorited = serializers.SerializerMethodField()
    is_author = serializers.SerializerMethodField()
    image_url = serializers.SerializerMethodField()
    # Ne pas inclure steps et recipe_ingredients ici - chargés via endpoints séparés
    
    class Meta:
        model = Recipe
        fields = [
            'id', 'title', 'description', 'steps_summary', 'meal_type', 'meal_type_display',
            'difficulty', 'difficulty_display', 'prep_time', 'cook_time',
            'servings', 'image_path', 'image_url', 'created_by', 'created_by_username',
            'is_public', 'source_type', 'source_type_display', 'import_source_url',
            'created_at', 'updated_at', 'is_favorited', 'is_author'
        ]
        read_only_fields = ['created_by', 'created_at', 'updated_at', 'image_url']
    
    def get_image_url(self, obj):
        return obj.image_url
    
    def get_is_favorited(self, obj):
        """Vérifier si l'utilisateur connecté a favorisé cette recette"""
        request = self.context.get('request')
        if request and request.user.is_authenticated:
            return obj.favorited_by.filter(id=request.user.id).exists()
        return False
    
    def get_is_author(self, obj):
        """Vérifier si l'utilisateur connecté est l'auteur de la recette"""
        request = self.context.get('request')
        if request and request.user.is_authenticated:
            return obj.created_by_id == request.user.id
        return False


class RecipeSerializer(serializers.ModelSerializer):
    steps = StepSerializer(many=True, read_only=True)
    recipe_ingredients = RecipeIngredientSerializer(many=True, read_only=True)
    created_by_username = serializers.CharField(source='created_by.username', read_only=True)
    meal_type_display = serializers.CharField(source='get_meal_type_display', read_only=True)
    difficulty_display = serializers.CharField(source='get_difficulty_display', read_only=True)
    source_type_display = serializers.CharField(source='get_source_type_display', read_only=True)
    is_favorited = serializers.SerializerMethodField()
    is_author = serializers.SerializerMethodField()
    image_url = serializers.SerializerMethodField()
    
    class Meta:
        model = Recipe
        fields = [
            'id', 'title', 'description', 'steps_summary', 'meal_type', 'meal_type_display',
            'difficulty', 'difficulty_display', 'prep_time', 'cook_time',
            'servings', 'image_path', 'image_url', 'created_by', 'created_by_username',
            'is_public', 'source_type', 'source_type_display', 'import_source_url',
            'created_at', 'updated_at', 'steps', 'recipe_ingredients', 'is_favorited', 'is_author'
        ]
        read_only_fields = ['created_by', 'created_at', 'updated_at', 'image_url']
    
    def get_image_url(self, obj):
        return obj.image_url
    
    def get_is_favorited(self, obj):
        """Vérifier si l'utilisateur connecté a favorisé cette recette"""
        request = self.context.get('request')
        if request and request.user.is_authenticated:
            return obj.favorited_by.filter(id=request.user.id).exists()
        return False
    
    def get_is_author(self, obj):
        """Vérifier si l'utilisateur connecté est l'auteur de la recette"""
        request = self.context.get('request')
        if request and request.user.is_authenticated:
            return obj.created_by_id == request.user.id
        return False


class RecipeCreateSerializer(serializers.ModelSerializer):
    steps = StepSerializer(many=True)
    ingredients = serializers.ListField(
        child=serializers.DictField(),
        write_only=True,
        required=False
    )
    
    class Meta:
        model = Recipe
        fields = [
            'title', 'description', 'steps_summary', 'meal_type', 'difficulty',
            'prep_time', 'cook_time', 'servings', 'image_path',
            'is_public', 'source_type', 'import_source_url',
            'steps', 'ingredients'
        ]
    
    def create(self, validated_data):
        steps_data = validated_data.pop('steps')
        ingredients_data = validated_data.pop('ingredients', [])
        user = self.context['request'].user
        
        recipe = Recipe.objects.create(created_by=user, **validated_data)
        
        # Créer les étapes directement liées à la recette
        for step_data in steps_data:
            Step.objects.create(recipe=recipe, **step_data)
        
        # Créer un batch initial pour la recette
        RecipeBatch.objects.create(recipe=recipe, created_by=user)
        
        # Créer les ingrédients
        for ingredient_data in ingredients_data:
            ingredient_id = ingredient_data.get('ingredient_id')
            quantity = ingredient_data.get('quantity')
            unit = ingredient_data.get('unit', 'g')
            
            if ingredient_id:
                RecipeIngredient.objects.create(
                    recipe=recipe,
                    ingredient_id=ingredient_id,
                    quantity=quantity,
                    unit=unit
                )
        
        return recipe


class RecipeLightSerializer(serializers.ModelSerializer):
    meal_type_display = serializers.CharField(source='get_meal_type_display', read_only=True)
    difficulty_display = serializers.CharField(source='get_difficulty_display', read_only=True)
    source_type_display = serializers.CharField(source='get_source_type_display', read_only=True)
    created_by_username = serializers.CharField(source='created_by.username', read_only=True)
    is_author = serializers.SerializerMethodField()
    image_url = serializers.SerializerMethodField()
    total_time = serializers.SerializerMethodField()
    
    class Meta:
        model = Recipe
        fields = [
            'id', 'title', 'image_path', 'image_url', 'meal_type', 'meal_type_display',
            'difficulty', 'difficulty_display', 'prep_time', 'cook_time', 'servings',
            'total_time',
            'source_type', 'source_type_display', 'import_source_url',
            'created_by', 'created_by_username', 'created_at',
            'is_author'
        ]
    
    def get_image_url(self, obj):
        return obj.image_url

    def get_total_time(self, obj):
        """Temps total (prep + cuisson) en minutes, ou None si inconnu."""
        try:
            prep = obj.prep_time or 0
            cook = obj.cook_time or 0
            total = prep + cook
            return total if total > 0 else None
        except Exception:
            return None
    
    def get_is_author(self, obj):
        """Vérifier si l'utilisateur connecté est l'auteur de la recette"""
        request = self.context.get('request')
        if request and request.user.is_authenticated:
            return obj.created_by_id == request.user.id
        return False


class RecipeBatchLightSerializer(serializers.ModelSerializer):
    recipe = RecipeLightSerializer(read_only=True)
    created_by = UserLightSerializer(read_only=True)
    total_servings_batch = serializers.FloatField(read_only=True)
    servings_breakdown = serializers.ListField(child=serializers.DictField(), read_only=True)
    total_servings_batch_accessible = serializers.FloatField(read_only=True)
    servings_breakdown_accessible = serializers.ListField(child=serializers.DictField(), read_only=True)
    groupedDates = serializers.ListField(child=serializers.CharField(), read_only=True)
    meal_plan_ids = serializers.ListField(child=serializers.IntegerField(), read_only=True)
    meals = serializers.ListField(child=serializers.DictField(), read_only=True)
    steps = StepSerializer(many=True, read_only=True)
    is_cooked = serializers.BooleanField(read_only=True)
    
    class Meta:
        model = RecipeBatch
        fields = [
            'id', 'name', 'recipe', 'created_by',
            'total_servings_batch', 'servings_breakdown',
            'total_servings_batch_accessible', 'servings_breakdown_accessible',
            'groupedDates',
            'meal_plan_ids', 'meals', 'is_cooked',
            'shopping_done',
            'steps', 'photo_step_orders',
            'created_at', 'updated_at'
        ]


class RecipeMinimalSerializer(serializers.ModelSerializer):
    """Serializer ultra-léger pour les recettes en mode minimal (seulement id, title, image_url)"""
    image_url = serializers.SerializerMethodField()
    
    class Meta:
        model = Recipe
        fields = ['id', 'title', 'image_url']
    
    def get_image_url(self, obj):
        return obj.image_url


class RecipeBatchSerializer(serializers.ModelSerializer):
    recipe = RecipeLightSerializer(read_only=True)
    recipe_id = serializers.PrimaryKeyRelatedField(
        queryset=Recipe.objects.all(),
        source='recipe',
        write_only=True
    )
    
    class Meta:
        model = RecipeBatch
        fields = ['id', 'name', 'notes', 'recipe', 'recipe_id', 'photo_step_orders', 'created_at', 'updated_at']
        read_only_fields = ['id', 'created_at', 'updated_at', 'photo_step_orders']


class RecipeBatchMealPlanStatusSerializer(serializers.ModelSerializer):
    """
    Serializer très léger pour les écrans MealPlan (statuts + association liste).
    Objectif: éviter N calls /shopping-lists/ côté front.
    """
    shopping_list = serializers.SerializerMethodField()

    class Meta:
        model = RecipeBatch
        fields = [
            'id',
            'is_cooked',
            'shopping_done',
            'shopping_list',
            'updated_at',
        ]
        read_only_fields = fields

    def get_fields(self):
        """
        Par défaut, on retire `shopping_list` du payload MealPlan pour réduire le JSON
        et éviter du travail inutile. Ré-activable via `?include_shopping_list=true`.
        """
        fields = super().get_fields()
        ctx = getattr(self, 'context', {}) or {}
        include_shopping_list = bool(ctx.get('include_shopping_list'))
        if not include_shopping_list:
            fields.pop('shopping_list', None)
        return fields

    def get_shopping_list(self, obj):
        """
        ShoppingListBatch est un OneToOne sur RecipeBatch (`recipe_batch.shopping_list_batch`).
        Quand `recipe_batch__shopping_list_batch__shopping_list` est select_related côté queryset,
        on évite une requête DB par batch ici.
        """
        try:
            link = getattr(obj, 'shopping_list_batch', None)
            sl = getattr(link, 'shopping_list', None) if link else None
            if not sl:
                return None
            return {
                'id': sl.id,
                'name': sl.name,
                'is_complete': bool(getattr(sl, 'is_complete', False)),
                'is_archived': bool(getattr(sl, 'is_archived', False)),
            }
        except Exception:
            # Fallback safe (on préfère "pas de shopping_list" plutôt que 500)
            return None


class MealPlanRecipeSerializer(serializers.ModelSerializer):
    """
    Serializer pour la relation MealPlan-RecipeBatch avec portions.
    """
    recipe = RecipeLightSerializer(source='recipe_batch.recipe', read_only=True)
    recipe_batch = RecipeBatchMealPlanStatusSerializer(read_only=True)
    recipe_batch_id = serializers.PrimaryKeyRelatedField(
        queryset=RecipeBatch.objects.all(),
        source='recipe_batch',
        write_only=True,
        required=False,
        allow_null=True
    )
    portions = serializers.SerializerMethodField()
    is_portions_overridden = serializers.BooleanField(read_only=True)
    groupedDates = serializers.SerializerMethodField()
    group_id = serializers.SerializerMethodField()
    
    class Meta:
        model = MealPlanRecipeBatch
        fields = [
            'id',
            'recipe',
            'recipe_batch',
            'recipe_batch_id',
            'portions',
            'is_portions_overridden',
            'order',
            'group_id',
            'groupedDates',
            'created_at',
            'updated_at'
        ]
        read_only_fields = ['id', 'created_at', 'updated_at', 'recipe', 'recipe_batch']
    
    def get_portions(self, obj):
        from .utils import get_batch_portions, get_meal_plan_people_count
        return get_batch_portions(obj.meal_plan, obj, people_count=get_meal_plan_people_count(obj.meal_plan))
    
    def get_group_id(self, obj):
        # Utiliser l'id du batch comme identifiant de groupe
        return obj.recipe_batch_id if obj.recipe_batch_id else None
    
    def get_groupedDates(self, obj):
        """Dates de tous les meal plans liés au même batch."""
        if not obj.recipe_batch_id:
            return [obj.meal_plan.date.isoformat()]
        ctx = getattr(self, 'context', {}) or {}
        mapping = ctx.get('grouped_dates_by_batch_id') or {}
        if obj.recipe_batch_id in mapping:
            return mapping.get(obj.recipe_batch_id) or [obj.meal_plan.date.isoformat()]
        meal_plans = MealPlan.objects.filter(
            meal_plan_recipe_batches__recipe_batch_id=obj.recipe_batch_id
        ).distinct().order_by('date', 'meal_time')
        dates = [mp.date.isoformat() for mp in meal_plans]
        return dates or [obj.meal_plan.date.isoformat()]


class MealPlanDetailSerializer(serializers.ModelSerializer):
    """
    Serializer léger pour retrieve - charge seulement les données essentielles
    Les steps et ingrédients détaillés sont chargés via des endpoints séparés
    """
    recipe = RecipeLightSerializer(read_only=True)  # Utiliser RecipeLightSerializer au lieu de RecipeSerializer
    recipes = MealPlanRecipeSerializer(source='meal_plan_recipe_batches', many=True, read_only=True)
    meal_time_display = serializers.CharField(source='get_meal_time_display', read_only=True)
    meal_type_display = serializers.CharField(source='get_meal_type_display', read_only=True)
    user = UserLightSerializer(read_only=True)
    participants = serializers.SerializerMethodField()
    total_guest_count = serializers.SerializerMethodField()
    total_participants = serializers.SerializerMethodField()
    total_servings = serializers.SerializerMethodField()
    servings_breakdown = serializers.SerializerMethodField()
    
    class Meta:
        model = MealPlan
        fields = [
            'id', 'date', 'meal_time', 'meal_time_display',
            'meal_type', 'meal_type_display', 'recipe', 'recipes',
            'user', 'participants', 'confirmed', 'guest_count', 
            'total_guest_count', 'total_participants', 'total_servings', 'servings_breakdown',
            'created_at', 'updated_at'
        ]
        read_only_fields = ['user', 'participants', 'created_at', 'updated_at']
    
    def get_participants(self, obj):
        from .models import MealInvitation
        invitations = obj.invitations.all() if hasattr(obj, 'invitations') else MealInvitation.objects.filter(meal_plan=obj).select_related('invitee')
        # Log pour debug (uniquement en mode DEBUG)
        if settings.DEBUG:
            import logging
            logger = logging.getLogger(__name__)
            logger.debug(f"[MealPlanDetailSerializer] get_participants for meal plan {obj.id}: {len(invitations)} invitations")
            for inv in invitations:
                logger.debug(f"  - Invitation {inv.id}: user_id={inv.invitee_id}, status={inv.status}")
        return [
            {
                'user': UserLightSerializer(inv.invitee, context=self.context).data,
                'status': inv.status
            }
            for inv in invitations
        ]
    
    def get_total_guest_count(self, obj: MealPlan):
        if hasattr(obj, '_total_guest_count'):
            return obj._total_guest_count
        return obj.guest_count or 0
    
    def get_total_participants(self, obj: MealPlan):
        if hasattr(obj, '_total_participants'):
            precedence = {'accepted': 3, 'pending': 2, 'declined': 1}
            by_user_id = {}
            for p in obj._total_participants:
                uid = p['user'].id
                existing = by_user_id.get(uid)
                if not existing or precedence.get(p['status'], 0) > precedence.get(existing['status'], 0):
                    by_user_id[uid] = {
                        'user': UserLightSerializer(p['user'], context=self.context).data,
                        'status': p['status'],
                    }
            return list(by_user_id.values())
        return self.get_participants(obj)
    
    def get_total_servings(self, obj: MealPlan):
        if hasattr(obj, '_total_servings'):
            return obj._total_servings
        participants = obj.invitations.filter(status__in=['accepted', 'pending']).count() if hasattr(obj, 'invitations') else 0
        guest_count = obj.guest_count or 0
        return 1 + participants + guest_count

    def get_servings_breakdown(self, obj: MealPlan):
        return []
    
    def get_groupedDates(self, obj: MealPlan):
        """Calculer groupedDates en agrégeant les dates de toutes les recettes groupées."""
        dates = set()
        for mprb in obj.meal_plan_recipe_batches.all():
            if mprb.recipe_batch_id:
                for mp in MealPlan.objects.filter(meal_plan_recipe_batches__recipe_batch_id=mprb.recipe_batch_id):
                    dates.add(mp.date.isoformat())
            else:
                dates.add(obj.date.isoformat())
        return sorted(list(dates)) if dates else [obj.date.isoformat()]
    
class MealPlanSerializer(serializers.ModelSerializer):
    recipe = RecipeSerializer(read_only=True)  # Garder pour compatibilité (utilisé pour create/update)
    recipe_id = serializers.PrimaryKeyRelatedField(
        queryset=Recipe.objects.all(),
        source='recipe',
        write_only=True,
        required=False,
        allow_null=True
    )
    # Rendre ces champs optionnels pour permettre les updates partiels
    date = serializers.DateField(required=False)
    meal_time = serializers.ChoiceField(choices=MealPlan.MEAL_TIME_CHOICES, required=False)
    meal_type = serializers.ChoiceField(choices=MealPlan.MEAL_TYPE_CHOICES, required=False)
    # Nouvelles propriétés pour plusieurs recettes
    recipes = MealPlanRecipeSerializer(source='meal_plan_recipe_batches', many=True, read_only=True)
    batch_ids = serializers.ListField(
        child=serializers.IntegerField(),
        write_only=True,
        required=False,
        help_text="Liste des IDs de recipe_batch à associer au meal plan (append)"
    )
    recipe_ids = serializers.ListField(
        child=serializers.IntegerField(),
        write_only=True,
        required=False,
        help_text="(Compat) Liste d'IDs de recettes pour créer des batches à la volée"
    )
    meal_time_display = serializers.SerializerMethodField()
    meal_type_display = serializers.CharField(source='get_meal_type_display', read_only=True)
    user = UserLightSerializer(read_only=True)
    participants = serializers.SerializerMethodField()
    total_guest_count = serializers.SerializerMethodField()
    total_participants = serializers.SerializerMethodField()
    total_servings = serializers.SerializerMethodField()
    # Payload unifié lecture
    recipes_entries = serializers.SerializerMethodField()
    # Payload unifié écriture
    entries = serializers.ListField(
        child=serializers.DictField(),
        write_only=True,
        required=False,
        help_text="Liste unifiée {recipe_id, batch_id, portions, order}"
    )
    entry_portions = serializers.DictField(
        child=serializers.IntegerField(),
        write_only=True,
        required=False,
        help_text="Dictionnaire {recipe_batch_id: portions} pour mettre à jour les portions sans recréer de batches"
    )
    servings_breakdown = serializers.SerializerMethodField()
    is_guest = serializers.SerializerMethodField()
    
    class Meta:
        model = MealPlan
        fields = [
            'id', 'date', 'meal_time', 'meal_time_display', 'slot_key', 'custom_label', 'scheduled_time',
            'meal_type', 'meal_type_display', 'recipe', 'recipe_id',
            'recipes', 'batch_ids', 'recipe_ids',
            'entries', 'entry_portions', 'recipes_entries',
            'user', 'participants', 'confirmed', 'guest_count', 
            'total_guest_count', 'total_participants', 'total_servings', 'servings_breakdown',
            'is_guest', 'created_at', 'updated_at'
        ]
        read_only_fields = ['user', 'participants', 'created_at', 'updated_at', 'recipes', 'recipe']

    def get_meal_time_display(self, obj: MealPlan):
        if obj.meal_time == 'other' and getattr(obj, 'custom_label', None):
            return obj.custom_label
        return obj.get_meal_time_display()
    
    def validate(self, attrs):
        # Si update partiel avec entries/recipe_ids/batch_ids, ne pas exiger date/meal_time/meal_type
        if self.instance and ('recipe_ids' in attrs or 'batch_ids' in attrs or 'entries' in attrs):
            return attrs
        return attrs
    
    def create(self, validated_data):
        from decimal import Decimal, ROUND_HALF_UP
        
        validated_data['user'] = self.context['request'].user
        
        entries = validated_data.pop('entries', None)
        batch_ids = validated_data.pop('batch_ids', None)
        recipe_ids = validated_data.pop('recipe_ids', None)
        validated_data.pop('recipe_ratios', None)
        
        meal_plan = super().create(validated_data)
        
        if entries:
            for order, item in enumerate(entries):
                recipe_id = item.get('recipe_id')
                batch_id = item.get('batch_id')
                portions = item.get('portions')
                if portions is not None:
                    try:
                        portions = int(portions)
                        portions = max(0, portions)
                    except (TypeError, ValueError):
                        portions = None
                order_value = item.get('order', order)
                if batch_id:
                    MealPlanRecipeBatch.objects.create(
                        meal_plan=meal_plan,
                        recipe_batch_id=batch_id,
                        portions=portions,
                        is_portions_overridden=portions is not None,
                        order=order_value
                    )
                elif recipe_id:
                    batch = RecipeBatch.objects.create(recipe_id=recipe_id, created_by=meal_plan.user)
                    MealPlanRecipeBatch.objects.create(
                        meal_plan=meal_plan,
                        recipe_batch=batch,
                        portions=portions,
                        is_portions_overridden=portions is not None,
                        order=order_value
                    )
            return meal_plan
        
        if batch_ids:
            for order, batch_id in enumerate(batch_ids):
                MealPlanRecipeBatch.objects.create(
                    meal_plan=meal_plan,
                    recipe_batch_id=batch_id,
                    portions=None,
                    is_portions_overridden=False,
                    order=order
                )
            return meal_plan
        
        if recipe_ids:
            for order, recipe_id in enumerate(recipe_ids):
                batch = RecipeBatch.objects.create(recipe_id=recipe_id, created_by=meal_plan.user)
                MealPlanRecipeBatch.objects.create(
                    meal_plan=meal_plan,
                    recipe_batch=batch,
                    portions=None,
                    is_portions_overridden=False,
                    order=order
                )
        
        return meal_plan
    
    def update(self, instance, validated_data):
        from decimal import Decimal, ROUND_HALF_UP
        
        entries = validated_data.pop('entries', None)
        batch_ids = validated_data.pop('batch_ids', None)
        recipe_ids = validated_data.pop('recipe_ids', None)
        entry_portions = validated_data.pop('entry_portions', None)
        validated_data.pop('recipe_ratios', None)
        validated_data.pop('entry_ratios', None)
        
        meal_plan = super().update(instance, validated_data)
        
        if entries is not None:
            existing_mprbs = list(
                meal_plan.meal_plan_recipe_batches.select_related('recipe_batch__recipe')
            )
            existing_batch_by_recipe_id = {}
            for mprb in existing_mprbs:
                if mprb.recipe_batch and mprb.recipe_batch.recipe_id:
                    existing_batch_by_recipe_id[mprb.recipe_batch.recipe_id] = mprb.recipe_batch_id

            meal_plan.meal_plan_recipe_batches.all().delete()
            for order, item in enumerate(entries):
                recipe_id = item.get('recipe_id')
                batch_id = item.get('batch_id')
                portions = item.get('portions')
                if portions is not None:
                    try:
                        portions = int(portions)
                        portions = max(0, portions)
                    except (TypeError, ValueError):
                        portions = None
                order_value = item.get('order', order)
                if batch_id:
                    MealPlanRecipeBatch.objects.create(
                        meal_plan=meal_plan,
                        recipe_batch_id=batch_id,
                        portions=portions,
                        is_portions_overridden=portions is not None,
                        order=order_value
                    )
                elif recipe_id:
                    reused_batch_id = existing_batch_by_recipe_id.get(recipe_id)
                    if reused_batch_id:
                        MealPlanRecipeBatch.objects.create(
                            meal_plan=meal_plan,
                            recipe_batch_id=reused_batch_id,
                            portions=portions,
                            is_portions_overridden=portions is not None,
                            order=order_value
                        )
                    else:
                        batch = RecipeBatch.objects.create(recipe_id=recipe_id, created_by=meal_plan.user)
                        MealPlanRecipeBatch.objects.create(
                            meal_plan=meal_plan,
                            recipe_batch=batch,
                            portions=portions,
                            is_portions_overridden=portions is not None,
                            order=order_value
                        )
            return meal_plan
        
        if batch_ids is not None:
            meal_plan.meal_plan_recipe_batches.all().delete()
            for order, batch_id in enumerate(batch_ids or []):
                MealPlanRecipeBatch.objects.create(
                    meal_plan=meal_plan,
                    recipe_batch_id=batch_id,
                    portions=None,
                    is_portions_overridden=False,
                    order=order
                )
            return meal_plan
        
        if recipe_ids is not None:
            existing_mprbs = list(
                meal_plan.meal_plan_recipe_batches.select_related('recipe_batch__recipe')
            )
            existing_batch_by_recipe_id = {mprb.recipe_batch.recipe_id: mprb.recipe_batch_id for mprb in existing_mprbs if mprb.recipe_batch and mprb.recipe_batch.recipe_id}

            meal_plan.meal_plan_recipe_batches.all().delete()
            for order, recipe_id in enumerate(recipe_ids):
                reused_batch_id = existing_batch_by_recipe_id.get(recipe_id)
                if reused_batch_id:
                    MealPlanRecipeBatch.objects.create(
                        meal_plan=meal_plan,
                        recipe_batch_id=reused_batch_id,
                        portions=None,
                        is_portions_overridden=False,
                        order=order
                    )
                else:
                    batch = RecipeBatch.objects.create(recipe_id=recipe_id, created_by=meal_plan.user)
                    MealPlanRecipeBatch.objects.create(
                        meal_plan=meal_plan,
                        recipe_batch=batch,
                        portions=None,
                        is_portions_overridden=False,
                        order=order
                    )
            return meal_plan
        
        if entry_portions is not None:
            for key, portions_value in entry_portions.items():
                try:
                    recipe_batch_id = int(key)
                except (TypeError, ValueError):
                    continue
                try:
                    mprb = meal_plan.meal_plan_recipe_batches.get(recipe_batch_id=recipe_batch_id)
                except MealPlanRecipeBatch.DoesNotExist:
                    continue
                try:
                    p = int(portions_value)
                    p = max(0, p)
                except (TypeError, ValueError):
                    continue
                mprb.portions = p
                mprb.is_portions_overridden = True
                mprb.save(update_fields=['portions', 'is_portions_overridden', 'updated_at'])
        
        return meal_plan

    def get_recipes_entries(self, obj: MealPlan):
        """Retourne une liste unifiée {recipe_id, batch_id, portions, order} pour sérialisation."""
        from .utils import get_batch_portions, get_meal_plan_people_count
        people = get_meal_plan_people_count(obj)
        entries = []
        for mprb in obj.meal_plan_recipe_batches.all().order_by('order', 'id'):
            entries.append({
                'recipe_id': mprb.recipe_batch.recipe_id if mprb.recipe_batch else None,
                'batch_id': mprb.recipe_batch_id,
                'portions': get_batch_portions(obj, mprb, people_count=people),
                'order': mprb.order,
            })
        return entries
    
    def get_participants(self, obj):
        from .models import MealInvitation
        # Utiliser le prefetch si disponible (via Prefetch), sinon faire une requête
        # obj.invitations.all() utilisera automatiquement le cache si prefetch est fait
        invitations = obj.invitations.all() if hasattr(obj, 'invitations') else MealInvitation.objects.filter(meal_plan=obj).select_related('invitee')
        return [
            {
                'user': UserLightSerializer(inv.invitee, context=self.context).data,
                'status': inv.status
            }
            for inv in invitations
        ]
    
    def get_total_guest_count(self, obj: MealPlan):
        """
        Retourne le total_guest_count pré-calculé si disponible.
        Sinon retourne simplement guest_count (meal plan non groupé).
        """
        if hasattr(obj, '_total_guest_count'):
            return obj._total_guest_count
        return obj.guest_count or 0
    
    def get_total_participants(self, obj: MealPlan):
        """
        Retourne les participants groupés pré-calculés si disponibles.
        Sinon retourne les participants du meal plan individuel.
        """
        if hasattr(obj, '_total_participants'):
            precedence = {'accepted': 3, 'pending': 2, 'declined': 1}
            by_user_id = {}
            for p in obj._total_participants:
                uid = p['user'].id
                existing = by_user_id.get(uid)
                if not existing or precedence.get(p['status'], 0) > precedence.get(existing['status'], 0):
                    by_user_id[uid] = {
                        'user': UserLightSerializer(p['user'], context=self.context).data,
                        'status': p['status'],
                    }
            return list(by_user_id.values())
        
        # Fallback : utiliser get_participants normal
        return self.get_participants(obj)
    
    def _calculate_recipe_group_servings(self, meal_plan_recipe_batch):
        """
        Calcule le total de portions pour ce batch (tous meal plans confondus).
        """
        from .utils import get_batch_portions
        batch_id = meal_plan_recipe_batch.recipe_batch_id
        if not batch_id:
            return get_batch_portions(meal_plan_recipe_batch.meal_plan, meal_plan_recipe_batch)
        total = 0
        for mprb in MealPlanRecipeBatch.objects.filter(recipe_batch_id=batch_id).select_related('meal_plan'):
            total += get_batch_portions(mprb.meal_plan, mprb)
        return total
    
    def get_total_servings(self, obj: MealPlan):
        # Les totals de meal plan ne doivent pas être affectés par les ratios (vue meal plan)
        if hasattr(obj, '_total_servings'):
            return obj._total_servings
        # Base = 1 + participants actifs + guests
        participants = obj.invitations.filter(status__in=['accepted', 'pending']).count() if hasattr(obj, 'invitations') else 0
        return 1 + participants + (obj.guest_count or 0)

    def get_servings_breakdown(self, obj: MealPlan):
        """
        Pour les vues meal plan (calendrier, by_date, détail simple), on conserve
        un total_servings basé uniquement sur 1 + participants actifs + guests,
        sans appliquer les ratios. Le breakdown par ratio est réservé aux vues
        de batch (RecipeBatchViewSet, RecipeSummaryModal, etc.).
        """
        return []
    
    def get_is_guest(self, obj: MealPlan):
        """
        Vérifie si l'utilisateur actuel a une invitation acceptée pour ce meal plan.
        """
        request = self.context.get('request')
        if not request or not request.user or not request.user.is_authenticated:
            return False
        
        from .models import MealInvitation
        return MealInvitation.objects.filter(
            meal_plan=obj,
            invitee=request.user,
            status='accepted'
        ).exists()
    
class MealPlanListSerializer(serializers.ModelSerializer):
    user = UserLightSerializer(read_only=True)
    recipe = RecipeLightSerializer(read_only=True)  # Garder pour compatibilité
    recipes = MealPlanRecipeSerializer(source='meal_plan_recipe_batches', many=True, read_only=True)
    meal_time_display = serializers.SerializerMethodField()
    meal_type_display = serializers.CharField(source='get_meal_type_display', read_only=True)
    groupedDates = serializers.SerializerMethodField()
    is_guest = serializers.SerializerMethodField()

    def get_meal_time_display(self, obj: MealPlan):
        if obj.meal_time == 'other' and getattr(obj, 'custom_label', None):
            return obj.custom_label
        return obj.get_meal_time_display()
    
    class Meta:
        model = MealPlan
        fields = [
            'id', 'date', 'meal_time', 'meal_time_display', 'slot_key', 'custom_label', 'scheduled_time',
            'meal_type', 'meal_type_display', 'confirmed',
            'recipe', 'user', 'recipes', 'groupedDates',
            'is_guest',
        ]
    
    def get_groupedDates(self, obj: MealPlan):
        """Calculer groupedDates en agrégeant les dates de toutes les recettes groupées."""
        dates = set()
        for mprb in obj.meal_plan_recipe_batches.all():
            if mprb.recipe_batch_id:
                for mp in MealPlan.objects.filter(meal_plan_recipe_batches__recipe_batch_id=mprb.recipe_batch_id):
                    dates.add(mp.date.isoformat())
            else:
                dates.add(obj.date.isoformat())
        return sorted(list(dates)) if dates else [obj.date.isoformat()]

    def get_is_guest(self, obj: MealPlan):
        """
        True si l'utilisateur courant a une invitation acceptée pour ce repas.
        """
        request = self.context.get('request')
        if not request or not request.user or not request.user.is_authenticated:
            return False
        from .models import MealInvitation
        return MealInvitation.objects.filter(meal_plan=obj, invitee=request.user, status='accepted').exists()


class MealPlanLightForInvitationSerializer(serializers.ModelSerializer):
    """
    Serializer ultra-léger pour meal_plan dans la liste d'invitations.
    Pas de recipes, participants, ni SerializerMethodField coûteux.
    """
    meal_time_display = serializers.SerializerMethodField()
    meal_type_display = serializers.CharField(source='get_meal_type_display', read_only=True)
    user = UserLightSerializer(read_only=True)

    def get_meal_time_display(self, obj: MealPlan):
        if obj.meal_time == 'other' and getattr(obj, 'custom_label', None):
            return obj.custom_label
        return obj.get_meal_time_display()

    class Meta:
        model = MealPlan
        fields = [
            'id', 'date', 'meal_time', 'meal_time_display', 'slot_key', 'custom_label', 'scheduled_time',
            'meal_type', 'meal_type_display', 'user',
        ]


def _meal_plan_recipe_previews(meal_plan, limit=3):
    """
    Retourne max `limit` previews {image_url, title} pour la timeline.
    Inclut les recettes sans photo (title seulement).
    """
    previews = []
    try:
        mprbs = meal_plan.meal_plan_recipe_batches.all()
    except Exception:
        mprbs = []
    for mprb in mprbs:
        recipe = getattr(getattr(mprb, 'recipe_batch', None), 'recipe', None)
        if not recipe:
            continue
        title = (getattr(recipe, 'title', None) or '').strip() or 'Recette'
        previews.append({
            'image_url': getattr(recipe, 'image_url', None) or None,
            'title': title,
        })
        if len(previews) >= limit:
            break
    return previews


class MealPlanTimelineSerializer(serializers.ModelSerializer):
    """
    Serializer ultra-léger pour MealPlanTimelineScreen:
    - pas de `recipes` détaillées
    - pas de `participants` détaillés
    - uniquement thumbs + preview avatars + compteurs
    """
    meal_time_display = serializers.SerializerMethodField()
    is_guest = serializers.SerializerMethodField()
    has_published_post = serializers.SerializerMethodField()
    workflow_status = serializers.SerializerMethodField()
    recipe_thumbs = serializers.SerializerMethodField()
    recipe_previews = serializers.SerializerMethodField()
    recipes_count = serializers.SerializerMethodField()
    people_count = serializers.SerializerMethodField()
    people_preview = serializers.SerializerMethodField()

    class Meta:
        model = MealPlan
        fields = [
            'id',
            'date',
            'meal_time',
            'meal_time_display',
            'slot_key',
            'custom_label',
            'scheduled_time',
            'confirmed',
            'guest_count',
            'is_guest',
            'has_published_post',
            'workflow_status',
            'recipe_thumbs',
            'recipe_previews',
            'recipes_count',
            'people_count',
            'people_preview',
        ]

    def get_meal_time_display(self, obj: MealPlan):
        if obj.meal_time == 'other' and getattr(obj, 'custom_label', None):
            return (obj.custom_label or '').strip() or obj.get_meal_time_display()
        return obj.get_meal_time_display()

    def get_is_guest(self, obj: MealPlan):
        if hasattr(obj, 'is_guest_annot'):
            return bool(getattr(obj, 'is_guest_annot'))
        request = self.context.get('request')
        if not request or not request.user or not request.user.is_authenticated:
            return False
        from .models import MealInvitation
        return MealInvitation.objects.filter(meal_plan=obj, invitee=request.user, status='accepted').exists()

    def get_has_published_post(self, obj: MealPlan):
        if hasattr(obj, 'has_published_post_annot'):
            return bool(getattr(obj, 'has_published_post_annot'))
        from .models import Post
        return Post.objects.filter(meal_plan=obj, is_published=True).exists()

    def get_workflow_status(self, obj: MealPlan):
        if self.get_has_published_post(obj):
            return {'key': 'done', 'label': 'Terminé'}
        return {'key': 'in_progress', 'label': 'En cuisine'}

    def get_recipe_previews(self, obj: MealPlan):
        return _meal_plan_recipe_previews(obj)

    def get_recipe_thumbs(self, obj: MealPlan):
        """
        Retourne max 3 URLs d'images de recette liées au meal plan.
        Pré-requis perf: prefetch meal_plan_recipe_batches + select_related(recipe_batch__recipe).
        """
        thumbs = []
        try:
            mprbs = obj.meal_plan_recipe_batches.all()
        except Exception:
            mprbs = []
        for mprb in mprbs:
            recipe = getattr(getattr(mprb, 'recipe_batch', None), 'recipe', None)
            u = getattr(recipe, 'image_url', None) if recipe else None
            if u:
                thumbs.append(u)
            if len(thumbs) >= 3:
                break
        return thumbs

    def get_recipes_count(self, obj: MealPlan):
        """
        Nombre total de recettes associées au meal plan.
        Le front affiche max 2 thumbnails + un badge "+X" basé sur ce count.
        Pré-requis perf: prefetch meal_plan_recipe_batches.
        """
        try:
            mprbs = obj.meal_plan_recipe_batches.all()
        except Exception:
            return 0
        # `len(qs)` utilise le cache de prefetch si présent.
        try:
            return len(mprbs)
        except Exception:
            return 0

    def _invited_users_for_preview(self, obj: MealPlan):
        """
        Retourne liste d'users invités actifs (accepted/pending) si invitations prefetched.
        """
        try:
            invitations = obj.invitations.all()
        except Exception:
            invitations = []
        invited = []
        for inv in invitations:
            if getattr(inv, 'status', None) not in ('accepted', 'pending'):
                continue
            u = getattr(inv, 'invitee', None)
            if u:
                invited.append(u)
        return invited

    def get_people_count(self, obj: MealPlan):
        host = getattr(obj, 'user', None)
        invited = self._invited_users_for_preview(obj)
        seen = set()
        count = 0
        if host and getattr(host, 'id', None) is not None:
            seen.add(str(host.id))
            count += 1
        for u in invited:
            uid = getattr(u, 'id', None)
            if uid is None:
                continue
            key = str(uid)
            if key in seen:
                continue
            seen.add(key)
            count += 1
        return count

    def get_people_preview(self, obj: MealPlan):
        """
        Max 3 personnes (host inclus) sous forme {id, name, avatar}.
        Le front gère les URLs relatives via sa fonction avatarFromUser si besoin.
        """
        host = getattr(obj, 'user', None)
        invited = self._invited_users_for_preview(obj)

        def _light(u):
            """
            IMPORTANT: `u.avatar_url` est souvent un chemin S3 (avatars/...)
            et DOIT être converti en URL presignée pour le mobile.
            """
            try:
                return UserLightSerializer(u, context=self.context).data
            except Exception:
                return None

        def _name(u):
            data = _light(u) or {}
            return (data.get('display_name') or data.get('username') or getattr(u, 'email', None) or 'Utilisateur')

        def _avatar(u):
            data = _light(u) or {}
            # `UserLightSerializer.avatar_url` est presigné si nécessaire.
            return data.get('avatar_url') or getattr(u, 'avatar', None) or getattr(u, 'profile_picture', None) or getattr(u, 'picture', None)

        out = []
        seen = set()
        if host and getattr(host, 'id', None) is not None:
            out.append({'id': host.id, 'name': _name(host), 'avatar': _avatar(host)})
            seen.add(str(host.id))
        for u in invited:
            if len(out) >= 3:
                break
            uid = getattr(u, 'id', None)
            if uid is None:
                continue
            key = str(uid)
            if key in seen:
                continue
            seen.add(key)
            out.append({'id': uid, 'name': _name(u), 'avatar': _avatar(u)})
        return out


class MealPlanTimelineForInvitationSerializer(serializers.ModelSerializer):
    meal_time_display = serializers.SerializerMethodField()
    recipe_thumbs = serializers.SerializerMethodField()
    recipe_previews = serializers.SerializerMethodField()
    recipes_count = serializers.SerializerMethodField()

    class Meta:
        model = MealPlan
        fields = [
            'id',
            'date',
            'meal_time',
            'meal_time_display',
            'slot_key',
            'custom_label',
            'scheduled_time',
            'recipe_thumbs',
            'recipe_previews',
            'recipes_count',
        ]

    def get_meal_time_display(self, obj: MealPlan):
        if obj.meal_time == 'other' and getattr(obj, 'custom_label', None):
            return (obj.custom_label or '').strip() or obj.get_meal_time_display()
        return obj.get_meal_time_display()

    def get_recipe_previews(self, obj: MealPlan):
        return _meal_plan_recipe_previews(obj)

    def get_recipe_thumbs(self, obj: MealPlan):
        thumbs = []
        try:
            mprbs = obj.meal_plan_recipe_batches.all()
        except Exception:
            mprbs = []
        for mprb in mprbs:
            recipe = getattr(getattr(mprb, 'recipe_batch', None), 'recipe', None)
            u = getattr(recipe, 'image_url', None) if recipe else None
            if u:
                thumbs.append(u)
            if len(thumbs) >= 3:
                break
        return thumbs

    def get_recipes_count(self, obj: MealPlan):
        try:
            mprbs = obj.meal_plan_recipe_batches.all()
        except Exception:
            return 0
        try:
            return len(mprbs)
        except Exception:
            return 0


class MealInvitationTimelineListSerializer(serializers.ModelSerializer):
    inviter = UserLightSerializer(read_only=True)
    meal_plan = MealPlanTimelineForInvitationSerializer(read_only=True)

    class Meta:
        model = MealInvitation
        fields = [
            'id',
            'inviter',
            'meal_plan',
            'status',
        ]


class MealInvitationListSerializer(serializers.ModelSerializer):
    """
    Serializer léger pour la liste des invitations (GET /meal-invitations/).
    Évite MealPlanSerializer (recettes, participants, etc.) et UserSerializer (is_following, etc.).
    """
    inviter = UserLightSerializer(read_only=True)
    invitee = UserLightSerializer(read_only=True)
    meal_plan = MealPlanLightForInvitationSerializer(read_only=True)
    status_display = serializers.CharField(source='get_status_display', read_only=True)

    class Meta:
        model = MealInvitation
        fields = [
            'id', 'inviter', 'invitee', 'meal_plan', 'status', 'status_display',
            'created_at', 'updated_at',
        ]


class MealInvitationSerializer(serializers.ModelSerializer):
    from accounts.serializers import UserSerializer
    inviter = UserSerializer(read_only=True)
    invitee = UserSerializer(read_only=True)
    meal_plan = MealPlanSerializer(read_only=True)
    status_display = serializers.CharField(source='get_status_display', read_only=True)
    
    class Meta:
        model = MealInvitation
        fields = [
            'id',
            'inviter',
            'invitee',
            'meal_plan',
            'status',
            'status_display',
            'created_at',
            'updated_at',
        ]
        read_only_fields = ['id', 'created_at', 'updated_at', 'inviter', 'invitee', 'meal_plan', 'status_display']

class MealPlanRangeListSerializer(serializers.ModelSerializer):
    """
    Lightweight list serializer for ranged listing:
    - removes user/shared_with to reduce payload
    """
    user = UserLightSerializer(read_only=True)
    recipe = RecipeLightSerializer(read_only=True)  # Garder pour compatibilité
    recipes = MealPlanRecipeSerializer(source='meal_plan_recipe_batches', many=True, read_only=True)
    meal_time_display = serializers.SerializerMethodField()
    meal_type_display = serializers.CharField(source='get_meal_type_display', read_only=True)
    participants = serializers.SerializerMethodField()
    total_guest_count = serializers.SerializerMethodField()
    total_participants = serializers.SerializerMethodField()
    total_servings = serializers.SerializerMethodField()
    groupedDates = serializers.SerializerMethodField()
    is_guest = serializers.SerializerMethodField()

    def get_meal_time_display(self, obj: MealPlan):
        if obj.meal_time == 'other' and getattr(obj, 'custom_label', None):
            return (obj.custom_label or '').strip() or obj.get_meal_time_display()
        return obj.get_meal_time_display()
    
    class Meta:
        model = MealPlan
        fields = [
            'id', 'date', 'meal_time', 'meal_time_display', 'slot_key', 'custom_label', 'scheduled_time',
            'meal_type', 'meal_type_display', 'confirmed',
            'user', 'participants',
            'recipe', 'recipes', 'total_guest_count', 'total_participants', 'total_servings',
            'groupedDates',
            'is_guest',
        ]
    
    def get_participants(self, obj):
        """
        Participants pour la timeline (léger):
        - invitations accepted/pending
        - user serialisé via UserLightSerializer (inclut avatar_url)
        """
        try:
            invitations = getattr(obj, 'invitations', None)
            if invitations is None:
                return []
            active = invitations.filter(status__in=['accepted', 'pending'])
            out = []
            for inv in active:
                u = getattr(inv, 'invitee', None)
                if not u:
                    continue
                out.append({
                    'user': UserLightSerializer(u, context=self.context).data,
                    'status': inv.status,
                })
            return out
        except Exception:
            return []

    def get_total_guest_count(self, obj: MealPlan):
        """
        Retourne le total_guest_count pré-calculé si disponible.
        Sinon retourne simplement guest_count (meal plan non groupé).
        """
        if hasattr(obj, '_total_guest_count'):
            return obj._total_guest_count
        return obj.guest_count or 0
    
    def get_total_participants(self, obj: MealPlan):
        """
        Retourne les participants groupés pré-calculés si disponibles.
        Sinon retourne une liste vide (meal plan non groupé).
        """
        if hasattr(obj, '_total_participants'):
            precedence = {'accepted': 3, 'pending': 2, 'declined': 1}
            by_user_id = {}
            for p in obj._total_participants:
                uid = p['user'].id
                existing = by_user_id.get(uid)
                if not existing or precedence.get(p['status'], 0) > precedence.get(existing['status'], 0):
                    by_user_id[uid] = {
                        'user': UserLightSerializer(p['user'], context=self.context).data,
                        'status': p['status'],
                    }
            return list(by_user_id.values())
        
        # Fallback : retourner une liste vide pour les meal plans non groupés
        return []
    
    def get_total_servings(self, obj: MealPlan):
        """
        Calcule le nombre total de personnes pour ce meal plan.
        Pour un meal plan simple : 1 + participants actifs + guest_count
        Pour un meal plan groupé : utilise _total_servings pré-calculé
        """
        # Si on a un total_servings pré-calculé (meal plan groupé), l'utiliser
        if hasattr(obj, '_total_servings'):
            return obj._total_servings
        
        # Sinon, calculer pour un meal plan simple
        # Utiliser total_participants si disponible (groupé), sinon participants
        participants_to_use = None
        if hasattr(obj, '_total_participants'):
            participants_to_use = obj._total_participants
        else:
            # Pour MealPlanRangeListSerializer, on n'a pas get_participants, donc utiliser une liste vide
            participants_to_use = []
        
        # Compter uniquement les participants actifs (accepted ou pending)
        active_participants_count = sum(
            1 for p in participants_to_use
            if isinstance(p, dict) and p.get('status') in ['accepted', 'pending']
        )
        
        # Utiliser total_guest_count si disponible (groupé), sinon guest_count
        guest_count_to_use = self.get_total_guest_count(obj)
        
        return 1 + active_participants_count + guest_count_to_use
    
    def get_groupedDates(self, obj: MealPlan):
        """Calculer groupedDates en agrégeant les dates de toutes les recettes groupées."""
        ctx = getattr(self, 'context', {}) or {}
        mapping = ctx.get('grouped_dates_by_batch_id') or {}
        dates = set()
        for mprb in obj.meal_plan_recipe_batches.all():
            if mprb.recipe_batch_id:
                if mprb.recipe_batch_id in mapping:
                    for ds in mapping.get(mprb.recipe_batch_id) or []:
                        dates.add(ds)
                else:
                    for mp in MealPlan.objects.filter(meal_plan_recipe_batches__recipe_batch_id=mprb.recipe_batch_id):
                        dates.add(mp.date.isoformat())
            else:
                dates.add(obj.date.isoformat())
        return sorted(list(dates)) if dates else [obj.date.isoformat()]

    def get_is_guest(self, obj: MealPlan):
        """
        True si l'utilisateur courant a une invitation acceptée pour ce repas.
        """
        request = self.context.get('request')
        if not request or not request.user or not request.user.is_authenticated:
            return False
        from .models import MealInvitation
        return MealInvitation.objects.filter(meal_plan=obj, invitee=request.user, status='accepted').exists()


class MealPlanMinimalListSerializer(serializers.ModelSerializer):
    """
    Serializer ultra-léger pour le mode minimal :
    - Seulement les champs essentiels (id, date, meal_time, meal_type)
    - PAS de recipe ni recipes (payload léger pour le calendrier)
    - Pas de groupedDates, total_servings, total_participants, total_guest_count
    - Pas de calculs coûteux sur les groupes
    """
    meal_time_display = serializers.SerializerMethodField()
    meal_type_display = serializers.CharField(source='get_meal_type_display', read_only=True)
    is_guest = serializers.SerializerMethodField()
    inviter_name = serializers.SerializerMethodField()
    user = UserLightSerializer(read_only=True)

    def get_meal_time_display(self, obj: MealPlan):
        if obj.meal_time == 'other' and getattr(obj, 'custom_label', None):
            return obj.custom_label
        return obj.get_meal_time_display()
    
    class Meta:
        model = MealPlan
        fields = [
            'id', 'date', 'meal_time', 'meal_time_display', 'slot_key', 'custom_label', 'scheduled_time',
            'meal_type', 'meal_type_display', 'confirmed', 'is_guest', 'inviter_name', 'user',
        ]
    
    def get_is_guest(self, obj: MealPlan):
        """
        Vérifie si l'utilisateur actuel a une invitation acceptée pour ce meal plan.
        """
        request = self.context.get('request')
        if not request or not request.user or not request.user.is_authenticated:
            return False
        
        from .models import MealInvitation
        return MealInvitation.objects.filter(
            meal_plan=obj,
            invitee=request.user,
            status='accepted'
        ).exists()
    
    def get_inviter_name(self, obj: MealPlan):
        """
        Retourne le nom de l'inviteur si l'utilisateur actuel est invité.
        """
        request = self.context.get('request')
        if not request or not request.user or not request.user.is_authenticated:
            return None
        
        from .models import MealInvitation
        invitation = MealInvitation.objects.filter(
            meal_plan=obj,
            invitee=request.user,
            status='accepted'
        ).select_related('inviter').first()
        
        if invitation and invitation.inviter:
            return invitation.inviter.username or invitation.inviter.email
        return None


class MealPlanByDateSerializer(serializers.ModelSerializer):
    """
    Detailed list for by_date: include host and participants with status.
    """
    host = UserLightSerializer(source='user', read_only=True)
    recipes = MealPlanRecipeSerializer(source='meal_plan_recipe_batches', many=True, read_only=True)
    meal_time_display = serializers.SerializerMethodField()
    meal_type_display = serializers.CharField(source='get_meal_type_display', read_only=True)
    participants = serializers.SerializerMethodField()
    total_guest_count = serializers.SerializerMethodField()
    total_participants = serializers.SerializerMethodField()
    total_servings = serializers.SerializerMethodField()
    groupedDates = serializers.SerializerMethodField()
    is_guest = serializers.SerializerMethodField()
    inviter_name = serializers.SerializerMethodField()
    
    class Meta:
        model = MealPlan
        fields = [
            'id', 'date', 'meal_time', 'meal_time_display', 'slot_key', 'custom_label', 'scheduled_time',
            'meal_type', 'meal_type_display', 'confirmed',
            'recipes', 'host', 'participants', 'guest_count', 
            'total_guest_count', 'total_participants', 'total_servings',
            'groupedDates', 'is_guest', 'inviter_name',
        ]

    def get_meal_time_display(self, obj: MealPlan):
        if obj.meal_time == 'other' and getattr(obj, 'custom_label', None):
            return obj.custom_label
        return obj.get_meal_time_display()
    
    def get_is_guest(self, obj: MealPlan):
        """
        Vérifie si l'utilisateur actuel a une invitation acceptée pour ce meal plan.
        """
        request = self.context.get('request')
        if not request or not request.user or not request.user.is_authenticated:
            return False
        
        from .models import MealInvitation
        return MealInvitation.objects.filter(
            meal_plan=obj,
            invitee=request.user,
            status='accepted'
        ).exists()
    
    def get_inviter_name(self, obj: MealPlan):
        """
        Retourne le nom de l'inviteur si l'utilisateur actuel est invité.
        """
        request = self.context.get('request')
        if not request or not request.user or not request.user.is_authenticated:
            return None
        
        from .models import MealInvitation
        invitation = MealInvitation.objects.filter(
            meal_plan=obj,
            invitee=request.user,
            status='accepted'
        ).select_related('inviter').first()
        
        if invitation and invitation.inviter:
            return invitation.inviter.username or invitation.inviter.email
        return None
    
    def get_participants(self, obj: MealPlan):
        from .models import MealInvitation
        # Utiliser le prefetch si disponible (via Prefetch), sinon faire une requête
        # obj.invitations.all() utilisera automatiquement le cache si prefetch est fait
        invitations = obj.invitations.all() if hasattr(obj, 'invitations') else MealInvitation.objects.filter(meal_plan=obj).select_related('invitee')
        # Uniq par user avec priorité accepted > pending > declined
        precedence = {'accepted': 3, 'pending': 2, 'declined': 1}
        by_user_id = {}
        for inv in invitations:
            uid = inv.invitee.id
            existing = by_user_id.get(uid)
            if not existing or precedence.get(inv.status, 0) > precedence.get(existing['status'], 0):
                by_user_id[uid] = {
                    'user': UserLightSerializer(inv.invitee, context=self.context).data,
                    'status': inv.status,
                }
        return list(by_user_id.values())
    
    def get_total_guest_count(self, obj: MealPlan):
        """
        Retourne le total_guest_count pré-calculé dans by_date.
        Si pas pré-calculé, retourne simplement guest_count (meal plan non groupé).
        """
        if hasattr(obj, '_total_guest_count'):
            return obj._total_guest_count
        return obj.guest_count or 0
    
    def get_total_participants(self, obj: MealPlan):
        """
        Retourne les participants groupés pré-calculés dans by_date.
        Si pas pré-calculé, retourne les participants du meal plan individuel.
        """
        if hasattr(obj, '_total_participants'):
            precedence = {'accepted': 3, 'pending': 2, 'declined': 1}
            by_user_id = {}
            for p in obj._total_participants:
                uid = p['user'].id
                existing = by_user_id.get(uid)
                if not existing or precedence.get(p['status'], 0) > precedence.get(existing['status'], 0):
                    by_user_id[uid] = {
                        'user': UserLightSerializer(p['user'], context=self.context).data,
                        'status': p['status'],
                    }
            return list(by_user_id.values())
        
        # Fallback : utiliser get_participants normal
        return self.get_participants(obj)
    
    def get_total_servings(self, obj: MealPlan):
        """
        Calcule le nombre total de personnes pour ce meal plan.
        Pour un meal plan simple : 1 + participants actifs + guest_count
        Pour un meal plan groupé : utilise _total_servings pré-calculé
        """
        # Si on a un total_servings pré-calculé (meal plan groupé), l'utiliser
        if hasattr(obj, '_total_servings'):
            return obj._total_servings
        
        # Sinon, calculer pour un meal plan simple
        # Utiliser total_participants si disponible (groupé), sinon participants
        participants_to_use = None
        if hasattr(obj, '_total_participants'):
            participants_to_use = obj._total_participants
        else:
            participants_to_use = self.get_participants(obj)
        
        # Compter uniquement les participants actifs (accepted ou pending)
        active_participants_count = sum(
            1 for p in participants_to_use
            if isinstance(p, dict) and p.get('status') in ['accepted', 'pending']
        )
        
        # Utiliser total_guest_count si disponible (groupé), sinon guest_count
        guest_count_to_use = self.get_total_guest_count(obj)
        
        return 1 + active_participants_count + guest_count_to_use
    
    def get_groupedDates(self, obj: MealPlan):
        """Calculer groupedDates en agrégeant les dates de toutes les recettes groupées."""
        dates = set()
        for mprb in obj.meal_plan_recipe_batches.all():
            if mprb.recipe_batch_id:
                for mp in MealPlan.objects.filter(meal_plan_recipe_batches__recipe_batch_id=mprb.recipe_batch_id):
                    dates.add(mp.date.isoformat())
            else:
                dates.add(obj.date.isoformat())
        return sorted(list(dates)) if dates else [obj.date.isoformat()]


class CookingProgressSerializer(serializers.ModelSerializer):
    recipe_title = serializers.CharField(source='recipe_batch.recipe.title', read_only=True)
    recipe_image_url = serializers.URLField(source='recipe_batch.recipe.image_url', read_only=True)
    status_display = serializers.CharField(source='get_status_display', read_only=True)
    
    class Meta:
        model = CookingProgress
        fields = [
            'id', 'user', 'recipe_batch', 'recipe_title', 'recipe_image_url',
            'current_step_index', 'status', 'status_display',
            'started_at', 'completed_at', 'total_time_minutes',
            'created_at', 'updated_at'
        ]
        read_only_fields = ['user', 'started_at', 'created_at', 'updated_at']


class CookingProgressCreateUpdateSerializer(serializers.ModelSerializer):
    """Serializer pour créer/mettre à jour une progression"""
    
    class Meta:
        model = CookingProgress
        fields = [
            'recipe_batch', 'current_step_index', 'status',
            'completed_at', 'total_time_minutes'
        ]
        read_only_fields = []
    
    def create(self, validated_data):
        validated_data['user'] = self.context['request'].user
        return super().create(validated_data)


class TimerSerializer(serializers.ModelSerializer):
    recipe_title = serializers.CharField(source='recipe_batch.recipe.title', read_only=True)
    step_title = serializers.CharField(source='step.title', read_only=True)
    step_order = serializers.IntegerField(source='step.order', read_only=True)
    # Identifiants pratiques pour le front
    recipe = serializers.IntegerField(source='recipe_batch.recipe_id', read_only=True)
    meal_plan = serializers.SerializerMethodField()
    
    class Meta:
        model = Timer
        fields = [
            'id',
            'user',
            'cooking_progress',
            'step',
            'step_title',
            'step_order',
            'recipe_batch',
            'recipe',
            'recipe_title',
            'meal_plan',
            'duration_minutes',
            'remaining_seconds',
            'started_at',
            'expires_at',
            'is_completed',
            'created_at',
            'updated_at',
        ]
        read_only_fields = ['user', 'started_at', 'expires_at', 'created_at', 'updated_at']

    def get_meal_plan(self, obj):
        """
        Retourne un meal_plan associé à ce timer (via RecipeBatch) si disponible.
        Utile pour la navigation depuis un timer vers la recette.
        """
        from .models import MealPlanRecipeBatch

        if not obj.recipe_batch_id:
            return None

        mprb = (
            MealPlanRecipeBatch.objects.filter(
                recipe_batch=obj.recipe_batch
            )
            .select_related('meal_plan')
            .order_by('id')
            .first()
        )
        return mprb.meal_plan_id if mprb else None


class TimerCreateSerializer(serializers.ModelSerializer):
    """Serializer pour créer un minuteur"""
    
    class Meta:
        model = Timer
        fields = [
            'cooking_progress', 'step', 'recipe_batch', 'duration_minutes', 'remaining_seconds'
        ]
    
    def validate(self, attrs):
        """
        Empêche la création de timers sans batch.
        """
        if not attrs.get('recipe_batch'):
            raise serializers.ValidationError(
                {'recipe_batch': 'Un recipe_batch est requis pour créer un minuteur.'}
            )
        return super().validate(attrs)

    def create(self, validated_data):
        from django.utils import timezone
        validated_data['user'] = self.context['request'].user
        # Calculer expires_at basé sur remaining_seconds
        remaining_seconds = validated_data.get('remaining_seconds', validated_data.get('duration_minutes', 0) * 60)
        validated_data['expires_at'] = timezone.now() + timezone.timedelta(seconds=remaining_seconds)
        if 'remaining_seconds' not in validated_data:
            validated_data['remaining_seconds'] = remaining_seconds
        return super().create(validated_data)


class PostPhotoLightSerializer(serializers.ModelSerializer):
    """Serializer léger pour la galerie de photos (endpoint /meal-plans/{id}/photos/)"""
    presigned_url = serializers.SerializerMethodField()
    captured_label = serializers.SerializerMethodField()
    time_display = serializers.SerializerMethodField()
    
    class Meta:
        model = PostPhoto
        fields = ['id', 'photo_type', 'presigned_url', 'captured_label', 'time_display']
    
    def get_presigned_url(self, obj):
        """Générer une URL pré-signée pour l'image"""
        if not obj.image_path:
            return None
        
        from django.conf import settings
        import boto3
        
        # Si pas de configuration S3, retourner None
        if not settings.AWS_ACCESS_KEY_ID or not settings.AWS_SECRET_ACCESS_KEY or not settings.AWS_BUCKET:
            return None
        
        try:
            # Nettoyer le chemin (enlever le préfixe s3:/ si présent)
            clean_path = obj.image_path.replace('s3:/', '').lstrip('/')
            
            # Configurer le client S3
            s3_config = {
                'aws_access_key_id': settings.AWS_ACCESS_KEY_ID,
                'aws_secret_access_key': settings.AWS_SECRET_ACCESS_KEY,
                'region_name': settings.AWS_S3_REGION_NAME
            }
            
            if settings.AWS_ENDPOINT:
                s3_config['endpoint_url'] = settings.AWS_ENDPOINT
                if settings.AWS_ENDPOINT.startswith('http://'):
                    s3_config['use_ssl'] = False
            
            s3_client = boto3.client('s3', **s3_config)
            
            # Générer l'URL pré-signée (valide 1 heure)
            presigned_url = s3_client.generate_presigned_url(
                'get_object',
                Params={
                    'Bucket': settings.AWS_BUCKET,
                    'Key': clean_path,
                },
                ExpiresIn=3600  # 1 heure
            )
            
            return presigned_url
        except Exception as e:
            # En cas d'erreur, retourner None
            print(f"⚠️ Error generating presigned URL: {e}")
            return None
    
    def get_captured_label(self, obj):
        base_labels = {
            'during_cooking': 'Pendant la recette',
            'after_cooking': 'Après la recette',
            'at_meal_time': 'À table',
            'spontaneous': 'Moment spontané',
            'imported_after_cooking': 'Importée après la recette',
        }
        label = base_labels.get(obj.photo_type, obj.photo_type)
        if obj.step and obj.step.order is not None:
            label += f" • Étape {obj.step.order}"
        return label
    
    def get_time_display(self, obj):
        if not obj.created_at:
            return None
        return obj.created_at.strftime('%d %b • %H:%M')


class PostPhotoSerializer(serializers.ModelSerializer):
    photo_type_display = serializers.CharField(source='get_photo_type_display', read_only=True)
    step_order = serializers.IntegerField(source='step.order', read_only=True)
    step_title = serializers.CharField(source='step.title', read_only=True)
    captured_label = serializers.SerializerMethodField()
    time_display = serializers.SerializerMethodField()
    editable = serializers.SerializerMethodField()
    # Optimisation: exposer les *_id natifs (évite d'accéder à la relation FK)
    recipe_batch_id = serializers.IntegerField(read_only=True, allow_null=True)
    post_id = serializers.IntegerField(read_only=True, allow_null=True)
    uploaded_by_id = serializers.IntegerField(read_only=True, allow_null=True)
    uploaded_by_username = serializers.CharField(source='uploaded_by.username', read_only=True, allow_null=True)
    image_url = serializers.SerializerMethodField()
    presigned_url = serializers.SerializerMethodField()
    
    class Meta:
        model = PostPhoto
        fields = [
            'id', 'photo_type', 'photo_type_display', 'image_path', 'image_url', 'presigned_url',
            'step', 'step_order', 'step_title', 'captured_label',
            'time_display', 'recipe_batch_id', 'post_id', 'uploaded_by_id', 'uploaded_by_username',
            'editable', 'order', 'is_draft', 'created_at'
        ]
        read_only_fields = ['created_at']
    
    def get_image_url(self, obj):
        """Construire l'URL complète à partir du chemin relatif"""
        from savr_back.settings import build_s3_url
        if not obj.image_path:
            return None
        return build_s3_url(obj.image_path)
    
    def get_presigned_url(self, obj):
        """
        Générer une URL pré-signée pour l'image.
        
        IMPORTANT : Les presigned URLs sont NÉCESSAIRES si le bucket S3 n'est pas public.
        Ne pas désactiver cette fonctionnalité même pour optimiser les performances.
        """
        # Option pour sauter la génération des presigned URLs (optimisation liste)
        if self.context.get('skip_presign'):
            return self.get_image_url(obj)
        
        if not obj.image_path:
            return None
        
        from django.conf import settings
        from savr_back.settings import build_presigned_get_url
        
        # Si pas de configuration S3, retourner l'URL directe
        if not settings.AWS_ACCESS_KEY_ID or not settings.AWS_SECRET_ACCESS_KEY or not settings.AWS_BUCKET:
            return self.get_image_url(obj)
        
        try:
            # TOUJOURS générer une presigned URL pour garantir l'accès aux images
            # Même si cela prend un peu de temps, c'est essentiel pour la sécurité
            presigned_url = build_presigned_get_url(obj.image_path)
            return presigned_url
        except Exception as e:
            # En cas d'erreur, retourner l'URL directe en espérant que le bucket est public
            print(f"⚠️ Error generating presigned URL: {e}")
            return self.get_image_url(obj)
    
    def get_captured_label(self, obj):
        base_labels = {
            'during_cooking': 'Pendant la recette',
            'after_cooking': 'Après la recette',
            'at_meal_time': 'À table',
            'spontaneous': 'Moment spontané',
            'imported_after_cooking': 'Importée après la recette',
        }
        label = base_labels.get(obj.photo_type, obj.photo_type)
        if obj.step and obj.step.order is not None:
            label += f" • Étape {obj.step.order}"
        return label
    
    def get_time_display(self, obj):
        if not obj.created_at:
            return None
        return obj.created_at.strftime('%d %b • %H:%M')
    
    def get_editable(self, obj):
        request = self.context.get('request')
        if not request or request.user.is_anonymous:
            return False

        if obj.post_id:
            return False

        if obj.uploaded_by_id and obj.uploaded_by_id != request.user.id:
            return False

        if obj.recipe_batch_id:
            from .models import RecipeBatch, MealPlan
            accessible_meal_plan_filter = get_accessible_meal_plan_filter(request.user)
            has_access = RecipeBatch.objects.filter(
                id=obj.recipe_batch_id,
                meal_plan_recipe_batches__meal_plan__in=MealPlan.objects.filter(
                    accessible_meal_plan_filter
                )
            ).exists()
            return has_access

        if getattr(obj, 'meal_plan_id', None):
            from .models import MealPlan
            accessible_meal_plan_filter = get_accessible_meal_plan_filter(request.user)
            return MealPlan.objects.filter(id=obj.meal_plan_id).filter(
                accessible_meal_plan_filter
            ).exists()

        return False


class PostSerializer(serializers.ModelSerializer):
    photos = PostPhotoSerializer(many=True, read_only=True)
    user = UserLightSerializer(read_only=True)
    recipe_batch = RecipeBatchLightSerializer(read_only=True)
    meal_plan = serializers.SerializerMethodField()
    photos_count = serializers.IntegerField(read_only=True)
    has_all_photos = serializers.BooleanField(read_only=True)
    recipe_meta = serializers.SerializerMethodField()
    recipe = serializers.SerializerMethodField()
    recipes = serializers.SerializerMethodField()
    cookies_count = serializers.SerializerMethodField()
    has_cookie_from_user = serializers.SerializerMethodField()
    comments_count = serializers.SerializerMethodField()

    class Meta:
        model = Post
        fields = [
            'id', 'user', 'recipe_batch', 'meal_plan',
            'comment', 'cooking_time_minutes', 'is_published', 'recipe_meta', 'recipe', 'recipes',
            'photos', 'photos_count', 'has_all_photos',
            'cookies_count', 'has_cookie_from_user', 'comments_count',
            'created_at', 'updated_at'
        ]
        read_only_fields = ['user', 'created_at', 'updated_at']

    def get_meal_plan(self, obj):
        mp = obj.meal_plan
        if not mp:
            return None
        return {
            'id': mp.id,
            'date': mp.date.isoformat() if mp.date else None,
            'meal_time': mp.meal_time,
            'meal_type': mp.meal_type,
            'meal_type_display': mp.get_meal_type_display(),
            **meal_plan_slot_api_fields(mp),
        }
    
    def get_recipe(self, obj):
        """Retourner les infos de base de la recette pour compatibilité avec PostDetailModal"""
        recipe = obj.recipe_batch.recipe if obj.recipe_batch else None
        if not recipe:
            return None
        return {
            'id': recipe.id,
            'title': recipe.title,
            'image_url': getattr(recipe, 'image_url', None),
        }

    def get_recipes(self, obj):
        """Toutes les recettes du post (repas multi-recettes ou batches des photos)."""
        entries = []
        seen_batch_ids = set()

        def recipe_payload(recipe):
            if not recipe:
                return None
            return {
                'id': recipe.id,
                'title': recipe.title,
                'image_url': getattr(recipe, 'image_url', None),
                'prep_time': recipe.prep_time,
                'cook_time': recipe.cook_time,
                'servings': recipe.servings,
            }

        def add_batch(batch, order=None):
            if not batch or not batch.id or batch.id in seen_batch_ids:
                return
            recipe = getattr(batch, 'recipe', None)
            if not recipe or not recipe.id:
                return
            seen_batch_ids.add(batch.id)
            entries.append({
                'recipe_batch_id': batch.id,
                'order': order if order is not None else len(entries),
                'recipe': recipe_payload(recipe),
            })

        if obj.meal_plan_id and obj.meal_plan:
            mprbs = obj.meal_plan.meal_plan_recipe_batches.all()
            for idx, mprb in enumerate(mprbs):
                add_batch(getattr(mprb, 'recipe_batch', None), order=getattr(mprb, 'order', idx))

        if not entries:
            for photo in obj.photos.all():
                add_batch(getattr(photo, 'recipe_batch', None))

        if obj.recipe_batch_id:
            add_batch(obj.recipe_batch)

        entries.sort(key=lambda item: item.get('order', 0))
        return entries
    
    def get_recipe_meta(self, obj):
        recipe = obj.recipe_batch.recipe if obj.recipe_batch else None
        if not recipe:
            return None
        if obj.cooking_time_minutes is not None:
            total_time = obj.cooking_time_minutes
        else:
            total_time = (recipe.prep_time or 0) + (recipe.cook_time or 0)
        servings = recipe.servings or 1
        shared_with = 1
        # Optimisation : éviter la requête supplémentaire si possible
        # Pour l'instant, on simplifie en ne comptant que l'utilisateur
        # (on peut précharger les invitations plus tard si nécessaire)
        return {
            'title': recipe.title,
            'total_time': total_time,
            'servings': servings,
            'shared_with': shared_with,
        }
    
    def get_cookies_count(self, obj):
        """Nombre total de cookies sur le post - utilise les données préchargées"""
        # Si les cookies sont déjà préchargés, utiliser len() au lieu de count()
        if hasattr(obj, '_prefetched_objects_cache') and 'cookies' in obj._prefetched_objects_cache:
            return len(obj._prefetched_objects_cache['cookies'])
        return obj.cookies.count()
    
    def get_has_cookie_from_user(self, obj):
        """Vérifie si l'utilisateur actuel a donné un cookie à ce post - utilise les données préchargées"""
        request = self.context.get('request')
        if not request or request.user.is_anonymous:
            return False
        # Si les cookies sont déjà préchargés, vérifier en mémoire
        if hasattr(obj, '_prefetched_objects_cache') and 'cookies' in obj._prefetched_objects_cache:
            return any(cookie.user_id == request.user.id for cookie in obj._prefetched_objects_cache['cookies'])
        return obj.cookies.filter(user=request.user).exists()

    def get_comments_count(self, obj):
        if hasattr(obj, '_prefetched_objects_cache') and 'comments' in obj._prefetched_objects_cache:
            return len(obj._prefetched_objects_cache['comments'])
        return obj.comments.count()


class PostPhotoListSerializer(serializers.ModelSerializer):
    """Version allégée pour la liste de posts (uniquement les URLs)."""
    image_url = serializers.SerializerMethodField()
    presigned_url = serializers.SerializerMethodField()
    recipe_batch_id = serializers.IntegerField(read_only=True, allow_null=True)
    
    class Meta:
        model = PostPhoto
        fields = ['id', 'photo_type', 'image_url', 'presigned_url', 'order', 'recipe_batch_id']
    
    def get_image_url(self, obj):
        from savr_back.settings import build_s3_url
        if not obj.image_path:
            return None
        return build_s3_url(obj.image_path)
    
    def get_presigned_url(self, obj):
        if self.context.get('skip_presign'):
            return self.get_image_url(obj)
        if not obj.image_path:
            return None
        from django.conf import settings
        from savr_back.settings import build_presigned_get_url
        if not settings.AWS_ACCESS_KEY_ID or not settings.AWS_SECRET_ACCESS_KEY or not settings.AWS_BUCKET:
            return self.get_image_url(obj)
        try:
            return build_presigned_get_url(obj.image_path)
        except Exception:
            return self.get_image_url(obj)


class PostCommentSerializer(serializers.ModelSerializer):
    """Serializer pour les commentaires sur un post"""
    user = UserLightSerializer(read_only=True)
    likes_count = serializers.SerializerMethodField()
    has_like_from_user = serializers.SerializerMethodField()

    class Meta:
        model = PostComment
        fields = ['id', 'user', 'text', 'created_at', 'likes_count', 'has_like_from_user']
        read_only_fields = ['user', 'created_at', 'likes_count', 'has_like_from_user']

    def get_likes_count(self, obj):
        # Utiliser un attribut annoté si présent, sinon retomber sur le count()
        annotated = getattr(obj, 'likes_count', None)
        if annotated is not None:
            return annotated
        return obj.likes.count()

    def get_has_like_from_user(self, obj):
        request = self.context.get('request')
        if not request or request.user.is_anonymous:
            return False
        user = request.user
        # Si le queryset a pré-annoté un booléen, on le réutilise
        if hasattr(obj, 'liked_by_user'):
            return bool(obj.liked_by_user)
        return PostCommentLike.objects.filter(comment=obj, user=user).exists()


class PostCommentCreateSerializer(serializers.ModelSerializer):
    """Serializer pour créer un commentaire"""

    class Meta:
        model = PostComment
        fields = ['text']

    def create(self, validated_data):
        validated_data['post'] = self.context['post']
        validated_data['user'] = self.context['request'].user
        return super().create(validated_data)


class PostListSerializer(serializers.ModelSerializer):
    """Serializer minimal pour la liste des posts (feed)."""
    user = UserLightSerializer(read_only=True)
    photos = PostPhotoListSerializer(many=True, read_only=True)
    recipe = serializers.SerializerMethodField()
    recipe_batch = serializers.SerializerMethodField()
    meal_plan = serializers.SerializerMethodField()
    cookies_count = serializers.SerializerMethodField()
    has_cookie_from_user = serializers.SerializerMethodField()
    comments_count = serializers.SerializerMethodField()
    author_is_following = serializers.SerializerMethodField()
    
    class Meta:
        model = Post
        fields = [
            'id', 'user',
            'comment', 'cooking_time_minutes', 'is_published',
            'photos',
            'cookies_count', 'has_cookie_from_user', 'comments_count',
            'recipe', 'recipe_batch', 'meal_plan',
            'author_is_following',
            'created_at',
        ]
        read_only_fields = ['user', 'created_at']

    def get_meal_plan(self, obj):
        mp = obj.meal_plan
        if not mp:
            return None
        return {
            'id': mp.id,
            'date': mp.date.isoformat() if mp.date else None,
            'meal_time': mp.meal_time,
            'meal_type': mp.meal_type,
            'meal_type_display': mp.get_meal_type_display(),
            **meal_plan_slot_api_fields(mp),
        }

    def get_author_is_following(self, obj):
        request = self.context.get('request')
        if not request or not request.user.is_authenticated:
            return False
        if obj.user_id == request.user.id:
            return False
        annotated = getattr(obj, '_viewer_follows_post_author', None)
        if annotated is not None:
            return bool(annotated)
        from accounts.models import Follow
        return Follow.objects.filter(
            follower_id=request.user.id, following_id=obj.user_id
        ).exists()

    def to_representation(self, instance):
        data = super().to_representation(instance)
        # Même info que author_is_following, pour les clients qui lisent user.is_following
        nested = data.get('user')
        if isinstance(nested, dict):
            nested['is_following'] = bool(data.get('author_is_following'))
        if settings.DEBUG:
            import logging
            logger = logging.getLogger(__name__)
            logger.info(
                "[PostListSerializer] post_id=%s recipe_batch_id=%s photos=%s",
                instance.id,
                getattr(instance, 'recipe_batch_id', None),
                [
                    {
                        'id': p.get('id'),
                        'order': p.get('order'),
                        'recipe_batch_id': p.get('recipe_batch_id'),
                    }
                    for p in (data.get('photos') or [])
                ],
            )
        return data
    
    def get_recipe(self, obj):
        recipe = obj.recipe_batch.recipe if obj.recipe_batch else None
        if not recipe:
            return None
        prep = recipe.prep_time or 0
        cook = recipe.cook_time or 0
        recipe_total = prep + cook if (prep or cook) else None
        if obj.cooking_time_minutes is not None:
            total_time = obj.cooking_time_minutes
        else:
            total_time = recipe_total
        return {
            'id': recipe.id,
            'title': recipe.title,
            'image_url': getattr(recipe, 'image_url', None),
            'servings': recipe.servings,
            'total_time': total_time,
        }
    
    def get_recipe_batch(self, obj):
        """Retourner les infos minimales du batch pour le carnet"""
        if not obj.recipe_batch:
            return None
        batch = obj.recipe_batch
        recipe = batch.recipe if batch else None
        
        # Utiliser les données préchargées si disponibles pour éviter les requêtes N+1
        meal_plan_ids = []
        grouped_dates = []
        meals = []
        
        # Si les meal_plan_recipe_batches sont préchargés, les utiliser
        if hasattr(batch, '_prefetched_objects_cache') and 'meal_plan_recipe_batches' in batch._prefetched_objects_cache:
            mprbs = batch._prefetched_objects_cache['meal_plan_recipe_batches']
            meal_plan_ids = [mprb.meal_plan_id for mprb in mprbs if mprb.meal_plan_id]
            # Récupérer les meal plans depuis le cache si disponible
            if meal_plan_ids:
                from .models import MealPlan
                meal_plans = MealPlan.objects.filter(id__in=meal_plan_ids).only(
                    'id', 'date', 'meal_time', 'custom_label'
                )
                grouped_dates = sorted({mp.date.isoformat() for mp in meal_plans})
                meals = [
                    {
                        'id': mp.id,
                        'date': mp.date.isoformat(),
                        'meal_time': mp.meal_time,
                        **meal_plan_slot_api_fields(mp),
                    }
                    for mp in meal_plans
                ]
        else:
            # Fallback : faire une requête si les données ne sont pas préchargées
            from .models import MealPlanRecipeBatch, MealPlan
            meal_plan_ids = list(
                MealPlanRecipeBatch.objects.filter(recipe_batch=batch)
                .values_list('meal_plan_id', flat=True)
            )
            if meal_plan_ids:
                meal_plans = MealPlan.objects.filter(id__in=meal_plan_ids).only(
                    'id', 'date', 'meal_time', 'custom_label'
                )
                grouped_dates = sorted({mp.date.isoformat() for mp in meal_plans})
                meals = [
                    {
                        'id': mp.id,
                        'date': mp.date.isoformat(),
                        'meal_time': mp.meal_time,
                        **meal_plan_slot_api_fields(mp),
                    }
                    for mp in meal_plans
                ]
        
        return {
            'id': batch.id,
            'recipe': {
                'id': recipe.id if recipe else None,
                'title': recipe.title if recipe else None,
                'image_url': getattr(recipe, 'image_url', None) if recipe else None,
                'servings': recipe.servings if recipe else 1,
            },
            'groupedDates': grouped_dates,
            'meals': meals,
            'meal_plan_ids': meal_plan_ids,
        }
    
    def get_cookies_count(self, obj):
        if hasattr(obj, '_prefetched_objects_cache') and 'cookies' in obj._prefetched_objects_cache:
            return len(obj._prefetched_objects_cache['cookies'])
        return obj.cookies.count()
    
    def get_has_cookie_from_user(self, obj):
        request = self.context.get('request')
        if not request or request.user.is_anonymous:
            return False
        if hasattr(obj, '_prefetched_objects_cache') and 'cookies' in obj._prefetched_objects_cache:
            return any(cookie.user_id == request.user.id for cookie in obj._prefetched_objects_cache['cookies'])
        return obj.cookies.filter(user=request.user).exists()

    def get_comments_count(self, obj):
        if hasattr(obj, '_prefetched_objects_cache') and 'comments' in obj._prefetched_objects_cache:
            return len(obj._prefetched_objects_cache['comments'])
        return obj.comments.count()


class PostCreateUpdateSerializer(serializers.ModelSerializer):
    """Serializer pour créer/mettre à jour un post"""
    recipe_batch_id = serializers.PrimaryKeyRelatedField(
        queryset=RecipeBatch.objects.all(),
        source='recipe_batch',
        write_only=True
    )
    
    class Meta:
        model = Post
        fields = [
            'id', 'recipe_batch', 'recipe_batch_id', 'comment', 'is_published'
        ]
        read_only_fields = ['id']
    
    def create(self, validated_data):
        validated_data['user'] = self.context['request'].user
        return super().create(validated_data)


class ShoppingListSerializer(serializers.ModelSerializer):
    """Serializer pour une liste de courses (V2)"""
    members = serializers.SerializerMethodField()
    items_count = serializers.SerializerMethodField()
    is_complete = serializers.SerializerMethodField()

    class Meta:
        model = ShoppingList
        fields = [
            'id', 'name', 'color', 'is_archived',
            'members', 'items_count', 'is_complete',
            'created_at', 'updated_at',
        ]
        read_only_fields = ['created_at', 'updated_at', 'members', 'items_count', 'is_complete']

    def get_members(self, obj):
        try:
            members = obj.members.select_related('user').all()
        except Exception:
            members = []
        return [
            {
                'id': m.id,
                'role': m.role,
                'user': UserLightSerializer(m.user).data if m.user else None,
            }
            for m in members
        ]

    def get_items_count(self, obj):
        try:
            from decimal import Decimal
            items = obj.items.prefetch_related('quantities').all()
            count = 0
            for item in items:
                pantry_qty = Decimal(str(item.pantry_quantity or 0))
                total_qty = sum(Decimal(str(q.quantity or 0)) for q in item.quantities.all())
                total_checked = sum(Decimal(str(q.checked_quantity or 0)) for q in item.quantities.all())
                remaining = total_qty - total_checked - pantry_qty
                if remaining > 0:
                    count += 1
            return count
        except Exception:
            return 0

    def get_is_complete(self, obj):
        """Vérifie si tous les ingrédients sont cochés (remaining <= 0 pour tous les items)"""
        try:
            from decimal import Decimal
            items = obj.items.prefetch_related('quantities').all()
            if not items.exists():
                return False  # Liste vide = pas complète
            
            for item in items:
                pantry_qty = Decimal(str(item.pantry_quantity or 0))
                total_qty = sum(Decimal(str(q.quantity or 0)) for q in item.quantities.all())
                total_checked = sum(Decimal(str(q.checked_quantity or 0)) for q in item.quantities.all())
                remaining = total_qty - total_checked - pantry_qty
                if remaining > 0:
                    return False
            return True
        except Exception:
            return False

    def create(self, validated_data):
        """
        Crée la liste puis crée automatiquement le membre owner pour l'utilisateur courant.
        """
        request = self.context.get('request')
        if not request or request.user.is_anonymous:
            raise serializers.ValidationError("Authentication required")
        shopping_list = super().create(validated_data)
        from .models import ShoppingListMember
        ShoppingListMember.objects.create(
            shopping_list=shopping_list,
            user=request.user,
            role='owner',
        )
        return shopping_list


class ShoppingListInvitationSerializer(serializers.ModelSerializer):
    """Serializer pour les invitations aux listes de courses"""
    from accounts.serializers import UserSerializer
    inviter = UserSerializer(read_only=True)
    invitee = UserSerializer(read_only=True)
    shopping_list = serializers.SerializerMethodField()
    status_display = serializers.CharField(source='get_status_display', read_only=True)
    
    class Meta:
        model = ShoppingListInvitation
        fields = [
            'id',
            'inviter',
            'invitee',
            'shopping_list',
            'status',
            'status_display',
            'created_at',
            'updated_at',
        ]
        read_only_fields = ['created_at', 'updated_at']
    
    def get_shopping_list(self, obj):
        """Utiliser ShoppingListSerializer pour éviter la référence circulaire"""
        return ShoppingListSerializer(obj.shopping_list, context=self.context).data


class ShoppingListItemSerializer(serializers.ModelSerializer):
    """Serializer pour les items de liste de courses (V2)"""
    ingredient = IngredientSerializer(read_only=True)
    checked_by = UserLightSerializer(read_only=True)
    total_quantity = serializers.SerializerMethodField()
    total_checked_quantity = serializers.SerializerMethodField()
    unit = serializers.SerializerMethodField()
    remaining_quantity = serializers.SerializerMethodField()
    status = serializers.SerializerMethodField()
    status_display = serializers.SerializerMethodField()

    class Meta:
        model = ShoppingListItem
        fields = [
            'id',
            'shopping_list',
            'ingredient',
            'unit_group',
            'pantry_quantity',
            'pantry_unit',
            'checked_at',
            'checked_by',
            'total_quantity',
            'total_checked_quantity',
            'unit',
            'remaining_quantity',
            'status',
            'status_display',
            'created_at',
            'updated_at',
        ]
        read_only_fields = [
            'shopping_list',
            'ingredient',
            'unit_group',
            'checked_at',
            'checked_by',
            'total_quantity',
            'total_checked_quantity',
            'unit',
            'remaining_quantity',
            'status',
            'status_display',
            'created_at',
            'updated_at',
        ]

    def _sum_field(self, obj, field):
        try:
            qs = obj.quantities.all()
            return float(sum(getattr(q, field) or 0 for q in qs))
        except Exception:
            return 0.0

    def get_total_quantity(self, obj):
        return self._sum_field(obj, 'quantity')

    def get_total_checked_quantity(self, obj):
        return self._sum_field(obj, 'checked_quantity')

    def get_unit(self, obj):
        try:
            q = obj.quantities.first()
            if q and q.unit:
                return q.unit
        except Exception:
            pass
        return obj.pantry_unit or ''

    def get_remaining_quantity(self, obj):
        total = self.get_total_quantity(obj)
        checked = self.get_total_checked_quantity(obj)
        pantry = float(obj.pantry_quantity or 0)
        remaining = total - checked - pantry
        return float(remaining) if remaining > 0 else 0.0

    def get_status(self, obj):
        remaining = self.get_remaining_quantity(obj)
        if remaining <= 0:
            return 'purchased'
        if float(obj.pantry_quantity or 0) > 0:
            return 'in_pantry'
        return 'to_buy'

    def get_status_display(self, obj):
        mapping = {
            'to_buy': 'À acheter',
            'in_pantry': 'Dans les placards',
            'purchased': 'Acheté',
        }
        return mapping.get(self.get_status(obj), 'À acheter')


class ShoppingListItemCreateSerializer(serializers.Serializer):
    """Serializer pour créer un item de liste de courses manuellement"""
    shopping_list_id = serializers.IntegerField(required=True)
    ingredient_name = serializers.CharField(max_length=200, required=True, help_text="Nom de l'ingrédient (sera créé si n'existe pas)")
    quantity = serializers.DecimalField(max_digits=10, decimal_places=2, default=1.0, required=False)
    unit = serializers.CharField(max_length=20, default='piece', required=False)
    category_id = serializers.IntegerField(required=False, allow_null=True, help_text="ID de catégorie (optionnel, sera déterminé automatiquement si non fourni)")
    
    def validate_unit(self, value):
        """Valider que l'unité est valide"""
        valid_units = ['g', 'kg', 'ml', 'l', 'tsp', 'tbsp', 'cup', 'piece', 'pinch', 'clove']
        if value and value.lower() not in valid_units:
            raise serializers.ValidationError(f"Unité invalide. Unités valides: {', '.join(valid_units)}")
        return value.lower() if value else 'piece'


# Serializers pour Collections
class CollectionRecipeSerializer(serializers.ModelSerializer):
    """Serializer pour la relation Collection-Recipe"""
    recipe = RecipeLightSerializer(read_only=True)
    recipe_id = serializers.PrimaryKeyRelatedField(
        queryset=Recipe.objects.all(),
        source='recipe',
        write_only=True
    )
    added_by_username = serializers.CharField(source='added_by.username', read_only=True)
    
    class Meta:
        model = CollectionRecipe
        fields = ['id', 'recipe', 'recipe_id', 'added_by', 'added_by_username', 'added_at']
        read_only_fields = ['added_by', 'added_at']


class CollectionMemberSerializer(serializers.ModelSerializer):
    """Serializer pour les membres d'une collection"""
    user = UserLightSerializer(read_only=True)
    user_id = serializers.PrimaryKeyRelatedField(
        queryset=User.objects.all(),
        source='user',
        write_only=True
    )
    role_display = serializers.CharField(source='get_role_display', read_only=True)
    
    class Meta:
        model = CollectionMember
        fields = ['id', 'user', 'user_id', 'role', 'role_display', 'joined_at']
        read_only_fields = ['joined_at']


class CollectionSerializer(serializers.ModelSerializer):
    """Serializer pour afficher une collection avec ses recettes"""
    owner = UserLightSerializer(read_only=True)
    recipes_count = serializers.SerializerMethodField()
    cover_image_url = serializers.SerializerMethodField()
    is_following = serializers.SerializerMethodField()

    class Meta:
        model = Collection
        fields = [
            'id', 'name', 'description', 'owner', 'is_public', 'is_collaborative',
            'cover_image_path', 'cover_image_url', 'recipes_count', 'is_following',
            'created_at', 'updated_at'
        ]
        read_only_fields = ['owner', 'created_at', 'updated_at']

    def get_is_following(self, obj):
        request = self.context.get('request')
        if not request or not request.user.is_authenticated:
            return False
        return obj.followers.filter(user=request.user).exists()
    
    def get_recipes_count(self, obj):
        """Compter les recettes de manière optimisée"""
        try:
            if hasattr(obj, 'recipes_count'):
                # Si le count a été précalculé via annotate
                return obj.recipes_count
            # Sinon, utiliser la relation préchargée
            if hasattr(obj, '_prefetched_objects_cache') and 'collection_recipes' in obj._prefetched_objects_cache:
                return len(obj._prefetched_objects_cache['collection_recipes'])
            # Dernier recours : count direct
            return obj.collection_recipes.count()
        except Exception:
            return 0
    
    def get_cover_image_url(self, obj):
        """Construire l'URL complète de l'image de couverture"""
        try:
            if obj.cover_image_path:
                from django.conf import settings
                if hasattr(settings, 'build_s3_url'):
                    return settings.build_s3_url(obj.cover_image_path)
                # Fallback si build_s3_url n'est pas disponible
                from savr_back.settings import build_s3_url
                return build_s3_url(obj.cover_image_path)
        except Exception:
            pass
        return None


class CollectionListSerializer(serializers.ModelSerializer):
    """Serializer simplifié pour la liste des collections"""
    owner = UserLightSerializer(read_only=True)
    recipes_count = serializers.SerializerMethodField()
    cover_image_url = serializers.SerializerMethodField()
    collection_recipes = serializers.SerializerMethodField()
    last_activity_at = serializers.SerializerMethodField()
    is_owner = serializers.SerializerMethodField()
    is_following = serializers.SerializerMethodField()

    class Meta:
        model = Collection
        fields = [
            'id', 'name', 'description', 'owner', 'is_public', 'is_collaborative',
            'cover_image_path', 'cover_image_url', 'recipes_count', 'collection_recipes',
            'last_activity_at', 'is_owner', 'is_following', 'created_at', 'updated_at'
        ]
        read_only_fields = ['owner', 'created_at', 'updated_at']

    def get_is_owner(self, obj):
        request = self.context.get('request')
        if not request or not request.user.is_authenticated:
            return False
        return obj.owner_id == request.user.id

    def get_is_following(self, obj):
        request = self.context.get('request')
        if not request or not request.user.is_authenticated:
            return False
        return obj.followers.filter(user=request.user).exists()
    
    def get_recipes_count(self, obj):
        """Compter les recettes"""
        try:
            # Vérifier si l'annotation total_recipes existe (depuis annotate)
            if hasattr(obj, 'total_recipes'):
                return obj.total_recipes
            # Sinon utiliser la relation préchargée
            if hasattr(obj, '_prefetched_objects_cache') and 'collection_recipes' in obj._prefetched_objects_cache:
                return len(obj._prefetched_objects_cache['collection_recipes'])
            # Dernier recours : count direct
            return obj.collection_recipes.count()
        except Exception:
            return 0
    
    def get_collection_recipes(self, obj):
        """Récupérer les premières recettes avec leurs images pour le collage"""
        try:
            # Récupérer les 4 premières recettes
            collection_recipes = obj.collection_recipes.all()[:4]
            return [
                {
                    'id': cr.id,
                    'recipe': {
                        'id': cr.recipe.id,
                        'title': cr.recipe.title,
                        'image_url': cr.recipe.image_url,
                    } if cr.recipe else None,
                }
                for cr in collection_recipes
            ]
        except Exception:
            return []
    
    def get_cover_image_url(self, obj):
        """Construire l'URL complète de l'image de couverture"""
        try:
            if obj.cover_image_path:
                from django.conf import settings
                if hasattr(settings, 'build_s3_url'):
                    return settings.build_s3_url(obj.cover_image_path)
                from savr_back.settings import build_s3_url
                return build_s3_url(obj.cover_image_path)
        except Exception:
            pass
        return None

    def get_last_activity_at(self, obj):
        try:
            if hasattr(obj, 'last_activity') and obj.last_activity:
                return obj.last_activity
            return obj.collection_recipes.values_list('added_at', flat=True).first()
        except Exception:
            return None


class CollectionCreateSerializer(serializers.ModelSerializer):
    """Serializer pour créer une collection"""
    
    class Meta:
        model = Collection
        fields = ['id', 'name', 'description', 'is_public', 'is_collaborative', 'cover_image_path']
        read_only_fields = ['id']
    
    def create(self, validated_data):
        """Créer une collection avec l'utilisateur connecté comme owner"""
        user = self.context['request'].user
        # Retirer owner de validated_data s'il est présent (pour éviter le conflit)
        validated_data.pop('owner', None)
        collection = Collection.objects.create(owner=user, **validated_data)
        # Créer automatiquement un CollectionMember pour le owner
        CollectionMember.objects.create(
            collection=collection,
            user=user,
            role='owner'
        )
        return collection


class CollectionUpdateSerializer(serializers.ModelSerializer):
    """Serializer pour mettre à jour une collection"""
    
    class Meta:
        model = Collection
        fields = ['name', 'description', 'is_public', 'is_collaborative', 'cover_image_path']


class RecipeFormalizeSerializer(serializers.Serializer):
    """Serializer pour recevoir les données brutes du formulaire de création de recette"""
    title = serializers.CharField(
        max_length=200, 
        required=True,
        help_text="Titre de la recette (max 200 caractères)"
    )
    description = serializers.CharField(
        required=False, 
        allow_blank=True,
        max_length=2000,
        help_text="Description optionnelle (max 2000 caractères)"
    )
    ingredients_text = serializers.CharField(
        required=True, 
        max_length=5000,
        help_text="Ingrédients séparés par sauts de ligne (max 5000 caractères)"
    )
    instructions_text = serializers.CharField(
        required=True,
        max_length=10000,
        help_text="Instructions séparées par sauts de ligne (max 10000 caractères)"
    )
    servings = serializers.IntegerField(
        required=False, 
        min_value=1, 
        max_value=50,
        allow_null=True,
        help_text="Nombre de portions (1-50)"
    )
    prep_time = serializers.IntegerField(
        required=False, 
        min_value=0, 
        max_value=1440,
        allow_null=True, 
        help_text="Temps de préparation en minutes (max 24h)"
    )
    cook_time = serializers.IntegerField(
        required=False, 
        min_value=0,
        max_value=1440,
        allow_null=True, 
        help_text="Temps de cuisson en minutes (max 24h)"
    )
    image_path = serializers.CharField(
        required=False,
        allow_blank=True,
        allow_null=True,
        max_length=500,
        help_text="Chemin relatif de l'image (fourni après upload S3)"
    )
    categories = serializers.ListField(
        child=serializers.CharField(max_length=100),
        required=False,
        allow_empty=True,
        max_length=10,
        help_text="Liste des catégories (max 10)"
    )
    
    def validate_title(self, value):
        """Valider le titre"""
        if not value or not value.strip():
            raise serializers.ValidationError("Le titre ne peut pas être vide.")
        if len(value.strip()) < 3:
            raise serializers.ValidationError("Le titre doit contenir au moins 3 caractères.")
        return value.strip()
    
    def validate_ingredients_text(self, value):
        """Valider le texte des ingrédients"""
        if not value or not value.strip():
            raise serializers.ValidationError("Les ingrédients sont requis.")
        # Vérifier qu'il y a au moins un ingrédient (au moins une ligne non vide)
        lines = [line.strip() for line in value.split('\n') if line.strip()]
        if len(lines) < 1:
            raise serializers.ValidationError("Veuillez saisir au moins un ingrédient.")
        if len(lines) > 100:
            raise serializers.ValidationError("Maximum 100 ingrédients autorisés.")
        return value
    
    def validate_instructions_text(self, value):
        """Valider le texte des instructions"""
        if not value or not value.strip():
            raise serializers.ValidationError("Les instructions sont requises.")
        # Vérifier qu'il y a au moins une étape
        lines = [line.strip() for line in value.split('\n') if line.strip()]
        if len(lines) < 1:
            raise serializers.ValidationError("Veuillez saisir au moins une étape.")
        if len(lines) > 50:
            raise serializers.ValidationError("Maximum 50 étapes autorisées.")
        return value


# Serializers for legacy meal plan groups removed (schema simplification)


class RecipeGenerateFromIdeaSerializer(serializers.Serializer):
    idea_text = serializers.CharField(max_length=2000)
    servings = serializers.IntegerField(required=False, allow_null=True, min_value=1, max_value=20)

    def validate_idea_text(self, value):
        cleaned = (value or '').strip()
        if len(cleaned) < 5:
            raise serializers.ValidationError(
                "Décrivez votre idée en au moins quelques mots (5 caractères minimum)."
            )
        return cleaned


class RecipeImportRequestSerializer(serializers.ModelSerializer):
    recipe = RecipeSerializer(read_only=True)
    recipe_id = serializers.SerializerMethodField()
    import_progress = serializers.SerializerMethodField()
    import_extractor = serializers.SerializerMethodField()
    url = serializers.SerializerMethodField()
    idea_text = serializers.SerializerMethodField()
    job_type = serializers.SerializerMethodField()

    class Meta:
        model = RecipeImportRequest
        fields = [
            'id',
            'status',
            'recipe',
            'recipe_id',
            'url',
            'idea_text',
            'job_type',
            'import_extractor',
            'import_progress',
            'error_message',
            'created_at',
            'updated_at',
        ]

    def get_recipe_id(self, obj):
        return obj.recipe_id

    def get_import_progress(self, obj):
        payload = obj.payload or {}
        return payload.get('import_progress')

    def get_import_extractor(self, obj):
        payload = obj.payload or {}
        return payload.get('import_extractor')

    def get_url(self, obj):
        payload = obj.payload or {}
        return payload.get('url') or payload.get('import_source_url')

    def get_idea_text(self, obj):
        payload = obj.payload or {}
        return payload.get('idea_text') or ''

    def get_job_type(self, obj):
        payload = obj.payload or {}
        if payload.get('job_type'):
            return payload['job_type']
        if payload.get('idea_text'):
            return 'generate'
        return 'import'


class RecipeImportRequestLightSerializer(serializers.ModelSerializer):
    """
    Version allégée pour le polling (bulle + écran imports).
    Ne retourne pas la recette complète pour limiter la taille du payload.
    """
    recipe_id = serializers.SerializerMethodField()
    recipe_title = serializers.SerializerMethodField()
    import_progress = serializers.SerializerMethodField()
    import_extractor = serializers.SerializerMethodField()
    url = serializers.SerializerMethodField()
    idea_text = serializers.SerializerMethodField()
    job_type = serializers.SerializerMethodField()

    class Meta:
        model = RecipeImportRequest
        fields = [
            'id',
            'status',
            'recipe_id',
            'recipe_title',
            'url',
            'idea_text',
            'job_type',
            'import_extractor',
            'import_progress',
            'error_message',
            'created_at',
            'updated_at',
        ]

    def get_recipe_id(self, obj):
        return obj.recipe_id

    def get_recipe_title(self, obj):
        return getattr(obj.recipe, 'title', None)

    def get_import_progress(self, obj):
        payload = obj.payload or {}
        return payload.get('import_progress')

    def get_import_extractor(self, obj):
        payload = obj.payload or {}
        return payload.get('import_extractor')

    def get_url(self, obj):
        payload = obj.payload or {}
        return payload.get('url') or payload.get('import_source_url')

    def get_idea_text(self, obj):
        payload = obj.payload or {}
        return payload.get('idea_text') or ''

    def get_job_type(self, obj):
        payload = obj.payload or {}
        if payload.get('job_type'):
            return payload['job_type']
        if payload.get('idea_text'):
            return 'generate'
        return 'import'
