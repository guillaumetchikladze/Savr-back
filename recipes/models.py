import uuid
import random
from django.db import models
from django.db.models.signals import post_save
from django.dispatch import receiver
from django.conf import settings
from pgvector.django import VectorField


class Category(models.Model):
    """Rayon magasin (feuille = assignée aux ingrédients ; parent = zone du parcours)."""
    name = models.CharField(max_length=100, unique=True)
    slug = models.SlugField(
        max_length=80,
        unique=True,
        null=True,
        blank=True,
        help_text="Identifiant stable pour règles et migrations (feuilles uniquement de préférence).",
    )
    parent = models.ForeignKey(
        'self',
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name='children',
        help_text="Section (ex. Pôle frais). Vide pour racines historiques sans hiérarchie.",
    )
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
        dimensions=512,
        null=True,
        blank=True,
        help_text="Vecteur d'embedding pour la recherche sémantique (nomic 512d)",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    
    class Meta:
        ordering = ['name']
    
    def __str__(self):
        return self.name


class DietaryIngredientMatch(models.Model):
    """
    Lie un libellé de préférence utilisateur (allergie ou « je n’aime pas »)
    à des sous-chaînes cherchées dans les noms d’ingrédients (correspondance insensible à la casse).
    """

    preference_label = models.CharField(max_length=120, db_index=True)
    match_keyword = models.CharField(max_length=200)

    class Meta:
        unique_together = [('preference_label', 'match_keyword')]
        verbose_name = 'Correspondance préférence → ingrédient'
        verbose_name_plural = 'Correspondances préférences → ingrédients'

    def __str__(self):
        return f'{self.preference_label} → {self.match_keyword}'


class PreferenceMapping(models.Model):
    id = models.BigAutoField(primary_key=True)
    """
    Mapping communautaire: un label (allergie ou goût) -> mots-clés d'ingrédients.

    - `status=pending` = impact global uniquement en soft (score/tags), jamais en strict.
    - `status=validated` = peut être utilisé en strict (suggest) + warnings forts.
    """

    KIND_CHOICES = [
        ('allergy', 'Allergie'),
        ('dislike', 'Goût (n’aime pas)'),
    ]
    STATUS_CHOICES = [
        ('pending', 'À valider'),
        ('validated', 'Validé'),
    ]

    kind = models.CharField(max_length=16, choices=KIND_CHOICES, db_index=True)
    label = models.CharField(max_length=120, db_index=True)
    keywords = models.JSONField(default=list, blank=True)
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default='pending', db_index=True)

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='created_preference_mappings',
    )
    forked_from = models.ForeignKey(
        'self',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='forks',
    )

    usage_count = models.IntegerField(default=0)
    last_used_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = [('kind', 'label')]
        indexes = [
            models.Index(fields=['kind', 'status', 'label'], name='prefmap_kind_status_label_idx'),
            models.Index(fields=['kind', 'label'], name='prefmap_kind_label_idx'),
        ]
        ordering = ['kind', 'label']

    def __str__(self):
        return f'{self.kind}:{self.label} ({self.status})'


