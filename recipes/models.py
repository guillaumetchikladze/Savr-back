import uuid
from django.db import models
from django.conf import settings
from pgvector.django import VectorField


class Category(models.Model):
    """Catégorie d'ingrédient (Fruits, Légumes, etc.)"""
    name = models.CharField(max_length=100, unique=True)
    display_order = models.IntegerField(default=0, help_text="Ordre d'affichage dans les listes")
    created_at = models.DateTimeField(auto_now_add=True)
    
    class Meta:
        ordering = ['display_order', 'name']
        verbose_name_plural = 'Categories'
    
    def __str__(self):
        return self.name


class Ingredient(models.Model):
    """Ingrédient de base"""
    name = models.CharField(max_length=200, unique=True)
    category = models.ForeignKey(
        Category,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='ingredients',
        help_text="Catégorie de l'ingrédient"
    )
    embedding = VectorField(
        dimensions=384,  # Dimension de BGE-small-en-v1.5
        null=True,
        blank=True,
        help_text="Vecteur d'embedding pour la recherche sémantique"
    )
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
    steps_summary = models.TextField(blank=True, help_text="Résumé des étapes de préparation")
    meal_type = models.CharField(max_length=20, choices=MEAL_TYPE_CHOICES, default='lunch')
    difficulty = models.CharField(max_length=20, choices=DIFFICULTY_CHOICES, default='medium')
    prep_time = models.IntegerField(help_text="Temps de préparation en minutes")
    cook_time = models.IntegerField(help_text="Temps de cuisson en minutes")
    servings = models.IntegerField(default=4)
    image_path = models.CharField(
        max_length=500,
        blank=True,
        null=True,
        help_text="Chemin relatif de l'image (ex: recipes/user/uuid.jpg)"
    )
    embedding = VectorField(
        dimensions=384,
        null=True,
        blank=True,
        help_text="Embedding sémantique pour la recherche"
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='recipes'
    )
    is_public = models.BooleanField(
        default=True,
        help_text="Recette publique ou privée"
    )
    source_type = models.CharField(
        max_length=20,
        choices=[
            ('user_created', 'Créée par l\'utilisateur'),
            ('imported', 'Importée'),
            ('system', 'Système'),
        ],
        default='user_created',
        help_text="Type de source de la recette"
    )
    import_source_url = models.URLField(
        blank=True,
        null=True,
        help_text="URL source si la recette a été importée"
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

    @property
    def image_url(self):
        if not self.image_path:
            return None
        if str(self.image_path).startswith('http'):
            return self.image_path
        try:
            from savr_back.settings import build_presigned_get_url
            return build_presigned_get_url(self.image_path)
        except Exception:
            try:
                from savr_back.settings import build_s3_url
                return build_s3_url(self.image_path)
            except Exception:
                return self.image_path


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
        ordering = ['id']  # Ordre d'insertion en base de données (plus petit ID = inséré en premier)
    
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
    title = models.CharField(max_length=200, blank=True, help_text="Titre court de l'étape")
    instruction = models.TextField()
    tip = models.TextField(blank=True, help_text="Astuce ou conseil pour cette étape")
    has_timer = models.BooleanField(default=False, help_text="Cette étape nécessite un minuteur")
    timer_duration = models.IntegerField(null=True, blank=True, help_text="Durée par défaut du minuteur en minutes")
    created_at = models.DateTimeField(auto_now_add=True)
    
    # Many-to-many avec Ingredient via StepIngredient
    ingredients = models.ManyToManyField(
        Ingredient,
        through='StepIngredient',
        related_name='steps',
        blank=True
    )
    
    class Meta:
        ordering = ['recipe', 'order']
        unique_together = ['recipe', 'order']
    
    def __str__(self):
        return f"{self.recipe.title} - Étape {self.order}"


class StepIngredient(models.Model):
    """Relation many-to-many entre Step et Ingredient avec quantité et unité"""
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
    
    step = models.ForeignKey(Step, on_delete=models.CASCADE, related_name='step_ingredients')
    ingredient = models.ForeignKey(Ingredient, on_delete=models.CASCADE)
    quantity = models.DecimalField(max_digits=10, decimal_places=2)
    unit = models.CharField(max_length=20, choices=UNIT_CHOICES, default='g')
    
    class Meta:
        unique_together = ['step', 'ingredient']
        ordering = ['ingredient__name']
    
    def __str__(self):
        return f"{self.step.recipe.title} - Étape {self.step.order} - {self.quantity} {self.get_unit_display()} {self.ingredient.name}"


class RecipeImportRequest(models.Model):
    """Requête d'import/formalisation asynchrone d'une recette"""
    STATUS_PENDING = 'pending'
    STATUS_PROCESSING = 'processing'
    STATUS_SUCCESS = 'success'
    STATUS_ERROR = 'error'
    STATUS_CHOICES = [
        (STATUS_PENDING, 'En attente'),
        (STATUS_PROCESSING, 'En cours'),
        (STATUS_SUCCESS, 'Terminé'),
        (STATUS_ERROR, 'Erreur'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='recipe_import_requests',
    )
    payload = models.JSONField(help_text="Données brutes envoyées par l'utilisateur")
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_PENDING)
    recipe = models.ForeignKey(
        Recipe,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name='import_requests',
    )
    error_message = models.TextField(blank=True)
    task_id = models.CharField(max_length=255, blank=True, null=True, help_text="ID de la tâche Celery associée")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"ImportRequest<{self.id}> - {self.status}"


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
    confirmed = models.BooleanField(default=False)
    is_cooked = models.BooleanField(default=False, help_text="Le plat a été cuisiné")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    class Meta:
        ordering = ['-date', 'meal_time']
        unique_together = ['user', 'date', 'meal_time']
        indexes = [
            models.Index(fields=['user', 'date'], name='mealplan_user_date_idx'),
            models.Index(fields=['user', 'is_cooked'], name='mealplan_user_cooked_idx'),
        ]
    
    def __str__(self):
        return f"{self.user.email} - {self.date} - {self.get_meal_time_display()}"


class MealPlanRecipe(models.Model):
    """Relation entre MealPlan et Recipe avec ratio personnalisable"""
    meal_plan = models.ForeignKey(
        MealPlan,
        on_delete=models.CASCADE,
        related_name='meal_plan_recipes',
        verbose_name='Repas planifié'
    )
    recipe = models.ForeignKey(
        Recipe,
        on_delete=models.CASCADE,
        related_name='meal_plan_recipes',
        verbose_name='Recette'
    )
    ratio = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=1.0,
        help_text="Ratio à appliquer aux quantités de la recette (défaut: 1.0)"
    )
    order = models.IntegerField(
        default=0,
        help_text="Ordre d'affichage des recettes dans le meal plan"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    class Meta:
        unique_together = ['meal_plan', 'recipe']
        ordering = ['order', 'created_at']
        indexes = [
            models.Index(fields=['meal_plan', 'order'], name='mpr_mealplan_order_idx'),
        ]
    
    def __str__(self):
        return f"{self.meal_plan} - {self.recipe.title} (ratio: {self.ratio})"


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


class CookingProgress(models.Model):
    """Progression de cuisson d'une recette par un utilisateur"""
    STATUS_CHOICES = [
        ('in_progress', 'En cours'),
        ('completed', 'Terminée'),
        ('abandoned', 'Abandonnée'),
    ]
    
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='cooking_progresses'
    )
    recipe = models.ForeignKey(
        Recipe,
        on_delete=models.CASCADE,
        related_name='cooking_progresses'
    )
    meal_plan = models.ForeignKey(
        MealPlan,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='cooking_progresses'
    )
    current_step_index = models.IntegerField(default=0, help_text="Index de l'étape actuelle (0-based)")
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='in_progress')
    started_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    total_time_minutes = models.IntegerField(null=True, blank=True, help_text="Temps total en minutes")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    class Meta:
        ordering = ['-updated_at']
        indexes = [
            models.Index(fields=['user', 'status'], name='cookprog_user_status_idx'),
            models.Index(fields=['recipe', 'status'], name='cookprog_recipe_status_idx'),
        ]
        # Permettre une seule progression en cours par recette/meal_plan
        constraints = [
            models.UniqueConstraint(
                fields=['user', 'recipe', 'meal_plan', 'status'],
                condition=models.Q(status='in_progress'),
                name='unique_in_progress_per_recipe_mealplan'
            ),
        ]
    
    def __str__(self):
        return f"{self.user.email} - {self.recipe.title} - Étape {self.current_step_index + 1}"
    
    def complete(self):
        """Marquer la progression comme terminée"""
        from django.utils import timezone
        self.status = 'completed'
        self.completed_at = timezone.now()
        if self.started_at:
            delta = self.completed_at - self.started_at
            self.total_time_minutes = int(delta.total_seconds() / 60)
        
        # Marquer le meal_plan comme cuisiné
        if self.meal_plan:
            self.meal_plan.is_cooked = True
            self.meal_plan.save(update_fields=['is_cooked', 'updated_at'])
        self.save()


