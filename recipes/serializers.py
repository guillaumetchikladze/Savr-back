from rest_framework import serializers
from .models import (
    Category,
    Recipe,
    Step,
    Ingredient,
    RecipeIngredient,
    StepIngredient,
    MealPlan,
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
)
from django.contrib.auth import get_user_model
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
        
        # Créer les étapes
        for step_data in steps_data:
            Step.objects.create(recipe=recipe, **step_data)
        
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


class MealPlanSerializer(serializers.ModelSerializer):
    recipe = RecipeSerializer(read_only=True)
    recipe_id = serializers.PrimaryKeyRelatedField(
        queryset=Recipe.objects.all(),
        source='recipe',
        write_only=True,
        required=False,
        allow_null=True
    )
    meal_type_display = serializers.CharField(source='get_meal_type_display', read_only=True)
    meal_time_display = serializers.CharField(source='get_meal_time_display', read_only=True)
    user = UserLightSerializer(read_only=True)
    participants = serializers.SerializerMethodField()
    
    class Meta:
        model = MealPlan
        fields = [
            'id', 'date', 'meal_time', 'meal_time_display',
            'meal_type', 'meal_type_display', 'recipe', 'recipe_id',
            'user', 'participants', 'confirmed', 'is_cooked',
            'created_at', 'updated_at'
        ]
        read_only_fields = ['user', 'participants', 'is_cooked', 'created_at', 'updated_at']
    
    def create(self, validated_data):
        validated_data['user'] = self.context['request'].user
        return super().create(validated_data)
    
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