class UserPreferenceMapping(models.Model):
    id = models.BigAutoField(primary_key=True)
    """
    Fork utilisateur: override des mots-clés pour un label donné.
    Priorité sur le mapping communautaire.
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='preference_mapping_overrides',
    )
    kind = models.CharField(max_length=16, choices=PreferenceMapping.KIND_CHOICES, db_index=True)
    label = models.CharField(max_length=120, db_index=True)
    keywords = models.JSONField(default=list, blank=True)
    base_mapping = models.ForeignKey(
        PreferenceMapping,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='user_overrides',
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = [('user', 'kind', 'label')]
        indexes = [
            models.Index(fields=['user', 'kind', 'label'], name='upm_user_kind_label_idx'),
        ]
        ordering = ['kind', 'label']

    def __str__(self):
        return f'{self.user_id}:{self.kind}:{self.label}'


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
    class SearchIndexStatus(models.TextChoices):
        PENDING = "pending", "En cours"
        READY = "ready", "Prêt"
        FAILED = "failed", "Échec"

    embedding = VectorField(
        dimensions=512,
        null=True,
        blank=True,
        help_text="Embedding sémantique pour la recherche (nomic 512d)",
    )
    search_context_tags = models.JSONField(null=True, blank=True)
    search_index_text = models.TextField(blank=True, default="")
    search_index_hash = models.CharField(max_length=64, blank=True, default="")
    search_indexed_at = models.DateTimeField(null=True, blank=True)
    search_index_status = models.CharField(
        max_length=16,
        choices=SearchIndexStatus.choices,
        default=SearchIndexStatus.PENDING,
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


class RecipeBatch(models.Model):
    """
    Batch préparatoire pour une recette donnée.
    Permet de lier plusieurs meal plans à une préparation unique.
    """
    recipe = models.ForeignKey(
        Recipe,
        on_delete=models.CASCADE,
        related_name='batches'
    )
    name = models.CharField(max_length=255, blank=True, null=True)
    notes = models.TextField(blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='recipe_batches'
    )
    is_cooked = models.BooleanField(default=False, help_text="Au moins un meal plan lié est cuisiné")
    shopping_done = models.BooleanField(
        default=False,
        help_text="Courses terminées pour ce batch (soit utilisateur a tout coché, soit a choisi « J'ai déjà fait les courses »)"
    )
    photo_step_orders = models.JSONField(
        default=list,
        blank=True,
        help_text="Liste des step.order qui auront des étapes photo (ex: [3, 5])"
    )
    meal_time_photo_reminder_task_id = models.CharField(
        max_length=255,
        blank=True,
        default='',
        help_text="ID tâche Celery (rappel push photo à table) pour révocation si replanification",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['recipe', '-created_at'], name='recipebatch_recipe_idx'),
        ]
    
    def __str__(self):
        label = self.name or f"Batch {self.id}"
        return f"{label} - {self.recipe.title}"


def generate_photo_step_orders(recipe):
    """
    Génère les positions des étapes photo "during_cooking" pour un batch.

    Objectif produit : en moyenne ~1 photo toutes les 7 étapes,
    en excluant la première et la dernière étape (la photo finale est gérée séparément).
    """
    steps = list(recipe.steps.all().order_by('order'))
    total_steps = len(steps)
    
    if total_steps < 2:
        return []
    
    # Nombre théorique total de "blocs" de 7 étapes.
    # Exemple : 21 étapes => 3 blocs de 7 => 3 photos au total.
    # La photo finale étant toujours présente, on ne garde que (blocs - 1) photos intermédiaires,
    # mais on impose au moins 1 photo intermédiaire dès qu'il y a suffisamment d'étapes.
    # Quelques exemples :
    # - 21 étapes => 3 blocs => 2 photos intermédiaires + 1 finale
    # - 14 étapes => 2 blocs => 1 photo intermédiaire + 1 finale
    # - 5 étapes  => 0 bloc complet => 1 photo intermédiaire (min) + 1 finale
    num_intermediate_photos = max(1, (total_steps // 7) - 1)
    
    # Éviter la première et dernière étape
    if total_steps <= 2:
        available_orders = []
    else:
        available_orders = [s.order for s in steps[1:-1]]
    
    if not available_orders:
        return []
    
    # Si on a moins d'étapes disponibles que de photos souhaitées,
    # on met une photo sur chaque étape disponible.
    if len(available_orders) <= num_intermediate_photos:
        return sorted(available_orders)
    
    # Répartir de manière régulière les photos sur les étapes disponibles.
    # On calcule des indices "équidistants" dans la liste available_orders.
    # Exemple : 10 étapes disponibles, 2 photos => indices proches de 3 et 7.
    step_size = len(available_orders) / float(num_intermediate_photos + 1)
    selected_indices = []
    for i in range(1, num_intermediate_photos + 1):
        # Position théorique dans la liste (1-based entre les "blocs")
        pos = int(round(i * step_size)) - 1
        pos = max(0, min(pos, len(available_orders) - 1))
        selected_indices.append(pos)
    
    # Supprimer les doublons éventuels tout en conservant l'ordre
    seen = set()
    selected_orders = []
    for idx in selected_indices:
        order = available_orders[idx]
        if order not in seen:
            seen.add(order)
            selected_orders.append(order)
    
    return sorted(selected_orders)


@receiver(post_save, sender=RecipeBatch)
def generate_photo_steps_for_batch(sender, instance, created, **kwargs):
    """Génère aléatoirement les positions photo à la création d'un batch"""
    if created and not instance.photo_step_orders:
        instance.photo_step_orders = generate_photo_step_orders(instance.recipe)
        # Utiliser update pour éviter de déclencher le signal à nouveau
        RecipeBatch.objects.filter(pk=instance.pk).update(photo_step_orders=instance.photo_step_orders)