class Timer(models.Model):
    """Minuteur actif pour une étape de cuisson"""
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='active_timers'
    )
    cooking_progress = models.ForeignKey(
        CookingProgress,
        on_delete=models.CASCADE,
        related_name='timers',
        null=True,
        blank=True
    )
    step = models.ForeignKey(
        Step,
        on_delete=models.CASCADE,
        related_name='timers'
    )
    recipe = models.ForeignKey(
        Recipe,
        on_delete=models.CASCADE,
        related_name='active_timers'
    )
    duration_minutes = models.IntegerField(help_text="Durée totale du minuteur en minutes")
    remaining_seconds = models.IntegerField(help_text="Secondes restantes")
    started_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField(help_text="Date et heure d'expiration")
    is_completed = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    class Meta:
        ordering = ['expires_at']
        indexes = [
            models.Index(fields=['user', 'is_completed'], name='timer_user_completed_idx'),
            models.Index(fields=['expires_at'], name='timer_expires_at_idx'),
        ]
    
    def __str__(self):
        return f"{self.user.email} - {self.recipe.title} - Étape {self.step.order} - {self.remaining_seconds}s"
    
    def save(self, *args, **kwargs):
        from django.utils import timezone
        if not self.expires_at:
            self.expires_at = timezone.now() + timezone.timedelta(seconds=self.remaining_seconds)
        super().save(*args, **kwargs)


