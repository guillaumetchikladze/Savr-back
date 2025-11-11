from rest_framework import serializers
from .models import Recipe, Step, Ingredient, RecipeIngredient, MealPlan, MealInvitation


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


class StepSerializer(serializers.ModelSerializer):
    class Meta:
        model = Step
        fields = ['id', 'order', 'instruction']


class RecipeSerializer(serializers.ModelSerializer):
    steps = StepSerializer(many=True, read_only=True)
    recipe_ingredients = RecipeIngredientSerializer(many=True, read_only=True)
    created_by_username = serializers.CharField(source='created_by.username', read_only=True)
    meal_type_display = serializers.CharField(source='get_meal_type_display', read_only=True)
    difficulty_display = serializers.CharField(source='get_difficulty_display', read_only=True)
    
    class Meta:
        model = Recipe
        fields = [
            'id', 'title', 'description', 'meal_type', 'meal_type_display',
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
            'title', 'description', 'meal_type', 'difficulty',
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


class MealPlanSerializer(serializers.ModelSerializer):
    from accounts.serializers import UserSerializer
    
    recipe = RecipeSerializer(read_only=True)
    recipe_id = serializers.PrimaryKeyRelatedField(
        queryset=Recipe.objects.all(),
        source='recipe',
        write_only=True,
        required=False,
        allow_null=True
    )
    shared_with = UserSerializer(many=True, read_only=True)
    shared_with_ids = serializers.ListField(
        child=serializers.IntegerField(),
        write_only=True,
        required=False,
        allow_empty=True
    )
    meal_type_display = serializers.CharField(source='get_meal_type_display', read_only=True)
    meal_time_display = serializers.CharField(source='get_meal_time_display', read_only=True)
    user = UserSerializer(read_only=True)
    
    class Meta:
        model = MealPlan
        fields = [
            'id', 'date', 'meal_time', 'meal_time_display',
            'meal_type', 'meal_type_display', 'recipe', 'recipe_id',
            'shared_with', 'shared_with_ids', 'user', 'confirmed',
            'created_at', 'updated_at'
        ]
        read_only_fields = ['user', 'created_at', 'updated_at']
    
    def validate_shared_with_ids(self, value):
        """Valider que les IDs sont des complices valides"""
        from django.contrib.auth import get_user_model
        from accounts.models import Follow
        User = get_user_model()
        request = self.context.get('request')
        
        if not request or not request.user.is_authenticated:
            return []
        
        # Récupérer les IDs des complices (following + followers)
        following_ids = Follow.objects.filter(follower=request.user).values_list('following_id', flat=True)
        followers_ids = Follow.objects.filter(following=request.user).values_list('follower_id', flat=True)
        complice_ids = set(list(following_ids) + list(followers_ids))
        
        # Filtrer pour ne garder que les complices valides
        valid_ids = [user_id for user_id in value if user_id in complice_ids]
        
        # Vérifier que tous les utilisateurs existent
        existing_users = User.objects.filter(id__in=valid_ids).values_list('id', flat=True)
        return [user_id for user_id in valid_ids if user_id in existing_users]
    
    def create(self, validated_data):
        shared_with_ids = validated_data.pop('shared_with_ids', [])
        validated_data['user'] = self.context['request'].user
        meal_plan = super().create(validated_data)
        if shared_with_ids:
            from django.contrib.auth import get_user_model
            User = get_user_model()
            users = User.objects.filter(id__in=shared_with_ids)
            meal_plan.shared_with.set(users)
        return meal_plan
    
    def update(self, instance, validated_data):
        shared_with_ids = validated_data.pop('shared_with_ids', None)
        meal_plan = super().update(instance, validated_data)
        if shared_with_ids is not None:
            from django.contrib.auth import get_user_model
            User = get_user_model()
            users = User.objects.filter(id__in=shared_with_ids)
            meal_plan.shared_with.set(users)
        return meal_plan