class Step(models.Model):
    """Étape de préparation d'une recette"""
    recipe = models.ForeignKey(
        Recipe,
        on_delete=models.CASCADE,
        related_name='steps',
        help_text="Recette associée"
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
        recipe_title = self.recipe.title if self.recipe else "Unknown"
        return f"{recipe_title} - Étape {self.order}"


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
        ('breakfast', 'Petit-déjeuner'),
        ('lunch', 'Déjeuner'),
        ('dinner', 'Dîner'),
        ('other', 'Autre'),
    ]
    
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='meal_plans'
    )
    date = models.DateField()
    meal_time = models.CharField(max_length=20, choices=MEAL_TIME_CHOICES)
    slot_key = models.CharField(
        max_length=64,
        blank=True,
        default='',
        help_text='lunch/dinner/breakfast ou UUID pour un créneau personnalisé (meal_time=other).',
    )
    custom_label = models.CharField(max_length=80, blank=True, default='')
    scheduled_time = models.TimeField(null=True, blank=True)
    meal_time_photo_reminder_task_id = models.CharField(
        max_length=255,
        blank=True,
        default='',
        help_text="ID tâche Celery (rappel push photo à table) pour révocation si replanification",
    )
    meal_type = models.CharField(max_length=20, choices=MEAL_TYPE_CHOICES)
    confirmed = models.BooleanField(default=False)
    guest_count = models.IntegerField(
        default=0,
        help_text="Nombre d'invités anonymes (sans compte) pour ce repas"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    class Meta:
        ordering = ['-date', 'meal_time', 'slot_key']
        constraints = [
            models.UniqueConstraint(
                fields=['user', 'date', 'slot_key'],
                name='mealplan_user_date_slotkey_uniq',
            ),
        ]
        indexes = [
            models.Index(fields=['user', 'date'], name='mealplan_user_date_idx'),
            models.Index(fields=['user', 'meal_time'], name='mealplan_user_meal_time_idx'),
        ]

    def save(self, *args, **kwargs):
        if not self.slot_key:
            self.slot_key = self.meal_time
        super().save(*args, **kwargs)
    
    def get_group_key(self):
        """
        Génère une clé unique pour identifier les meal plans du même groupe
        (même meal_time, mêmes recettes).
        Utilisé pour grouper les meal plans sur plusieurs dates.
        """
        mprbs = self.meal_plan_recipe_batches.all().order_by('order')
        if mprbs.exists():
            recipe_ids = ','.join(
                str(mpr.recipe_batch.recipe_id) for mpr in mprbs if mpr.recipe_batch_id
            )
            return f"{self.meal_time}|{recipe_ids}"
        return None
    
    def __str__(self):
        return f"{self.user.email} - {self.date} - {self.get_meal_time_display()}"


class MealPlanRecipeBatch(models.Model):
    """
    Relation entre MealPlan et RecipeBatch avec gestion des portions.
    portions=None => suit le nombre de personnes du repas.
    is_portions_overridden=True => l'utilisateur a modifié manuellement les portions.
    """
    meal_plan = models.ForeignKey(
        MealPlan,
        on_delete=models.CASCADE,
        related_name='meal_plan_recipe_batches',
        verbose_name='Repas planifié'
    )
    recipe_batch = models.ForeignKey(
        RecipeBatch,
        on_delete=models.CASCADE,
        related_name='meal_plan_recipe_batches',
        verbose_name='Batch'
    )
    portions = models.IntegerField(
        null=True,
        blank=True,
        help_text="Nombre de portions pour ce batch dans ce repas (None = suit le nombre de personnes)"
    )
    is_portions_overridden = models.BooleanField(
        default=False,
        help_text="True si l'utilisateur a détaché les portions du nombre de personnes"
    )
    order = models.IntegerField(
        default=0,
        help_text="Ordre d'affichage dans le meal plan"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    class Meta:
        unique_together = ['meal_plan', 'recipe_batch']
        ordering = ['order', 'created_at']
        indexes = [
            models.Index(fields=['meal_plan', 'order'], name='mprb_mealplan_order_idx'),
            models.Index(fields=['meal_plan', 'recipe_batch'], name='mprb_mealplan_batch_idx'),
        ]
    
    def __str__(self):
        return f"{self.meal_plan} - {self.recipe_batch} (portions: {self.portions}, overridden: {self.is_portions_overridden})"


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
        indexes = [
            models.Index(fields=['meal_plan', 'status'], name='mealinv_mealplan_status_idx'),
            models.Index(fields=['invitee', 'status'], name='mealinv_invitee_status_idx'),
            models.Index(fields=['meal_plan'], name='mealinv_mealplan_idx'),
        ]
    
    def __str__(self):
        return f"{self.inviter.username} invite {self.invitee.username} - {self.meal_plan.date} - {self.meal_plan.get_meal_time_display()}"


class CookingProgress(models.Model):
    """Progression de cuisson d'un batch par un utilisateur"""
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
    recipe_batch = models.ForeignKey(
        RecipeBatch,
        on_delete=models.CASCADE,
        related_name='cooking_progresses',
        null=True,
        blank=True
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
            models.Index(fields=['recipe_batch', 'status'], name='cookprog_batch_status_idx'),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=['user', 'recipe_batch', 'status'],
                condition=models.Q(status='in_progress'),
                name='unique_in_progress_per_batch'
            ),
        ]
    
    def __str__(self):
        return f"{self.user.email} - Batch {self.recipe_batch.id} - Étape {self.current_step_index + 1}"
    
    def complete(self):
        """Marquer la progression comme terminée"""
        from django.utils import timezone
        self.status = 'completed'
        self.completed_at = timezone.now()
        if self.started_at:
            delta = self.completed_at - self.started_at
            self.total_time_minutes = int(delta.total_seconds() / 60)
        self.save(update_fields=['status', 'completed_at', 'total_time_minutes', 'updated_at'])
        # Marquer le batch comme cuisiné
        if self.recipe_batch and not self.recipe_batch.is_cooked:
            self.recipe_batch.is_cooked = True
            self.recipe_batch.save(update_fields=['is_cooked', 'updated_at'])


class Timer(models.Model):
    """Minuteur actif pour une étape de cuisson (batch)"""
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
    recipe_batch = models.ForeignKey(
        RecipeBatch,
        on_delete=models.CASCADE,
        related_name='active_timers',
        null=True,
        blank=True
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
        return f"{self.user.email} - Batch {self.recipe_batch.id} - Étape {self.step.order} - {self.remaining_seconds}s"
    
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
    recipe_batch = models.ForeignKey(
        RecipeBatch,
        on_delete=models.CASCADE,
        related_name='posts',
        null=True,
        blank=True
    )
    meal_plan = models.ForeignKey(
        'MealPlan',
        on_delete=models.SET_NULL,
        related_name='posts',
        null=True,
        blank=True,
        help_text="Repas associé (nouveau workflow : post du repas).",
    )
    comment = models.TextField(blank=True, help_text="Commentaire du post")
    is_published = models.BooleanField(default=False, help_text="Le post est publié")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['user', 'is_published'], name='post_user_published_idx'),
            models.Index(fields=['recipe_batch'], name='post_recipebatch_idx'),
            models.Index(fields=['is_published', '-created_at'], name='post_published_created_idx'),
        ]
    
    def __str__(self):
        if self.meal_plan_id:
            return f"{self.user.email} - MealPlan {self.meal_plan_id} - {self.created_at.strftime('%d/%m/%Y')}"
        if self.recipe_batch_id:
            return f"{self.user.email} - Batch {self.recipe_batch_id} - {self.created_at.strftime('%d/%m/%Y')}"
        return f"{self.user.email} - Post {self.id} - {self.created_at.strftime('%d/%m/%Y')}"
    
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

    # Plusieurs photos `at_meal_time` par batch autorisées (contraintes DB retirées, voir migrations).
    UNIQUE_TYPES = ()

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
        help_text="Repas associé (upload temporaire avant attribution à une recette)",
    )
    recipe_batch = models.ForeignKey(
        RecipeBatch,
        on_delete=models.CASCADE,
        related_name='draft_photos',
        null=True,
        blank=True,
        help_text="Batch associé (avant publication)"
    )
    photo_type = models.CharField(
        max_length=25,
        choices=PHOTO_TYPE_CHOICES,
        help_text="Type de photo"
    )
    is_draft = models.BooleanField(
        default=False,
        help_text="Photo en cours de capture (pas encore uploadée)"
    )
    image_path = models.CharField(
        max_length=500,
        blank=True,
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
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='uploaded_post_photos',
        help_text="Utilisateur qui a uploadé la photo (suppression réservée à cet utilisateur si défini)",
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
    
    def __str__(self):
        if self.post:
            return f"{self.post.user.email} - {self.get_photo_type_display()} - {self.created_at.strftime('%d/%m/%Y %H:%M')}"
        elif self.recipe_batch:
            return f"Batch {self.recipe_batch.id} - {self.get_photo_type_display()} - {self.created_at.strftime('%d/%m/%Y %H:%M')}"
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


class PostComment(models.Model):
    """Commentaire laissé par un utilisateur sur un post"""
    post = models.ForeignKey(
        Post,
        on_delete=models.CASCADE,
        related_name='comments'
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='post_comments'
    )
    text = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['created_at']
        indexes = [
            models.Index(fields=['post', 'created_at'], name='postcomment_post_created_idx'),
        ]

    def __str__(self):
        return f"{self.user.username} on post {self.post.id}"


class PostReport(models.Model):
    """Signalement d'un post publié par un utilisateur."""
    post = models.ForeignKey(
        Post,
        on_delete=models.CASCADE,
        related_name='reports',
    )
    reporter = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='post_reports',
    )
    reason = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['post', 'reporter'],
                name='uniq_post_report_per_user',
            ),
        ]

    def __str__(self):
        return f"Report post {self.post_id} by {self.reporter_id}"