class Post(models.Model):
    """Post créé pendant la cuisine avec photos"""
    PHOTO_TYPE_CHOICES = [
        ('during_cooking', 'Pendant la cuisine'),
        ('after_cooking', 'Après la cuisine'),
        ('at_meal_time', 'À l\'heure du repas'),
        ('spontaneous', 'Spontanée'),
        ('imported_after_cooking', 'Importée après la recette'),
    ]
    
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='posts'
    )
    recipe = models.ForeignKey(
        Recipe,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='posts'
    )
    meal_plan = models.ForeignKey(
        MealPlan,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='posts'
    )
    cooking_progress = models.ForeignKey(
        CookingProgress,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='posts'
    )
    comment = models.TextField(blank=True, help_text="Commentaire du post")
    is_published = models.BooleanField(default=False, help_text="Le post est publié")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['user', 'is_published'], name='post_user_published_idx'),
            models.Index(fields=['recipe'], name='post_recipe_idx'),
            models.Index(fields=['is_published', '-created_at'], name='post_published_created_idx'),
        ]
    
    def __str__(self):
        return f"{self.user.email} - {self.recipe.title if self.recipe else 'Sans recette'} - {self.created_at.strftime('%d/%m/%Y')}"
    
    @property
    def photos_count(self):
        """Nombre de photos dans le post"""
        return self.photos.count()
    
    @property
    def has_all_photos(self):
        """Vérifie si le post a les 3 photos requises (during, after, at_meal_time)"""
        photo_types = set(self.photos.values_list('photo_type', flat=True))
        required_types = {'during_cooking', 'after_cooking', 'at_meal_time'}
        return required_types.issubset(photo_types)


