from rest_framework import serializers
from .models import Recipe, Step, Ingredient, RecipeIngredient, StepIngredient, MealPlan, MealInvitation, CookingProgress, Timer, Post, PostPhoto
from django.contrib.auth import get_user_model
User = get_user_model()

class UserLightSerializer(serializers.ModelSerializer):
    class Meta:
        model = User
        fields = ['id', 'username', 'avatar_url']


class IngredientSerializer(serializers.ModelSerializer):
    class Meta:
        model = Ingredient
        fields = ['id', 'name']


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
    
    class Meta:
        model = Recipe
        fields = [
            'id', 'title', 'description', 'steps_summary', 'meal_type', 'meal_type_display',
            'difficulty', 'difficulty_display', 'prep_time', 'cook_time',
            'servings', 'image_url', 'created_by', 'created_by_username',
            'created_at', 'updated_at', 'steps', 'recipe_ingredients'
        ]
        read_only_fields = ['created_by', 'created_at', 'updated_at']


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
            'prep_time', 'cook_time', 'servings', 'image_url',
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
    
    class Meta:
        model = Recipe
        fields = ['id', 'title', 'image_url', 'meal_type', 'meal_type_display', 'difficulty', 'difficulty_display', 'prep_time', 'cook_time', 'servings']


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
            'user', 'participants', 'confirmed',
            'created_at', 'updated_at'
        ]
        read_only_fields = ['user', 'participants', 'created_at', 'updated_at']
    
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
            'meal_type', 'meal_type_display', 'confirmed',
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
            'meal_type', 'meal_type_display', 'confirmed',
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
            'meal_type', 'meal_type_display', 'confirmed',
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
        """Générer une URL pré-signée pour l'image"""
        if not obj.image_path:
            return None
        
        from django.conf import settings
        import boto3
        
        # Si pas de configuration S3, retourner l'URL directe
        if not settings.AWS_ACCESS_KEY_ID or not settings.AWS_SECRET_ACCESS_KEY or not settings.AWS_BUCKET:
            return self.get_image_url(obj)
        
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
            # En cas d'erreur, retourner l'URL directe
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
        if meal_plan and hasattr(meal_plan, 'invitations'):
            invitations = meal_plan.invitations.all()
            shared_with += len([
                inv for inv in invitations if inv.status in ['accepted', 'pending']
            ])
        elif meal_plan:
            shared_with += MealInvitation.objects.filter(
                meal_plan=meal_plan,
                status__in=['accepted', 'pending']
            ).count()
        return {
            'title': recipe.title,
            'total_time': total_time,
            'servings': servings,
            'shared_with': shared_with,
        }
    
    def get_cookies_count(self, obj):
        """Nombre total de cookies sur le post"""
        return obj.cookies.count()
    
    def get_has_cookie_from_user(self, obj):
        """Vérifie si l'utilisateur actuel a donné un cookie à ce post"""
        request = self.context.get('request')
        if not request or request.user.is_anonymous:
            return False
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