class PostCommentLike(models.Model):
    """Like sur un commentaire de post"""
    comment = models.ForeignKey(
        PostComment,
        on_delete=models.CASCADE,
        related_name='likes',
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='post_comment_likes',
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']
        unique_together = ['comment', 'user']
        indexes = [
            models.Index(fields=['comment', 'created_at'], name='pcl_comment_created_idx'),
        ]

    def __str__(self):
        return f"{self.user.username} like on comment {self.comment_id}"


class ShoppingList(models.Model):
    """
    Liste de courses persistante (Maison, Appart, etc.).
    V2: collaboration + association de batches via ShoppingListBatch, et items multi-lignes via unit_group.
    """
    name = models.CharField(
        max_length=200,
        blank=True,
        help_text="Nom de la liste (ex: Maison, Appart, etc.)"
    )
    color = models.CharField(
        max_length=20,
        blank=True,
        help_text="Couleur optionnelle (hex ou token)"
    )
    is_archived = models.BooleanField(default=False, help_text="Liste archivée")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return self.name or f"Liste du {self.created_at.strftime('%d/%m/%Y')}"


class ShoppingListMember(models.Model):
    """Membre d'une liste de courses (collaboration)"""
    ROLE_CHOICES = [
        ('owner', 'Propriétaire'),
        ('collaborator', 'Collaborateur'),
    ]
    shopping_list = models.ForeignKey(
        ShoppingList,
        on_delete=models.CASCADE,
        related_name='members'
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='shopping_list_memberships'
    )
    role = models.CharField(
        max_length=20,
        choices=ROLE_CHOICES,
        default='collaborator'
    )
    joined_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ['shopping_list', 'user']
        ordering = ['joined_at']
        indexes = [
            models.Index(fields=['shopping_list', 'role'], name='shoplistmember_role_idx'),
        ]

    def __str__(self):
        return f"{self.user.username} - {self.shopping_list} ({self.get_role_display()})"


class ShoppingListBatch(models.Model):
    """
    Association d'un RecipeBatch à une ShoppingList.
    V1: un batch ne peut être associé qu'à UNE seule liste (OneToOne).
    """
    shopping_list = models.ForeignKey(
        ShoppingList,
        on_delete=models.CASCADE,
        related_name='batches'
    )
    recipe_batch = models.OneToOneField(
        RecipeBatch,
        on_delete=models.CASCADE,
        related_name='shopping_list_batch'
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['shopping_list', 'created_at'], name='shoplistbatch_list_created_idx'),
        ]

    def __str__(self):
        return f"{self.shopping_list} - batch {self.recipe_batch_id}"