class PostPhoto(models.Model):
    """Photo d'un post ou d'un meal_plan (avant publication)"""
    PHOTO_TYPE_CHOICES = [
        ('during_cooking', 'Pendant la cuisine'),
        ('after_cooking', 'Après la cuisine'),
        ('at_meal_time', 'À l\'heure du repas'),
        ('spontaneous', 'Spontanée'),
        ('imported_after_cooking', 'Importée après la recette'),
    ]

    UNIQUE_TYPES = ('during_cooking', 'after_cooking', 'at_meal_time')
    
    post = models.ForeignKey(
        Post,
        on_delete=models.CASCADE,
        related_name='photos',
        null=True,
        blank=True,
        help_text="Post associé (null si pas encore publié)"
    )
    meal_plan = models.ForeignKey(
        'MealPlan',
        on_delete=models.CASCADE,
        related_name='draft_photos',
        null=True,
        blank=True,
        help_text="Meal plan associé (avant publication)"
    )
    photo_type = models.CharField(
        max_length=25,
        choices=PHOTO_TYPE_CHOICES,
        help_text="Type de photo"
    )
    image_path = models.CharField(
        max_length=500,
        help_text="Chemin relatif de la photo dans S3 (ex: meal_plans/70/6096a520a71247229f1cae315fc2bd84.jpg)"
    )
    step = models.ForeignKey(
        Step,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='post_photos',
        help_text="Étape associée si photo spontanée"
    )
    order = models.PositiveIntegerField(
        default=0,
        help_text="Ordre d'affichage dans le post (0 = non ordonné, utilise created_at)"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    
    @property
    def image_url(self):
        """Générer l'URL pré-signée pour accéder à la photo"""
        if not self.image_path:
            return None
        if str(self.image_path).startswith('http'):
            return self.image_path
        try:
            from savr_back.settings import build_presigned_get_url
            return build_presigned_get_url(self.image_path)
        except Exception:
            return None
    
    class Meta:
        ordering = ['order', 'created_at']
        # Un seul photo de chaque type par post ou meal_plan (sauf spontaneous qui peut être multiple)
        constraints = [
            models.UniqueConstraint(
                fields=['post', 'photo_type'],
                condition=models.Q(photo_type__in=['during_cooking', 'after_cooking', 'at_meal_time']) & models.Q(post__isnull=False),
                name='unique_photo_type_per_post'
            ),
            models.UniqueConstraint(
                fields=['meal_plan', 'photo_type'],
                condition=models.Q(photo_type__in=['during_cooking', 'after_cooking', 'at_meal_time']) & models.Q(meal_plan__isnull=False),
                name='unique_photo_type_per_meal_plan'
            ),
        ]
    
    def __str__(self):
        if self.post:
            return f"{self.post.user.email} - {self.get_photo_type_display()} - {self.created_at.strftime('%d/%m/%Y %H:%M')}"
        elif self.meal_plan:
            return f"{self.meal_plan.user.email} - {self.get_photo_type_display()} - {self.created_at.strftime('%d/%m/%Y %H:%M')}"
        return f"{self.get_photo_type_display()} - {self.created_at.strftime('%d/%m/%Y %H:%M')}"


class PostCookie(models.Model):
    """Cookie (like) donné à un post par un utilisateur"""
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='post_cookies'
    )
    post = models.ForeignKey(
        Post,
        on_delete=models.CASCADE,
        related_name='cookies'
    )
    created_at = models.DateTimeField(auto_now_add=True)
    
    class Meta:
        ordering = ['-created_at']
        unique_together = ['user', 'post']
        indexes = [
            models.Index(fields=['post', 'created_at'], name='postcookie_post_created_idx'),
        ]
    
    def __str__(self):
        return f"{self.user.username} cookie on {self.post.id}"


class ShoppingList(models.Model):
    """Liste de courses créée par un utilisateur"""
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='shopping_lists'
    )
    name = models.CharField(
        max_length=200,
        blank=True,
        help_text="Nom de la liste (optionnel, par défaut date de création)"
    )
    meal_plans = models.ManyToManyField(
        MealPlan,
        related_name='shopping_lists',
        help_text="Repas planifiés inclus dans cette liste"
    )
    is_active = models.BooleanField(
        default=True,
        help_text="Liste active (une seule liste active par utilisateur à la fois)"
    )
    is_archived = models.BooleanField(
        default=False,
        help_text="Liste archivée"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['user', 'is_active'], name='shoppinglist_user_active_idx'),
        ]
    
    def __str__(self):
        return f"{self.user.username} - {self.name or f'Liste du {self.created_at.strftime("%d/%m/%Y")}'}"


