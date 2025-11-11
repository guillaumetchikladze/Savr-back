from django.db import models
from django.conf import settings


class Ingredient(models.Model):
    """Ingrédient de base"""
    name = models.CharField(max_length=200, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)
    
    class Meta:
        ordering = ['name']
    
    def __str__(self):
        return self.name


class Recipe(models.Model):
    """Recette de cuisine"""
    MEAL_TYPE_CHOICES = [
        ('breakfast', 'Petit-déjeuner'),
        ('lunch', 'Déjeuner'),
        ('dinner', 'Dîner'),
        ('snack', 'En-cas'),
    ]
    
    DIFFICULTY_CHOICES = [
        ('easy', 'Facile'),
        ('medium', 'Moyen'),
        ('hard', 'Difficile'),
    ]
    
    title = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    meal_type = models.CharField(max_length=20, choices=MEAL_TYPE_CHOICES, default='lunch')
    difficulty = models.CharField(max_length=20, choices=DIFFICULTY_CHOICES, default='medium')
    prep_time = models.IntegerField(help_text="Temps de préparation en minutes")
    cook_time = models.IntegerField(help_text="Temps de cuisson en minutes")
    servings = models.IntegerField(default=4)
    image_url = models.URLField(blank=True, null=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='recipes'
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    # Many-to-many avec Ingredient via RecipeIngredient
    ingredients = models.ManyToManyField(
        Ingredient,
        through='RecipeIngredient',
        related_name='recipes'
    )
    
    class Meta:
        ordering = ['-created_at']
    
    def __str__(self):
        return f"{self.title} - {self.get_meal_type_display()}"


class RecipeIngredient(models.Model):
    """Relation many-to-many entre Recipe et Ingredient avec quantité et unité"""
    UNIT_CHOICES = [
        ('g', 'Grammes'),
        ('kg', 'Kilogrammes'),
        ('ml', 'Millilitres'),
        ('l', 'Litres'),
        ('tsp', 'Cuillère à café'),
        ('tbsp', 'Cuillère à soupe'),
        ('cup', 'Tasse'),
        ('piece', 'Pièce(s)'),
        ('pinch', 'Pincée(s)'),
        ('clove', 'Gousse(s)'),
    ]
    
    recipe = models.ForeignKey(Recipe, on_delete=models.CASCADE, related_name='recipe_ingredients')
    ingredient = models.ForeignKey(Ingredient, on_delete=models.CASCADE)
    quantity = models.DecimalField(max_digits=10, decimal_places=2)
    unit = models.CharField(max_length=20, choices=UNIT_CHOICES, default='g')
    
    class Meta:
        unique_together = ['recipe', 'ingredient']
        ordering = ['ingredient__name']
    
    def __str__(self):
        return f"{self.recipe.title} - {self.quantity} {self.get_unit_display()} {self.ingredient.name}"


class Step(models.Model):
    """Étape de préparation d'une recette"""
    recipe = models.ForeignKey(
        Recipe,
        on_delete=models.CASCADE,
        related_name='steps'
    )
    order = models.IntegerField()
    instruction = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)
    
    class Meta:
        ordering = ['recipe', 'order']
        unique_together = ['recipe', 'order']
    
    def __str__(self):
        return f"{self.recipe.title} - Étape {self.order}"


class MealPlan(models.Model):
    """Repas planifié par un utilisateur"""
    MEAL_TYPE_CHOICES = [
        ('cantine', 'Cantine'),
        ('takeaway', 'À emporter'),
        ('recipe', 'Recette'),
        ('unknown', 'Je ne sais pas'),
    ]
    
    MEAL_TIME_CHOICES = [
        ('lunch', 'Déjeuner'),
        ('dinner', 'Dîner'),
    ]
    
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='meal_plans'
    )
    date = models.DateField()
    meal_time = models.CharField(max_length=20, choices=MEAL_TIME_CHOICES)
    meal_type = models.CharField(max_length=20, choices=MEAL_TYPE_CHOICES)
    recipe = models.ForeignKey(
        Recipe,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='meal_plans'
    )
    shared_with = models.ManyToManyField(
        settings.AUTH_USER_MODEL,
        related_name='shared_meal_plans',
        blank=True,
        verbose_name='Partagé avec'
    )
    confirmed = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    class Meta:
        ordering = ['-date', 'meal_time']
        unique_together = ['user', 'date', 'meal_time']
    
    def __str__(self):
        return f"{self.user.email} - {self.date} - {self.get_meal_time_display()}"


class MealInvitation(models.Model):
    """Invitation à un repas partagé"""
    STATUS_CHOICES = [
        ('pending', 'En attente'),
        ('accepted', 'Acceptée'),
        ('declined', 'Refusée'),
    ]
    
    inviter = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='sent_meal_invitations',
        verbose_name='Inviteur'
    )
    invitee = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='received_meal_invitations',
        verbose_name='Invité'
    )
    meal_plan = models.ForeignKey(
        MealPlan,
        on_delete=models.CASCADE,
        related_name='invitations',
        verbose_name='Repas planifié'
    )
    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default='pending',
        verbose_name='Statut'
    )
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='Créé le')
    updated_at = models.DateTimeField(auto_now=True, verbose_name='Mis à jour le')
    
    class Meta:
        ordering = ['-created_at']
        unique_together = ['invitee', 'meal_plan']
        verbose_name = 'Invitation à un repas'
        verbose_name_plural = 'Invitations à des repas'
    
    def __str__(self):
        return f"{self.inviter.username} invite {self.invitee.username} - {self.meal_plan.date} - {self.meal_plan.get_meal_time_display()}"
