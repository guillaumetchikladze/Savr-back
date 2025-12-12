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
    ShoppingList,
    ShoppingListItem,
    Collection,
    CollectionRecipe,
    CollectionMember,
    RecipeImportRequest,
    RecipeBatch,
)
from django.contrib.auth import get_user_model
from django.db.models import Q
from .utils import get_accessible_meal_plan_filter
User = get_user_model()

class UserLightSerializer(serializers.ModelSerializer):
    class Meta:
        model = User
        fields = ['id', 'username', 'avatar_url']


class CategorySerializer(serializers.ModelSerializer):
    class Meta:
        model = Category
        fields = ['id', 'name', 'display_order']


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
    image_url = serializers.SerializerMethodField()
    # Ne pas inclure steps et recipe_ingredients ici - chargés via endpoints séparés
    
    class Meta:
        model = Recipe
        fields = [
            'id', 'title', 'description', 'steps_summary', 'meal_type', 'meal_type_display',
            'difficulty', 'difficulty_display', 'prep_time', 'cook_time',
            'servings', 'image_path', 'image_url', 'created_by', 'created_by_username',
            'is_public', 'source_type', 'source_type_display', 'import_source_url',
            'created_at', 'updated_at', 'is_favorited'
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


class RecipeSerializer(serializers.ModelSerializer):
    steps = StepSerializer(many=True, read_only=True)
    recipe_ingredients = RecipeIngredientSerializer(many=True, read_only=True)
    created_by_username = serializers.CharField(source='created_by.username', read_only=True)
    meal_type_display = serializers.CharField(source='get_meal_type_display', read_only=True)
    difficulty_display = serializers.CharField(source='get_difficulty_display', read_only=True)
    source_type_display = serializers.CharField(source='get_source_type_display', read_only=True)
    is_favorited = serializers.SerializerMethodField()
    image_url = serializers.SerializerMethodField()
    
    class Meta:
        model = Recipe
        fields = [
            'id', 'title', 'description', 'steps_summary', 'meal_type', 'meal_type_display',
            'difficulty', 'difficulty_display', 'prep_time', 'cook_time',
            'servings', 'image_path', 'image_url', 'created_by', 'created_by_username',
            'is_public', 'source_type', 'source_type_display', 'import_source_url',
            'created_at', 'updated_at', 'steps', 'recipe_ingredients', 'is_favorited'
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
    image_url = serializers.SerializerMethodField()
    
    class Meta:
        model = Recipe
        fields = ['id', 'title', 'image_path', 'image_url', 'meal_type', 'meal_type_display', 'difficulty', 'difficulty_display', 'prep_time', 'cook_time', 'servings']
    
    def get_image_url(self, obj):
        return obj.image_url


class RecipeBatchLightSerializer(serializers.ModelSerializer):
    recipe = RecipeLightSerializer(read_only=True)
    total_servings_batch = serializers.IntegerField(read_only=True)
    groupedDates = serializers.ListField(child=serializers.CharField(), read_only=True)
    meal_plan_ids = serializers.ListField(child=serializers.IntegerField(), read_only=True)
    meals = serializers.ListField(child=serializers.DictField(), read_only=True)
    steps = StepSerializer(many=True, read_only=True)
    is_cooked = serializers.BooleanField(read_only=True)
    
    class Meta:
        model = RecipeBatch
        fields = [
            'id', 'name', 'recipe',
            'total_servings_batch', 'groupedDates',
            'meal_plan_ids', 'meals', 'is_cooked',
            'steps',
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
        fields = ['id', 'name', 'notes', 'recipe', 'recipe_id', 'created_at', 'updated_at']
        read_only_fields = ['id', 'created_at', 'updated_at']


class MealPlanRecipeSerializer(serializers.ModelSerializer):
    """
    Serializer pour la relation MealPlan-RecipeBatch avec ratio.
    Conserve le nom pour compat compat, mais utilise MealPlanRecipeBatch.
    """
    recipe = RecipeLightSerializer(source='recipe_batch.recipe', read_only=True)
    recipe_batch = RecipeBatchSerializer(read_only=True)
    recipe_batch_id = serializers.PrimaryKeyRelatedField(
        queryset=RecipeBatch.objects.all(),
        source='recipe_batch',
        write_only=True,
        required=False,
        allow_null=True
    )
    ratio = serializers.DecimalField(max_digits=5, decimal_places=2)
    groupedDates = serializers.SerializerMethodField()
    group_id = serializers.SerializerMethodField()
    
    class Meta:
        model = MealPlanRecipeBatch
        fields = [
            'id',
            'recipe',
            'recipe_batch',
            'recipe_batch_id',
            'ratio',
            'order',
            'group_id',
            'groupedDates',
            'created_at',
            'updated_at'
        ]
        read_only_fields = ['id', 'created_at', 'updated_at', 'recipe', 'recipe_batch']
    
    def get_group_id(self, obj):
        # Utiliser l'id du batch comme identifiant de groupe
        return obj.recipe_batch_id if obj.recipe_batch_id else None
    
    def get_groupedDates(self, obj):
        """Dates de tous les meal plans liés au même batch."""
        if not obj.recipe_batch_id:
            return [obj.meal_plan.date.isoformat()]
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
    
    class Meta:
        model = MealPlan
        fields = [
            'id', 'date', 'meal_time', 'meal_time_display',
            'meal_type', 'meal_type_display', 'recipe', 'recipes',
            'user', 'participants', 'confirmed', 'guest_count', 
            'total_guest_count', 'total_participants', 'total_servings',
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
        
        participants_to_use = None
        if hasattr(obj, '_total_participants'):
            participants_to_use = obj._total_participants
        else:
            participants_to_use = self.get_participants(obj)
        
        active_participants_count = sum(
            1 for p in participants_to_use
            if isinstance(p, dict) and p.get('status') in ['accepted', 'pending']
        )
        
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
    recipe_ratios = serializers.DictField(
        child=serializers.FloatField(),
        write_only=True,
        required=False,
        help_text="(Compat) Dictionnaire {recipe_id: ratio} pour personnaliser les ratios"
    )
    meal_time_display = serializers.CharField(source='get_meal_time_display', read_only=True)
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
        help_text="Liste unifiée {recipe_id, batch_id, ratio, order}"
    )
    
    class Meta:
        model = MealPlan
        fields = [
            'id', 'date', 'meal_time', 'meal_time_display',
            'meal_type', 'meal_type_display', 'recipe', 'recipe_id',
            'recipes', 'batch_ids', 'recipe_ids', 'recipe_ratios',
            'entries', 'recipes_entries',
            'user', 'participants', 'confirmed', 'guest_count', 
            'total_guest_count', 'total_participants', 'total_servings',
            'created_at', 'updated_at'
        ]
        read_only_fields = ['user', 'participants', 'created_at', 'updated_at', 'recipes', 'recipe']
    
    def validate(self, attrs):
        # Si update partiel avec entries/recipe_ids/batch_ids, ne pas exiger date/meal_time/meal_type
        if self.instance and ('recipe_ids' in attrs or 'batch_ids' in attrs or 'entries' in attrs):
            return attrs
        return attrs
    
    def create(self, validated_data):
        from decimal import Decimal, ROUND_HALF_UP
        
        validated_data['user'] = self.context['request'].user
        
        # Extraire les données pour les recettes / batches
        entries = validated_data.pop('entries', None)
        batch_ids = validated_data.pop('batch_ids', None)
        recipe_ids = validated_data.pop('recipe_ids', None)
        recipe_ratios = validated_data.pop('recipe_ratios', {})
        
        # Créer le meal plan
        meal_plan = super().create(validated_data)
        
        # Nouveau payload unifié
        if entries:
            from decimal import Decimal, ROUND_HALF_UP
            for order, item in enumerate(entries):
                recipe_id = item.get('recipe_id')
                batch_id = item.get('batch_id')
                ratio_value = item.get('ratio', 1.0)
                order_value = item.get('order', order)
                ratio_decimal = Decimal(str(ratio_value)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
                if batch_id:
                    MealPlanRecipeBatch.objects.create(
                        meal_plan=meal_plan,
                        recipe_batch_id=batch_id,
                        ratio=ratio_decimal,
                        order=order_value
                    )
                elif recipe_id:
                    batch = RecipeBatch.objects.create(recipe_id=recipe_id, created_by=meal_plan.user)
                    MealPlanRecipeBatch.objects.create(
                        meal_plan=meal_plan,
                        recipe_batch=batch,
                        ratio=ratio_decimal,
                        order=order_value
                    )
            return meal_plan
        
        # Si batch_ids est fourni, on associe uniquement ces batches
        if batch_ids:
            for order, batch_id in enumerate(batch_ids):
                MealPlanRecipeBatch.objects.create(
                    meal_plan=meal_plan,
                    recipe_batch_id=batch_id,
                    ratio=Decimal('1.00'),
                    order=order
                )
            return meal_plan
        
        # Sinon, compat : créer des batches à la volée depuis des recettes
        if recipe_ids:
            default_ratio = Decimal('1.0') / Decimal(str(len(recipe_ids)))
            for order, recipe_id in enumerate(recipe_ids):
                ratio_value = recipe_ratios.get(str(recipe_id), recipe_ratios.get(recipe_id, default_ratio))
                ratio_decimal = Decimal(str(ratio_value)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
                batch = RecipeBatch.objects.create(recipe_id=recipe_id, created_by=meal_plan.user)
                MealPlanRecipeBatch.objects.create(
                    meal_plan=meal_plan,
                    recipe_batch=batch,
                    ratio=ratio_decimal,
                    order=order
                )
        
        return meal_plan
    
    def update(self, instance, validated_data):
        from decimal import Decimal, ROUND_HALF_UP
        
        # Extraire les données pour les recettes
        entries = validated_data.pop('entries', None)
        batch_ids = validated_data.pop('batch_ids', None)
        recipe_ids = validated_data.pop('recipe_ids', None)
        recipe_ratios = validated_data.pop('recipe_ratios', {})
        
        # Mettre à jour le meal plan (seulement si d'autres champs sont fournis)
        meal_plan = super().update(instance, validated_data)
        
        # Payload unifié : remplace l'ensemble
        if entries is not None:
            from decimal import Decimal, ROUND_HALF_UP
            meal_plan.meal_plan_recipe_batches.all().delete()
            for order, item in enumerate(entries):
                recipe_id = item.get('recipe_id')
                batch_id = item.get('batch_id')
                ratio_value = item.get('ratio', 1.0)
                order_value = item.get('order', order)
                ratio_decimal = Decimal(str(ratio_value)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
                if batch_id:
                    MealPlanRecipeBatch.objects.create(
                        meal_plan=meal_plan,
                        recipe_batch_id=batch_id,
                        ratio=ratio_decimal,
                        order=order_value
                    )
                elif recipe_id:
                    batch = RecipeBatch.objects.create(recipe_id=recipe_id, created_by=meal_plan.user)
                    MealPlanRecipeBatch.objects.create(
                        meal_plan=meal_plan,
                        recipe_batch=batch,
                        ratio=ratio_decimal,
                        order=order_value
                    )
            return meal_plan
        
        # Si batch_ids est fourni explicitement, on remplace les liens par ces batches
        if batch_ids is not None:
            meal_plan.meal_plan_recipe_batches.all().delete()
            if len(batch_ids) > 0:
                for order, batch_id in enumerate(batch_ids):
                    MealPlanRecipeBatch.objects.create(
                        meal_plan=meal_plan,
                        recipe_batch_id=batch_id,
                        ratio=Decimal('1.00'),
                        order=order
                    )
            return meal_plan
        
        # Sinon, compat: handle recipe_ids en créant des batches
        if recipe_ids is not None:
            meal_plan.meal_plan_recipe_batches.all().delete()
            default_ratio = Decimal('1.0') / Decimal(str(len(recipe_ids))) if recipe_ids else Decimal('1.0')
            for order, recipe_id in enumerate(recipe_ids):
                ratio_value = recipe_ratios.get(str(recipe_id)) or recipe_ratios.get(recipe_id) or default_ratio
                ratio_decimal = Decimal(str(ratio_value)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
                batch = RecipeBatch.objects.create(recipe_id=recipe_id, created_by=meal_plan.user)
                MealPlanRecipeBatch.objects.create(
                    meal_plan=meal_plan,
                    recipe_batch=batch,
                    ratio=ratio_decimal,
                    order=order
                )
        
        return meal_plan

    def get_recipes_entries(self, obj: MealPlan):
        """Retourne une liste unifiée {recipe_id, batch_id, ratio, order} pour sérialisation."""
        entries = []
        for mprb in obj.meal_plan_recipe_batches.all().order_by('order', 'id'):
            entries.append({
                'recipe_id': mprb.recipe_batch.recipe_id if mprb.recipe_batch else None,
                'batch_id': mprb.recipe_batch_id,
                'ratio': float(mprb.ratio),
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
        Calcule les servings pour un batch en sommant les convives
        de tous les meal plans liés au même batch.
        """
        batch_id = meal_plan_recipe_batch.recipe_batch_id
        if not batch_id:
            meal_plan = meal_plan_recipe_batch.meal_plan
            participants = meal_plan.invitations.filter(status__in=['accepted', 'pending']).count()
            return 1 + participants + (meal_plan.guest_count or 0)
        
        total_servings = 0
        seen_meal_plans = set()
        for mp in MealPlan.objects.filter(meal_plan_recipe_batches__recipe_batch_id=batch_id).distinct():
            if mp.id in seen_meal_plans:
                continue
            seen_meal_plans.add(mp.id)
            participants = mp.invitations.filter(status__in=['accepted', 'pending']).count()
            total_servings += 1 + participants + (mp.guest_count or 0)
        return total_servings
    
    def get_total_servings(self, obj: MealPlan):
        """
        Calcule le nombre total de personnes pour ce meal plan.
        Pour un meal plan avec plusieurs recettes : somme des servings de chaque recette
        (groupée ou non).
        """
        # Si on a un total_servings pré-calculé, l'utiliser
        if hasattr(obj, '_total_servings'):
            return obj._total_servings
        
        # Calculer en sommant les servings de chaque recette
        meal_plan_recipes = obj.meal_plan_recipe_batches.all()
        if not meal_plan_recipes.exists():
            # Pas de recettes : calculer pour le meal plan seul
            participants = obj.invitations.filter(status__in=['accepted', 'pending']).count()
            return 1 + participants + (obj.guest_count or 0)
        
        # Pour chaque recette, calculer ses servings (groupée ou non)
        total_servings = 0
        for meal_plan_recipe in meal_plan_recipes:
            recipe_servings = self._calculate_recipe_group_servings(meal_plan_recipe)
            total_servings += recipe_servings
        
        return total_servings
    
class MealPlanListSerializer(serializers.ModelSerializer):
    user = UserLightSerializer(read_only=True)
    recipe = RecipeLightSerializer(read_only=True)  # Garder pour compatibilité
    recipes = MealPlanRecipeSerializer(source='meal_plan_recipe_batches', many=True, read_only=True)
    meal_time_display = serializers.CharField(source='get_meal_time_display', read_only=True)
    meal_type_display = serializers.CharField(source='get_meal_type_display', read_only=True)
    groupedDates = serializers.SerializerMethodField()
    
    class Meta:
        model = MealPlan
        fields = [
            'id', 'date', 'meal_time', 'meal_time_display',
            'meal_type', 'meal_type_display', 'confirmed',
            'recipe', 'user', 'recipes', 'groupedDates',
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
    recipe = RecipeLightSerializer(read_only=True)  # Garder pour compatibilité
    recipes = MealPlanRecipeSerializer(source='meal_plan_recipe_batches', many=True, read_only=True)
    meal_time_display = serializers.CharField(source='get_meal_time_display', read_only=True)
    meal_type_display = serializers.CharField(source='get_meal_type_display', read_only=True)
    total_guest_count = serializers.SerializerMethodField()
    total_participants = serializers.SerializerMethodField()
    total_servings = serializers.SerializerMethodField()
    groupedDates = serializers.SerializerMethodField()
    
    class Meta:
        model = MealPlan
        fields = [
            'id', 'date', 'meal_time', 'meal_time_display',
            'meal_type', 'meal_type_display', 'confirmed',
            'recipe', 'recipes', 'total_guest_count', 'total_participants', 'total_servings',
            'groupedDates',
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
        dates = set()
        for mprb in obj.meal_plan_recipe_batches.all():
            if mprb.recipe_batch_id:
                for mp in MealPlan.objects.filter(meal_plan_recipe_batches__recipe_batch_id=mprb.recipe_batch_id):
                    dates.add(mp.date.isoformat())
            else:
                dates.add(obj.date.isoformat())
        return sorted(list(dates)) if dates else [obj.date.isoformat()]


class MealPlanMinimalListSerializer(serializers.ModelSerializer):
    """
    Serializer ultra-léger pour le mode minimal :
    - Seulement les champs essentiels (id, date, meal_time, meal_type)
    - PAS de recipe ni recipes (payload léger pour le calendrier)
    - Pas de groupedDates, total_servings, total_participants, total_guest_count
    - Pas de calculs coûteux sur les groupes
    """
    meal_time_display = serializers.CharField(source='get_meal_time_display', read_only=True)
    meal_type_display = serializers.CharField(source='get_meal_type_display', read_only=True)
    
    class Meta:
        model = MealPlan
        fields = [
            'id', 'date', 'meal_time', 'meal_time_display',
            'meal_type', 'meal_type_display', 'confirmed',
        ]


class MealPlanByDateSerializer(serializers.ModelSerializer):
    """
    Detailed list for by_date: include host and participants with status.
    """
    host = UserLightSerializer(source='user', read_only=True)
    recipe = RecipeLightSerializer(read_only=True)  # Garder pour compatibilité
    recipes = MealPlanRecipeSerializer(source='meal_plan_recipe_batches', many=True, read_only=True)
    meal_time_display = serializers.CharField(source='get_meal_time_display', read_only=True)
    meal_type_display = serializers.CharField(source='get_meal_type_display', read_only=True)
    participants = serializers.SerializerMethodField()
    total_guest_count = serializers.SerializerMethodField()
    total_participants = serializers.SerializerMethodField()
    total_servings = serializers.SerializerMethodField()
    groupedDates = serializers.SerializerMethodField()
    
    class Meta:
        model = MealPlan
        fields = [
            'id', 'date', 'meal_time', 'meal_time_display',
            'meal_type', 'meal_type_display', 'confirmed',
            'recipe', 'recipes', 'host', 'participants', 'guest_count', 
            'total_guest_count', 'total_participants', 'total_servings',
            'groupedDates',
        ]
    
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
    
    class Meta:
        model = Timer
        fields = [
            'id', 'user', 'cooking_progress', 'step', 'step_title', 'step_order',
            'recipe_batch', 'recipe_title', 'duration_minutes', 'remaining_seconds',
            'started_at', 'expires_at', 'is_completed', 'created_at', 'updated_at'
        ]
        read_only_fields = ['user', 'started_at', 'expires_at', 'created_at', 'updated_at']


class TimerCreateSerializer(serializers.ModelSerializer):
    """Serializer pour créer un minuteur"""
    
    class Meta:
        model = Timer
        fields = [
            'cooking_progress', 'step', 'recipe_batch', 'duration_minutes', 'remaining_seconds'
        ]
    
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
    recipe_batch_id = serializers.IntegerField(source='recipe_batch.id', read_only=True)
    post_id = serializers.IntegerField(source='post.id', read_only=True)
    image_url = serializers.SerializerMethodField()
    presigned_url = serializers.SerializerMethodField()
    
    class Meta:
        model = PostPhoto
        fields = [
            'id', 'photo_type', 'photo_type_display', 'image_path', 'image_url', 'presigned_url',
            'step', 'step_order', 'step_title', 'captured_label',
            'time_display', 'recipe_batch_id', 'post_id', 'editable', 'order', 'created_at'
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
        
        # Vérifier l'accès via recipe_batch (via meal_plan_recipe_batches)
        # (propriétaire ou invité accepté)
        if obj.recipe_batch_id:
            from .models import RecipeBatch, MealPlan
            accessible_meal_plan_filter = get_accessible_meal_plan_filter(request.user)
            has_access = RecipeBatch.objects.filter(
                id=obj.recipe_batch_id,
                meal_plan_recipe_batches__meal_plan__in=MealPlan.objects.filter(
                    accessible_meal_plan_filter
                )
            ).exists()
            if has_access:
                return True
        
        # Vérifier l'accès via post
        if obj.post_id and obj.post:
            return obj.post.user == request.user
        
        return False


class PostSerializer(serializers.ModelSerializer):
    photos = PostPhotoSerializer(many=True, read_only=True)
    user = UserLightSerializer(read_only=True)
    recipe_batch = RecipeBatchLightSerializer(read_only=True)
    photos_count = serializers.IntegerField(read_only=True)
    has_all_photos = serializers.BooleanField(read_only=True)
    recipe_meta = serializers.SerializerMethodField()
    cookies_count = serializers.SerializerMethodField()
    has_cookie_from_user = serializers.SerializerMethodField()
    
    class Meta:
        model = Post
        fields = [
            'id', 'user', 'recipe_batch',
            'comment', 'is_published', 'recipe_meta',
            'photos', 'photos_count', 'has_all_photos',
            'cookies_count', 'has_cookie_from_user',
            'created_at', 'updated_at'
        ]
        read_only_fields = ['user', 'created_at', 'updated_at']
    
    def get_recipe_meta(self, obj):
        recipe = obj.recipe_batch.recipe if obj.recipe_batch else None
        if not recipe:
            return None
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


class PostPhotoListSerializer(serializers.ModelSerializer):
    """Version allégée pour la liste de posts (uniquement les URLs)."""
    image_url = serializers.SerializerMethodField()
    presigned_url = serializers.SerializerMethodField()
    
    class Meta:
        model = PostPhoto
        fields = ['id', 'photo_type', 'image_url', 'presigned_url', 'order']
    
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


class PostListSerializer(serializers.ModelSerializer):
    """Serializer minimal pour la liste des posts (feed)."""
    user = UserLightSerializer(read_only=True)
    photos = PostPhotoListSerializer(many=True, read_only=True)
    recipe = serializers.SerializerMethodField()
    cookies_count = serializers.SerializerMethodField()
    has_cookie_from_user = serializers.SerializerMethodField()
    
    class Meta:
        model = Post
        fields = [
            'id', 'user',
            'comment', 'is_published',
            'photos',
            'cookies_count', 'has_cookie_from_user',
            'recipe',
            'created_at',
        ]
        read_only_fields = ['user', 'created_at']
    
    def get_recipe(self, obj):
        recipe = obj.recipe_batch.recipe if obj.recipe_batch else None
        if not recipe:
            return None
        return {
            'id': recipe.id,
            'title': recipe.title,
            'image_url': getattr(recipe, 'image_url', None),
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


class ShoppingListMealPlanSerializer(serializers.ModelSerializer):
    """Serializer léger pour les meal plans dans une shopping list"""
    recipe = RecipeLightSerializer(read_only=True)
    meal_time_display = serializers.CharField(source='get_meal_time_display', read_only=True)
    
    class Meta:
        model = MealPlan
        fields = ['id', 'date', 'meal_time', 'meal_time_display', 'recipe']


class ShoppingListSerializer(serializers.ModelSerializer):
    """Serializer pour une liste de courses"""
    recipe_batches = RecipeBatchLightSerializer(many=True, read_only=True)
    recipe_batch_ids = serializers.PrimaryKeyRelatedField(
        queryset=RecipeBatch.objects.all(),
        source='recipe_batches',
        many=True,
        write_only=True,
        required=False
    )
    items_count = serializers.SerializerMethodField()
    
    class Meta:
        model = ShoppingList
        fields = [
            'id', 'name', 'recipe_batches', 'recipe_batch_ids', 'is_active', 'is_archived',
            'items_count', 'created_at', 'updated_at'
        ]
        read_only_fields = ['user', 'created_at', 'updated_at']
    
    def get_items_count(self, obj):
        return obj.items.count()
    
    def create(self, validated_data):
        validated_data['user'] = self.context['request'].user
        # Désactiver les autres listes actives de l'utilisateur
        ShoppingList.objects.filter(
            user=validated_data['user'],
            is_active=True
        ).update(is_active=False)
        return super().create(validated_data)


class ShoppingListItemSerializer(serializers.ModelSerializer):
    """Serializer pour les items de liste de courses"""
    ingredient = IngredientSerializer(read_only=True)
    ingredient_id = serializers.PrimaryKeyRelatedField(
        queryset=Ingredient.objects.all(),
        source='ingredient',
        write_only=True
    )
    status_display = serializers.CharField(source='get_status_display', read_only=True)
    
    class Meta:
        model = ShoppingListItem
        fields = [
            'id', 'ingredient', 'ingredient_id', 'shopping_list',
            'status', 'status_display', 'pantry_quantity', 'pantry_unit',
            'created_at', 'updated_at'
        ]
        read_only_fields = ['shopping_list', 'created_at', 'updated_at']


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
    
    class Meta:
        model = Collection
        fields = [
            'id', 'name', 'description', 'owner', 'is_public', 'is_collaborative',
            'cover_image_path', 'cover_image_url', 'recipes_count',
            'created_at', 'updated_at'
        ]
        read_only_fields = ['owner', 'created_at', 'updated_at']
    
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
    
    class Meta:
        model = Collection
        fields = [
            'id', 'name', 'description', 'owner', 'is_public', 'is_collaborative',
            'cover_image_path', 'cover_image_url', 'recipes_count', 'collection_recipes',
            'last_activity_at', 'created_at', 'updated_at'
        ]
        read_only_fields = ['owner', 'created_at', 'updated_at']
    
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


class RecipeImportRequestSerializer(serializers.ModelSerializer):
    recipe = RecipeSerializer(read_only=True)

    class Meta:
        model = RecipeImportRequest
        fields = ['id', 'status', 'recipe', 'error_message', 'created_at', 'updated_at']