class MealPlanListSerializer(serializers.ModelSerializer):
    user = UserLightSerializer(read_only=True)
    recipe = RecipeLightSerializer(read_only=True)
    meal_time_display = serializers.CharField(source='get_meal_time_display', read_only=True)
    meal_type_display = serializers.CharField(source='get_meal_type_display', read_only=True)
    
    class Meta:
        model = MealPlan
        fields = [
            'id', 'date', 'meal_time', 'meal_time_display',
            'meal_type', 'meal_type_display', 'confirmed', 'is_cooked',
            'recipe', 'user',
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
    recipe = RecipeLightSerializer(read_only=True)
    meal_time_display = serializers.CharField(source='get_meal_time_display', read_only=True)
    meal_type_display = serializers.CharField(source='get_meal_type_display', read_only=True)
    
    class Meta:
        model = MealPlan
        fields = [
            'id', 'date', 'meal_time', 'meal_time_display',
            'meal_type', 'meal_type_display', 'confirmed', 'is_cooked',
            'recipe',
        ]

class MealPlanByDateSerializer(serializers.ModelSerializer):
    """
    Detailed list for by_date: include host and participants with status.
    """
    host = UserLightSerializer(source='user', read_only=True)
    recipe = RecipeLightSerializer(read_only=True)
    meal_time_display = serializers.CharField(source='get_meal_time_display', read_only=True)
    meal_type_display = serializers.CharField(source='get_meal_type_display', read_only=True)
    participants = serializers.SerializerMethodField()
    
    class Meta:
        model = MealPlan
        fields = [
            'id', 'date', 'meal_time', 'meal_time_display',
            'meal_type', 'meal_type_display', 'confirmed', 'is_cooked',
            'recipe', 'host', 'participants',
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


class CookingProgressSerializer(serializers.ModelSerializer):
    recipe_title = serializers.CharField(source='recipe.title', read_only=True)
    recipe_image_url = serializers.URLField(source='recipe.image_url', read_only=True)
    status_display = serializers.CharField(source='get_status_display', read_only=True)
    
    class Meta:
        model = CookingProgress
        fields = [
            'id', 'user', 'recipe', 'recipe_title', 'recipe_image_url',
            'meal_plan', 'current_step_index', 'status', 'status_display',
            'started_at', 'completed_at', 'total_time_minutes',
            'created_at', 'updated_at'
        ]
        read_only_fields = ['user', 'started_at', 'created_at', 'updated_at']


class CookingProgressCreateUpdateSerializer(serializers.ModelSerializer):
    """Serializer pour créer/mettre à jour une progression"""
    
    class Meta:
        model = CookingProgress
        fields = [
            'recipe', 'meal_plan', 'current_step_index', 'status',
            'completed_at', 'total_time_minutes'
        ]
        read_only_fields = []
    
    def create(self, validated_data):
        validated_data['user'] = self.context['request'].user
        return super().create(validated_data)


class TimerSerializer(serializers.ModelSerializer):
    recipe_title = serializers.CharField(source='recipe.title', read_only=True)
    step_title = serializers.CharField(source='step.title', read_only=True)
    step_order = serializers.IntegerField(source='step.order', read_only=True)
    meal_plan = serializers.SerializerMethodField()
    
    class Meta:
        model = Timer
        fields = [
            'id', 'user', 'cooking_progress', 'step', 'step_title', 'step_order',
            'recipe', 'recipe_title', 'meal_plan', 'duration_minutes', 'remaining_seconds',
            'started_at', 'expires_at', 'is_completed', 'created_at', 'updated_at'
        ]
        read_only_fields = ['user', 'started_at', 'expires_at', 'created_at', 'updated_at']
    
    def get_meal_plan(self, obj):
        """Récupérer l'ID du meal plan depuis cooking_progress"""
        if obj.cooking_progress and obj.cooking_progress.meal_plan_id:
            return obj.cooking_progress.meal_plan_id
        return None


class TimerCreateSerializer(serializers.ModelSerializer):
    """Serializer pour créer un minuteur"""
    
    class Meta:
        model = Timer
        fields = [
            'cooking_progress', 'step', 'recipe', 'duration_minutes', 'remaining_seconds'
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
    meal_plan_id = serializers.IntegerField(source='meal_plan.id', read_only=True)
    post_id = serializers.IntegerField(source='post.id', read_only=True)
    image_url = serializers.SerializerMethodField()
    presigned_url = serializers.SerializerMethodField()
    
    class Meta:
        model = PostPhoto
        fields = [
            'id', 'photo_type', 'photo_type_display', 'image_path', 'image_url', 'presigned_url',
            'step', 'step_order', 'step_title', 'captured_label',
            'time_display', 'meal_plan_id', 'post_id', 'editable', 'order', 'created_at'
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
        owner = None
        if obj.meal_plan_id and obj.meal_plan:
            owner = obj.meal_plan.user
        elif obj.post_id and obj.post:
            owner = obj.post.user
        return owner == request.user


class PostSerializer(serializers.ModelSerializer):
    photos = PostPhotoSerializer(many=True, read_only=True)
    user = UserLightSerializer(read_only=True)
    recipe = RecipeLightSerializer(read_only=True)
    photos_count = serializers.IntegerField(read_only=True)
    has_all_photos = serializers.BooleanField(read_only=True)
    recipe_meta = serializers.SerializerMethodField()
    cookies_count = serializers.SerializerMethodField()
    has_cookie_from_user = serializers.SerializerMethodField()
    
    class Meta:
        model = Post
        fields = [
            'id', 'user', 'recipe', 'meal_plan', 'cooking_progress',
            'comment', 'is_published', 'recipe_meta',
            'photos', 'photos_count', 'has_all_photos',
            'cookies_count', 'has_cookie_from_user',
            'created_at', 'updated_at'
        ]
        read_only_fields = ['user', 'created_at', 'updated_at']
    
    def get_recipe_meta(self, obj):
        recipe = obj.recipe
        meal_plan = obj.meal_plan
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


class PostCreateUpdateSerializer(serializers.ModelSerializer):
    """Serializer pour créer/mettre à jour un post"""
    
    class Meta:
        model = Post
        fields = [
            'id', 'recipe', 'meal_plan', 'cooking_progress', 'comment', 'is_published'
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
    meal_plans = ShoppingListMealPlanSerializer(many=True, read_only=True)
    meal_plan_ids = serializers.PrimaryKeyRelatedField(
        queryset=MealPlan.objects.all(),
        source='meal_plans',
        many=True,
        write_only=True,
        required=False
    )
    items_count = serializers.SerializerMethodField()
    
    class Meta:
        model = ShoppingList
        fields = [
            'id', 'name', 'meal_plans', 'meal_plan_ids', 'is_active', 'is_archived',
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
    
    class Meta:
        model = Collection
        fields = [
            'id', 'name', 'description', 'owner', 'is_public', 'is_collaborative',
            'cover_image_path', 'cover_image_url', 'recipes_count', 'collection_recipes',
            'created_at', 'updated_at'
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


class CollectionCreateSerializer(serializers.ModelSerializer):
    """Serializer pour créer une collection"""
    
    class Meta:
        model = Collection
        fields = ['name', 'description', 'is_public', 'is_collaborative', 'cover_image_path']
    
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


class RecipeImportRequestSerializer(serializers.ModelSerializer):
    recipe = RecipeSerializer(read_only=True)

    class Meta:
        model = RecipeImportRequest
        fields = ['id', 'status', 'recipe', 'error_message', 'created_at', 'updated_at']