class ShoppingListLoyaltyCard(models.Model):
    """
    Association d'une carte de fidélité à une liste de courses.
    Une même carte peut être partagée sur plusieurs listes.
    """

    shopping_list = models.ForeignKey(
        ShoppingList,
        on_delete=models.CASCADE,
        related_name='loyalty_cards_links',
    )
    card = models.ForeignKey(
        'accounts.LoyaltyCard',
        on_delete=models.CASCADE,
        related_name='shopping_list_links',
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']
        unique_together = ['shopping_list', 'card']
        indexes = [
            models.Index(fields=['shopping_list'], name='shoplist_loyalty_list_idx'),
        ]

    def __str__(self):
        return f"{self.shopping_list} - carte {self.card_id}"


class ShoppingListItem(models.Model):
    """
    Ligne d'ingrédient dans une liste.
    V2: un même ingredient peut apparaître plusieurs fois si unités non convertibles (unit_group).
    """
    UNIT_GROUP_CHOICES = [
        ('weight', 'Poids (g/kg)'),
        ('volume', 'Volume (ml/l)'),
        ('count', 'Pièce'),
        ('pinch', 'Pincée'),
        ('clove', 'Gousse'),
        ('other', 'Autre'),
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
    unit_group = models.CharField(
        max_length=20,
        choices=UNIT_GROUP_CHOICES,
        default='other',
        help_text="Regroupement d'unité pour permettre plusieurs lignes par ingrédient"
    )
    pantry_quantity = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=0,
        help_text="Quantité déjà disponible dans les placards (par ligne)"
    )
    pantry_unit = models.CharField(
        max_length=20,
        blank=True,
        help_text="Unité de la quantité dans les placards (doit correspondre au groupe d'unité)"
    )
    checked_at = models.DateTimeField(null=True, blank=True, help_text="Dernière validation (ligne)")
    checked_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='shopping_list_item_checks'
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-updated_at']
        unique_together = ['shopping_list', 'ingredient', 'unit_group']
        indexes = [
            models.Index(fields=['shopping_list', 'unit_group'], name='shoplistitem_group_idx'),
            models.Index(fields=['shopping_list', 'checked_at'], name='shoplistitem_checked_idx'),
        ]

    def __str__(self):
        return f"{self.shopping_list} - {self.ingredient.name} ({self.unit_group})"


class ShoppingListItemQuantity(models.Model):
    """Quantité nécessaire (et cochée) pour un ingrédient, par batch. recipe_batch=None = ajout manuel."""
    shopping_list_item = models.ForeignKey(
        ShoppingListItem,
        on_delete=models.CASCADE,
        related_name='quantities'
    )
    recipe_batch = models.ForeignKey(
        RecipeBatch,
        on_delete=models.CASCADE,
        related_name='shopping_list_item_quantities',
        null=True,
        blank=True,
        help_text="Null = quantité ajoutée manuellement (pas liée à une recette)"
    )
    quantity = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    unit = models.CharField(max_length=20, blank=True)
    checked_quantity = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    checked_at = models.DateTimeField(null=True, blank=True)
    checked_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='shopping_list_item_quantity_checks'
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ['shopping_list_item', 'recipe_batch']
        ordering = ['-updated_at']
        indexes = [
            models.Index(fields=['recipe_batch', 'updated_at'], name='shopliq_batch_updated_idx'),
        ]

    def __str__(self):
        if self.recipe_batch_id:
            return f"{self.shopping_list_item} - batch {self.recipe_batch_id}"
        return f"{self.shopping_list_item} - manuel"


class ShoppingListInvitation(models.Model):
    """Invitation à collaborer sur une liste (pattern MealInvitation)."""
    STATUS_CHOICES = [
        ('pending', 'En attente'),
        ('accepted', 'Acceptée'),
        ('declined', 'Refusée'),
    ]

    inviter = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='sent_shopping_list_invitations'
    )
    invitee = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='received_shopping_list_invitations'
    )
    shopping_list = models.ForeignKey(
        ShoppingList,
        on_delete=models.CASCADE,
        related_name='invitations'
    )
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']
        unique_together = ['invitee', 'shopping_list']
        indexes = [
            models.Index(fields=['shopping_list', 'status'], name='shoplistinv_list_status_idx'),
        ]

    def __str__(self):
        return f"{self.inviter.username} invite {self.invitee.username} - {self.shopping_list}"


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


class CollectionFollower(models.Model):
    """Utilisateur qui suit un livre de recettes (pour l'avoir dans "Mes livres")"""
    collection = models.ForeignKey(
        Collection,
        on_delete=models.CASCADE,
        related_name='followers'
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='followed_collections'
    )
    followed_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ['collection', 'user']
        ordering = ['-followed_at']

    def __str__(self):
        return f"{self.user.username} suit {self.collection.name}"