class ShoppingListItem(models.Model):
    """Item de liste de courses pour un ingrédient dans une liste"""
    STATUS_CHOICES = [
        ('to_buy', 'À acheter'),
        ('in_pantry', 'Dans les placards'),
        ('purchased', 'Acheté'),
    ]
    
    shopping_list = models.ForeignKey(
        ShoppingList,
        on_delete=models.CASCADE,
        related_name='items'
    )
    ingredient = models.ForeignKey(
        Ingredient,
        on_delete=models.CASCADE,
        related_name='shopping_list_items'
    )
    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default='to_buy'
    )
    pantry_quantity = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=0,
        help_text="Quantité déjà disponible dans les placards"
    )
    pantry_unit = models.CharField(
        max_length=20,
        blank=True,
        help_text="Unité de la quantité dans les placards"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    class Meta:
        ordering = ['-updated_at']
        unique_together = ['shopping_list', 'ingredient']
        indexes = [
            models.Index(fields=['shopping_list', 'status'], name='shoplistitem_status_idx'),
        ]
    
    def __str__(self):
        return f"{self.shopping_list} - {self.ingredient.name} - {self.get_status_display()}"


class Collection(models.Model):
    """Collection de recettes (style Pinterest)"""
    name = models.CharField(max_length=200, help_text="Nom de la collection")
    description = models.TextField(blank=True, help_text="Description de la collection")
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='collections',
        help_text="Propriétaire de la collection"
    )
    is_public = models.BooleanField(default=True, help_text="Collection publique ou privée")
    is_collaborative = models.BooleanField(default=False, help_text="Mode collaboratif activé")
    cover_image_path = models.CharField(
        max_length=500,
        blank=True,
        null=True,
        help_text="Chemin relatif de l'image de couverture dans S3"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    # Many-to-many avec Recipe via CollectionRecipe
    recipes = models.ManyToManyField(
        Recipe,
        through='CollectionRecipe',
        related_name='collections',
        blank=True
    )
    
    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['owner', 'is_public'], name='collection_owner_public_idx'),
        ]
    
    def __str__(self):
        return f"{self.name} - {self.owner.username}"
    
    @property
    def recipes_count(self):
        """Nombre de recettes dans la collection"""
        return self.recipes.count()


class CollectionRecipe(models.Model):
    """Relation many-to-many entre Collection et Recipe"""
    collection = models.ForeignKey(
        Collection,
        on_delete=models.CASCADE,
        related_name='collection_recipes'
    )
    recipe = models.ForeignKey(
        Recipe,
        on_delete=models.CASCADE,
        related_name='collection_recipes'
    )
    added_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='added_collection_recipes',
        help_text="Utilisateur qui a ajouté la recette à la collection"
    )
    added_at = models.DateTimeField(auto_now_add=True)
    
    class Meta:
        unique_together = ['collection', 'recipe']
        ordering = ['-added_at']
        indexes = [
            models.Index(fields=['collection', 'recipe'], name='collrecipe_coll_recipe_idx'),
        ]
    
    def __str__(self):
        return f"{self.collection.name} - {self.recipe.title}"


class CollectionMember(models.Model):
    """Membre d'une collection (pour collaboration)"""
    ROLE_CHOICES = [
        ('owner', 'Propriétaire'),
        ('collaborator', 'Collaborateur'),
    ]
    
    collection = models.ForeignKey(
        Collection,
        on_delete=models.CASCADE,
        related_name='members'
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='collection_memberships'
    )
    role = models.CharField(
        max_length=20,
        choices=ROLE_CHOICES,
        default='collaborator',
        help_text="Rôle dans la collection"
    )
    joined_at = models.DateTimeField(auto_now_add=True)
    
    class Meta:
        unique_together = ['collection', 'user']
        ordering = ['joined_at']
    
    def __str__(self):
        return f"{self.user.username} - {self.collection.name} ({self.get_role_display()})"
