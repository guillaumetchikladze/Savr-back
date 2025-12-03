from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from django.db.models import Q, Count, Max
from datetime import datetime, date, timedelta
from time import perf_counter
from django.conf import settings
from django.db import connection, transaction
from django.shortcuts import get_object_or_404
from urllib.parse import urlparse
from pgvector.django import CosineDistance
from pydantic_ai.exceptions import UserError as PydanticAIUserError
import uuid
import logging
from savr_back.settings import build_s3_client, build_s3_url, build_presigned_get_url
from .services.ingredient_matcher import get_batch_embeddings
from .models import (
    Category, Recipe, Step, Ingredient, RecipeIngredient, StepIngredient,
    MealPlan, MealPlanGroup, MealPlanGroupMember, MealInvitation, CookingProgress, Timer, Post, PostPhoto, PostCookie,
    ShoppingList, ShoppingListItem, Collection, CollectionRecipe, CollectionMember,
    RecipeImportRequest
)
from accounts.models import Follow
PHOTO_TYPES = [choice[0] for choice in PostPhoto.PHOTO_TYPE_CHOICES]
RESTRICTED_PHOTO_TYPES = PostPhoto.UNIQUE_TYPES
from .serializers import (
    RecipeSerializer, RecipeDetailSerializer, RecipeCreateSerializer, RecipeLightSerializer,
    StepSerializer, IngredientSerializer, CategorySerializer,
    MealPlanSerializer, MealPlanDetailSerializer, MealInvitationSerializer,
    MealPlanListSerializer, MealPlanRangeListSerializer, MealPlanByDateSerializer,
    MealPlanGroupSerializer, MealPlanGroupMemberSerializer,
    CookingProgressSerializer, CookingProgressCreateUpdateSerializer,
    TimerSerializer, TimerCreateSerializer,
    PostSerializer, PostCreateUpdateSerializer, PostPhotoSerializer,
    ShoppingListSerializer, ShoppingListItemSerializer,
    CollectionSerializer, CollectionCreateSerializer, CollectionUpdateSerializer,
    CollectionRecipeSerializer, CollectionMemberSerializer,
    RecipeFormalizeSerializer, RecipeImportRequestSerializer
)
from .tasks import process_recipe_import


def calculate_meal_plan_servings(meal_plan, group_meal_plans=None):
    """
    Calcule le nombre total de personnes pour un meal plan.
    
    Args:
        meal_plan: Le meal plan pour lequel calculer
        group_meal_plans: Liste optionnelle de meal plans du même groupe (si groupé)
    
    Returns:
        int: Nombre total de personnes (total_servings)
    """
    # Si on a déjà _total_servings calculé, l'utiliser
    if hasattr(meal_plan, '_total_servings'):
        return meal_plan._total_servings
    
    # Si group_meal_plans est fourni, calculer pour un groupe
    if group_meal_plans and len(group_meal_plans) > 1:
        # Meal plan groupé : calculer total_servings
        total_guest_count = sum(mp.guest_count or 0 for mp in group_meal_plans)
        all_participants = []
        
        for mp in group_meal_plans:
            invitations = mp.invitations.all() if hasattr(mp, 'invitations') else []
            for inv in invitations:
                all_participants.append({
                    'user': inv.invitee,
                    'status': inv.status,
                })
        
        # Compter les participants actifs (accepted ou pending) en dédupliquant par utilisateur
        # Un utilisateur invité sur plusieurs meal plans du groupe ne compte qu'une seule fois
        active_participants_by_user = {}
        for p in all_participants:
            if p.get('status') in ['accepted', 'pending']:
                user_id = p['user'].id if hasattr(p['user'], 'id') else p['user']['id'] if isinstance(p['user'], dict) else None
                if user_id:
                    # Garder le meilleur statut (accepted > pending)
                    existing_status = active_participants_by_user.get(user_id)
                    if not existing_status or (p.get('status') == 'accepted' and existing_status != 'accepted'):
                        active_participants_by_user[user_id] = p.get('status')
        
        active_participants_count = len(active_participants_by_user)
        days_count = len(group_meal_plans)
        return days_count + active_participants_count + total_guest_count
    
    # Meal plan simple : 1 (créateur) + participants actifs + guests
    participants_count = meal_plan.invitations.filter(
        status__in=['accepted', 'pending']
    ).count() if hasattr(meal_plan, 'invitations') else 0
    guest_count = meal_plan.guest_count or 0
    return 1 + participants_count + guest_count


class RecipeViewSet(viewsets.ModelViewSet):
    """ViewSet pour les recettes"""
    queryset = Recipe.objects.all()
    permission_classes = [IsAuthenticated]
    
    def get_serializer_class(self):
        if self.action == 'create':
            return RecipeCreateSerializer
        # Utiliser RecipeLightSerializer pour les listes (pas besoin de steps/ingredients)
        if self.action in ['list', 'search', 'my_imports', 'my_favorites', 'my_recipes']:
            return RecipeLightSerializer
        # Utiliser RecipeDetailSerializer pour retrieve (léger, sans steps/ingredients)
        if self.action == 'retrieve':
            return RecipeDetailSerializer
        # Utiliser RecipeSerializer complet pour update, etc.
        return RecipeSerializer
    
    def get_queryset(self):
        """Filtrer selon is_public et user, puis appliquer les autres filtres"""
        user = self.request.user
        
        # Si l'utilisateur est connecté, voir ses recettes privées + toutes les publiques
        if user.is_authenticated:
            queryset = Recipe.objects.filter(
                Q(is_public=True) | Q(created_by=user)
            )
        else:
            # Sinon, seulement les publiques
            queryset = Recipe.objects.filter(is_public=True)
        
        meal_type = self.request.query_params.get('meal_type', None)
        difficulty = self.request.query_params.get('difficulty', None)
        search = self.request.query_params.get('search', None)
        
        if meal_type:
            queryset = queryset.filter(meal_type=meal_type)
        if difficulty:
            queryset = queryset.filter(difficulty=difficulty)
        if search:
            # Pour les listes, chercher uniquement dans le titre (plus rapide)
            if self.action in ['list', 'search']:
                queryset = queryset.filter(title__icontains=search)
            else:
                queryset = queryset.filter(
                    Q(title__icontains=search) | Q(description__icontains=search)
                )
        
        # Pour les listes, ne pas précharger steps et ingredients (inutiles)
        # Utiliser defer() pour exclure les gros champs
        if self.action in ['list', 'search']:
            queryset = queryset.defer(
                'description', 'created_at', 'updated_at', 'created_by_id'
            )
        elif self.action == 'retrieve':
            # Pour retrieve : ne pas précharger steps et ingredients (chargés via endpoints séparés)
            # Juste select_related pour created_by
            queryset = queryset.select_related('created_by')
        else:
            # Pour update, etc. : précharger les steps avec leurs ingrédients
            from django.db.models import Prefetch
            queryset = queryset.prefetch_related(
                Prefetch('steps', queryset=Step.objects.prefetch_related(
                    Prefetch('step_ingredients', queryset=StepIngredient.objects.select_related('ingredient'))
                )),
                'recipe_ingredients__ingredient',
            ).select_related('created_by')
        
        return queryset.order_by('-created_at')
    
    def list(self, request, *args, **kwargs):
        """Log détaillé pour diagnostiquer les lenteurs"""
        if settings.DEBUG:
            from django.db import reset_queries
            from time import perf_counter
            reset_queries()
            t0 = perf_counter()
        
        queryset = self.filter_queryset(self.get_queryset())
        
        if settings.DEBUG:
            t_qs_start = perf_counter()
            # Forcer l'évaluation pour mesurer le temps DB
            count = queryset.count()
            t_qs_end = perf_counter()
            db_queries = len(connection.queries)
            db_time_ms = sum(float(q.get('time', 0)) for q in connection.queries) * 1000
        
        page = self.paginate_queryset(queryset)
        if page is not None:
            serializer = self.get_serializer(page, many=True)
            data = serializer.data
            
            # Ajouter les meal plans proches si demandé
            include_nearby = request.query_params.get('include_nearby_meal_plans', 'false').lower() == 'true'
            if include_nearby:
                target_date_str = request.query_params.get('date')
                meal_time = request.query_params.get('meal_time')
                
                if target_date_str and meal_time:
                    try:
                        target_date = datetime.strptime(target_date_str, '%Y-%m-%d').date()
                        nearby_meal_plans = self._get_nearby_meal_plans(request.user, target_date, meal_time)
                        
                        # Convertir les meal plans en format "recette suggérée"
                        meal_plan_suggestions = []
                        suggested_recipe_ids = set()  # Pour filtrer les doublons
                        
                        for meal_plan in nearby_meal_plans:
                            # Calculer le nombre total de personnes
                            total_servings = 1 + (meal_plan.invitations.filter(
                                status__in=['pending', 'accepted']
                            ).count())
                            
                            # Récupérer les recettes du meal plan
                            recipes = []
                            for mpr in meal_plan.meal_plan_recipes.all().select_related('recipe'):
                                recipe_data = RecipeLightSerializer(mpr.recipe).data
                                recipe_data['ratio'] = float(mpr.ratio)
                                recipes.append(recipe_data)
                                # Ajouter l'ID de la recette à l'ensemble pour filtrage
                                if recipe_data.get('id'):
                                    suggested_recipe_ids.add(recipe_data['id'])
                            
                            # Récupérer les informations de groupe
                            membership = meal_plan.group_memberships.first()
                            group_id = None
                            grouped_dates = [meal_plan.date.isoformat()]
                            
                            if membership:
                                group = membership.group
                                group_id = group.id
                                # Récupérer toutes les dates du groupe triées par ordre
                                members = list(group.members.all())
                                members.sort(key=lambda m: (m.order, m.meal_plan.date, m.meal_plan.meal_time))
                                grouped_dates = [member.meal_plan.date.isoformat() for member in members]
                            
                            meal_plan_suggestions.append({
                                'id': f'meal_plan_{meal_plan.id}',
                                'is_meal_plan': True,
                                'meal_plan_id': meal_plan.id,
                                'title': f"Repas du {meal_plan.date.strftime('%d/%m')}",
                                'image_url': recipes[0]['image_url'] if recipes else None,
                                'recipes': recipes,
                                'original_date': meal_plan.date.strftime('%Y-%m-%d'),
                                'total_servings': total_servings,
                                'meal_time': meal_plan.meal_time,
                                'group_id': group_id,
                                'groupedDates': grouped_dates,
                            })
                        
                        # Mélanger les suggestions et limiter à 2-3
                        import random
                        random.shuffle(meal_plan_suggestions)
                        meal_plan_suggestions = meal_plan_suggestions[:3]
                        
                        # Filtrer les recettes déjà suggérées dans un meal plan
                        data = [item for item in data if item.get('id') not in suggested_recipe_ids]
                        
                        # Insérer les suggestions de meal plans au début de la liste
                        data = list(meal_plan_suggestions) + list(data)
                    except (ValueError, TypeError) as e:
                        # Si la date est invalide, ignorer les meal plans proches
                        pass
            
            if settings.DEBUG:
                t_ser_end = perf_counter()
                total_ms = (t_ser_end - t0) * 1000
                qs_ms = (t_qs_end - t_qs_start) * 1000 if 't_qs_end' in locals() else 0
                ser_ms = (t_ser_end - (t_qs_end if 't_qs_end' in locals() else t0)) * 1000
                print(f"[RecipeViewSet.list] count={count} items={len(page)} qs_ms={qs_ms:.1f} ser_ms={ser_ms:.1f} "
                      f"db_queries={db_queries} db_time_ms={db_time_ms:.1f} total_ms={total_ms:.1f}")
            
            return self.get_paginated_response(data)
        
        serializer = self.get_serializer(queryset, many=True)
        return Response(serializer.data)
    
    def _get_nearby_meal_plans(self, user, target_date, meal_time, max_days=4, limit=10):
        """Récupérer les meal plans non cuisinés des jours passés, du jour même (autre meal_time), et futurs"""
        from django.db.models import Prefetch, Q
        from .models import MealPlanRecipe
        from django.utils import timezone
        from datetime import timedelta
        
        # Calculer la plage de dates : 4 jours en arrière et 4 jours en avant
        date_start = target_date - timedelta(days=max_days)
        date_end = target_date + timedelta(days=max_days)
        
        # Récupérer les meal plans non cuisinés dans la plage
        # Inclure :
        # 1. Même meal_time ET date différente (passé ou futur)
        # 2. Autre meal_time ET même date (jour même)
        meal_plans = MealPlan.objects.filter(
            user=user,
            date__gte=date_start,
            date__lte=date_end,
            is_cooked=False  # Exclure strictement les meal plans cuisinés
        ).filter(
            (Q(meal_time=meal_time) & ~Q(date=target_date)) |  # Même meal_time, date différente
            (~Q(meal_time=meal_time) & Q(date=target_date))    # Autre meal_time, même date
        ).prefetch_related(
            Prefetch('meal_plan_recipes', queryset=MealPlanRecipe.objects.select_related('recipe').order_by('order')),
            'invitations',
            'group_memberships__group__members__meal_plan'  # Pour avoir groupedDates
        ).order_by('-date', 'meal_time')[:limit]  # Trier par date décroissante puis meal_time
        
        return meal_plans
    
    def perform_create(self, serializer):
        serializer.save(created_by=self.request.user)
    
    @action(detail=True, methods=['get'])
    def steps(self, request, pk=None):
        """
        Endpoint séparé pour charger les steps d'une recette.
        Chargé de manière lazy quand l'utilisateur clique sur "Go".
        """
        recipe = self.get_object()
        
        # Charger les steps avec leurs step_ingredients
        from django.db.models import Prefetch
        steps = Step.objects.filter(recipe=recipe).prefetch_related(
            Prefetch('step_ingredients', queryset=StepIngredient.objects.select_related('ingredient'))
        ).order_by('order')
        
        serializer = StepSerializer(steps, many=True, context={'request': request})
        return Response(serializer.data)
    
    @action(detail=True, methods=['get'])
    def ingredients(self, request, pk=None):
        """
        Endpoint séparé pour charger les ingrédients détaillés d'une recette.
        Chargé de manière lazy si nécessaire.
        """
        recipe = self.get_object()
        
        # Charger les recipe_ingredients
        ingredients = RecipeIngredient.objects.filter(recipe=recipe).select_related('ingredient')
        
        from .serializers import RecipeIngredientSerializer
        serializer = RecipeIngredientSerializer(ingredients, many=True, context={'request': request})
        return Response(serializer.data)
    
    @action(detail=False, methods=['get'])
    def my_recipes(self, request):
        """Récupérer les recettes de l'utilisateur connecté"""
        recipes = Recipe.objects.filter(created_by=request.user)
        serializer = self.get_serializer(recipes, many=True)
        return Response(serializer.data)
    
    @action(detail=True, methods=['post', 'delete'])
    def favorite(self, request, pk=None):
        """Ajouter ou retirer une recette des favoris"""
        recipe = self.get_object()
        user = request.user
        
        if request.method == 'POST':
            # Ajouter aux favoris
            if not user.favorite_recipes.filter(id=recipe.id).exists():
                user.favorite_recipes.add(recipe)
                return Response({'status': 'added', 'is_favorited': True}, status=status.HTTP_200_OK)
            return Response({'status': 'already_favorited', 'is_favorited': True}, status=status.HTTP_200_OK)
        elif request.method == 'DELETE':
            # Retirer des favoris
            user.favorite_recipes.remove(recipe)
            return Response({'status': 'removed', 'is_favorited': False}, status=status.HTTP_200_OK)
    
    @action(detail=False, methods=['get'])
    def my_imports(self, request):
        """Récupérer les recettes de l'utilisateur (créées + importées)"""
        recipes = Recipe.objects.filter(
            created_by=request.user
        ).filter(
            Q(source_type='user_created') | Q(source_type='imported')
        )
        summary_only = request.query_params.get('summary')
        if summary_only:
            count = recipes.count()
            last_recipe = recipes.order_by('-updated_at').first()
            return Response({
                'count': count,
                'last_activity': last_recipe.updated_at if last_recipe else None,
            })
        page = self.paginate_queryset(recipes)
        serializer = self.get_serializer(page if page is not None else recipes, many=True)
        if page is not None:
            return self.get_paginated_response(serializer.data)
        return Response(serializer.data)
    
    @action(detail=False, methods=['get'])
    def my_favorites(self, request):
        """Récupérer les recettes favorites de l'utilisateur"""
        recipes = request.user.favorite_recipes.all()
        summary_only = request.query_params.get('summary')
        if summary_only:
            count = recipes.count()
            last_recipe = recipes.order_by('-updated_at').first()
            return Response({
                'count': count,
                'last_activity': last_recipe.updated_at if last_recipe else None,
            })
        page = self.paginate_queryset(recipes)
        serializer = self.get_serializer(page if page is not None else recipes, many=True)
        if page is not None:
            return self.get_paginated_response(serializer.data)
        return Response(serializer.data)
    
    @action(detail=False, methods=['post'])
    def formalize(self, request):
        """
        Endpoint pour formaliser une recette brute avec l'IA et la créer en DB
        """
        import logging
        logger = logging.getLogger(__name__)
        
        process_start = perf_counter()
        logger.info(
            "[RecipeFormalize] Appel entrant user=%s payload_keys=%s",
            request.user.id,
            list(request.data.keys())
        )

        serializer = RecipeFormalizeSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        data = serializer.validated_data
        logger.info(
            "[RecipeFormalize] Requête reçue user=%s title='%s' len_ing=%d len_steps=%d",
            request.user.id,
            data.get('title'),
            len(data.get('ingredients_text', '')),
            len(data.get('instructions_text', ''))
        )

        try:
            import_request = RecipeImportRequest.objects.create(
                user=request.user,
                payload=data,
                status=RecipeImportRequest.STATUS_PENDING,
            )
            process_recipe_import.delay(str(import_request.id))

            logger.info(
                "[RecipeFormalize] Requête %s en file d'attente (%.2fs)",
                import_request.id,
                perf_counter() - process_start
            )

            response_serializer = RecipeImportRequestSerializer(import_request, context={'request': request})
            return Response(response_serializer.data, status=status.HTTP_202_ACCEPTED)
        
        except PydanticAIUserError as e:
            logger.warning("[RecipeFormalize] Erreur PydanticAI: %s", e)
            return Response(
                {'error': str(e)},
                status=status.HTTP_400_BAD_REQUEST
            )
        except ValueError as e:
            # Erreur de configuration (ex: AI_API_KEY manquant ou modèle non supporté)
            logger.warning("[RecipeFormalize] Erreur de configuration: %s", e)
            return Response(
                {'error': str(e)},
                status=status.HTTP_400_BAD_REQUEST
            )
        except (BrokenPipeError, ConnectionResetError) as e:
            logger.warning("[RecipeFormalize] Connexion interrompue par le client (broken pipe): %s", e)
            return Response(
                {'error': 'Client disconnected during processing.'},
                status=status.HTTP_400_BAD_REQUEST
            )
        except (BrokenPipeError, ConnectionResetError) as e:
            logger.warning("[RecipeFormalize] Connexion interrompue par le client (broken pipe): %s", e)
            return Response(
                {'error': 'Client disconnected during processing.'},
                status=status.HTTP_400_BAD_REQUEST
            )
        except Exception as e:
            logger.error(f"Erreur lors de la formalisation de la recette: {e}", exc_info=True)
            return Response(
                {'error': f'Erreur lors de la formalisation: {str(e)}'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

    @action(detail=False, methods=['get'], url_path='formalize/status/(?P<request_id>[0-9a-f-]+)')
    def formalize_status(self, request, request_id=None):
        import_request = get_object_or_404(
            RecipeImportRequest,
            id=request_id,
            user=request.user
        )
        serializer = RecipeImportRequestSerializer(import_request, context={'request': request})
        return Response(serializer.data)

    @action(detail=False, methods=['get'], url_path='formalize/requests')
    def formalize_requests(self, request):
        qs = RecipeImportRequest.objects.filter(user=request.user).order_by('-created_at')[:20]
        serializer = RecipeImportRequestSerializer(qs, many=True, context={'request': request})
        return Response(serializer.data)

    @action(detail=False, methods=['post'], url_path='import_from_url')
    def import_from_url(self, request):
        """
        Importe une recette depuis une URL externe (Bergamot, Marmiton, etc.)
        L'extraction et la formalisation sont faites de manière asynchrone via Celery
        """
        logger = logging.getLogger(__name__)
        url = request.data.get('url', '').strip()
        if not url:
            return Response(
                {'error': 'URL requise'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        try:
            # Créer une demande d'import avec l'URL (l'extraction sera faite par Celery)
            from .models import RecipeImportRequest
            import_request = RecipeImportRequest.objects.create(
                user=request.user,
                payload={
                    'url': url,
                    'source_type': 'imported',
                },
                status=RecipeImportRequest.STATUS_PENDING
            )
            
            # Lancer la tâche Celery qui fait l'extraction + formalisation
            from .tasks import process_recipe_import_from_url
            task = process_recipe_import_from_url.delay(str(import_request.id))
            import_request.task_id = task.id
            import_request.save(update_fields=['task_id'])
            
            logger.info(
                "[RecipeImportURL] Import depuis %s - request_id=%s",
                url,
                import_request.id
            )
            
            return Response(
                {
                    'request_id': import_request.id,
                    'status': import_request.status,
                },
                status=status.HTTP_202_ACCEPTED
            )
            
        except Exception as e:
            logger.error(f"Erreur lors de la soumission de l'import depuis URL: {e}", exc_info=True)
            return Response(
                {'error': f'Erreur lors de la soumission: {str(e)}'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

    @action(detail=False, methods=['get'], url_path='search_semantic')
    def search_semantic(self, request):
        query = request.query_params.get('q', '').strip()
        page = int(request.query_params.get('page', 1))
        page_size = min(int(request.query_params.get('page_size', 20)), 50)

        if not query:
            return Response({'error': 'Paramètre q requis.'}, status=status.HTTP_400_BAD_REQUEST)

        embeddings = get_batch_embeddings([query])
        vector = embeddings[0] if embeddings else None
        if not vector:
            return self.get_paginated_response([])

        queryset = (
            Recipe.objects.exclude(embedding__isnull=True)
            .annotate(distance=CosineDistance('embedding', vector))
            .order_by('distance')
        )
        
        # Appliquer la pagination
        paginated_queryset = self.paginate_queryset(queryset)
        if paginated_queryset is not None:
            serializer = RecipeLightSerializer(paginated_queryset, many=True, context={'request': request})
            return self.get_paginated_response(serializer.data)
        
        # Fallback si pas de pagination
        serializer = RecipeLightSerializer(queryset[:page_size], many=True, context={'request': request})
        return Response(serializer.data)
    
    @action(detail=False, methods=['post'])
    def get_recipe_image_presigned_url(self, request):
        """Générer une URL pré-signée pour uploader une image de recette directement vers S3"""
        try:
            logger = logging.getLogger(__name__)
            logger.info(
                "[RecipeImages] Demande de presigned URL user=%s payload=%s",
                request.user.id,
                request.data
            )
            s3_client = build_s3_client()
            bucket_name = settings.AWS_BUCKET
            
            if not bucket_name:
                return Response(
                    {'error': 'S3 bucket non configuré'},
                    status=status.HTTP_500_INTERNAL_SERVER_ERROR
                )
            
            # Générer un nom de fichier unique pour l'image de recette
            unique_id = str(uuid.uuid4()).replace('-', '')
            file_name = f"recipes/{request.user.id}/{unique_id}.jpg"
            
            # Générer l'URL pré-signée pour l'upload (valide 5 minutes)
            try:
                presigned_url = s3_client.generate_presigned_url(
                    'put_object',
                    Params={
                        'Bucket': bucket_name,
                        'Key': file_name,
                        'ContentType': 'image/jpeg',
                    },
                    ExpiresIn=300  # 5 minutes
                )
            except Exception as url_error:
                logger.error(f"Erreur lors de la génération de l'URL pré-signée: {url_error}")
                # Essayer sans ContentType si ça échoue
                presigned_url = s3_client.generate_presigned_url(
                    'put_object',
                    Params={
                        'Bucket': bucket_name,
                        'Key': file_name,
                    },
                    ExpiresIn=300
                )
            
            # Construire l'URL de consultation (pré-signée si possible)
            image_url = build_presigned_get_url(file_name)
            
            return Response({
                'presigned_url': presigned_url,
                'file_name': file_name,
                'image_path': file_name,  # Chemin relatif à stocker en base
                'image_url': image_url,  # URL complète pour l'affichage
            }, status=status.HTTP_200_OK)
            
        except Exception as e:
            logger.error(f"Erreur lors de la génération de l'URL pré-signée pour l'image de recette: {e}", exc_info=True)
            return Response(
                {'error': f'Erreur lors de la génération de l\'URL: {str(e)}'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )


class CategoryViewSet(viewsets.ReadOnlyModelViewSet):
    """ViewSet pour les catégories d'ingrédients"""
    queryset = Category.objects.all()
    serializer_class = CategorySerializer
    permission_classes = [IsAuthenticated]


class IngredientViewSet(viewsets.ReadOnlyModelViewSet):
    """ViewSet pour les ingrédients (lecture seule)"""
    queryset = Ingredient.objects.all()
    serializer_class = IngredientSerializer
    permission_classes = [IsAuthenticated]
    
    @action(detail=False, methods=['get'])
    def search(self, request):
        """Rechercher des ingrédients"""
        query = request.query_params.get('q', '')
        ingredients = Ingredient.objects.filter(name__icontains=query)[:10]
        serializer = self.get_serializer(ingredients, many=True)
        return Response(serializer.data)


class MealPlanViewSet(viewsets.ModelViewSet):
    """ViewSet pour les repas planifiés"""
    serializer_class = MealPlanSerializer
    permission_classes = [IsAuthenticated]
    
    def get_serializer_class(self):
        # Utiliser des serializers adaptés par action
        if self.action == 'retrieve':
            return MealPlanDetailSerializer  # Serializer léger pour retrieve
        if self.action in ['list']:
            return MealPlanRangeListSerializer
        if self.action in ['by_date']:
            return MealPlanByDateSerializer
        if self.action in ['by_week', 'shared_with_me', 'by_dates', 'bulk']:
            return MealPlanListSerializer
        return super().get_serializer_class()
    
    def get_queryset(self):
        """
        Optimiser la liste:
        - Filtrer côté DB avec date__gte/date__lte si fournis
        - Éviter les N+1 queries via select_related/prefetch_related
        """
        from django.db.models import Case, When, IntegerField
        
        qs = MealPlan.objects.filter(user=self.request.user)
        
        # Filtres de date (format YYYY-MM-DD)
        date_gte = self.request.query_params.get('date__gte')
        date_lte = self.request.query_params.get('date__lte')
        if date_gte:
            qs = qs.filter(date__gte=date_gte)
        if date_lte:
            qs = qs.filter(date__lte=date_lte)
        
        # Exclure les meal plans déjà dans une shopping list non archivée
        exclude_in_shopping_list = self.request.query_params.get('exclude_in_shopping_list')
        if exclude_in_shopping_list == 'true':
            qs = qs.exclude(shopping_lists__is_archived=False)
        
        # Exclure les meal plans déjà cuisinés
        exclude_cooked = self.request.query_params.get('exclude_cooked')
        if exclude_cooked == 'true':
            qs = qs.filter(is_cooked=False)
        
        # Autres filtres éventuels
        meal_time = self.request.query_params.get('meal_time')
        if meal_time:
            qs = qs.filter(meal_time=meal_time)
        confirmed = self.request.query_params.get('confirmed')
        if confirmed in ('true', 'false'):
            qs = qs.filter(confirmed=(confirmed == 'true'))
        group_id = self.request.query_params.get('group_id')
        if group_id:
            try:
                group_id_int = int(group_id)
                qs = qs.filter(group_memberships__group__id=group_id_int)
            except ValueError:
                qs = qs.none()
        
        # Définir l'ordre des meal_time : lunch (0) avant dinner (1)
        meal_time_order = Case(
            When(meal_time='lunch', then=0),
            When(meal_time='dinner', then=1),
            default=2,
            output_field=IntegerField(),
        )
        
        # Chargement optimisé des relations utilisées par le serializer
        from django.db.models import Prefetch
        from .models import MealPlanRecipe, StepIngredient
        
        if self.action in ['list']:
            qs = qs.select_related('recipe').prefetch_related(
                Prefetch('invitations', queryset=MealInvitation.objects.select_related('invitee')),
                Prefetch('meal_plan_recipes', queryset=MealPlanRecipe.objects.select_related('recipe').order_by('order')),
                'group_memberships__group__members__meal_plan',  # Précharger les groupes explicites
            ).order_by('date', meal_time_order)
        elif self.action in ['by_date']:
            qs = qs.select_related('user', 'recipe').prefetch_related(
                Prefetch('invitations', queryset=MealInvitation.objects.select_related('invitee')),
                Prefetch('meal_plan_recipes', queryset=MealPlanRecipe.objects.select_related('recipe').order_by('order')),
                'group_memberships__group__members__meal_plan',  # Précharger les groupes explicites
            ).order_by('date', meal_time_order)
        elif self.action in ['by_week', 'by_dates', 'bulk']:
            qs = qs.select_related('user', 'recipe').prefetch_related(
                Prefetch('meal_plan_recipes', queryset=MealPlanRecipe.objects.select_related('recipe').order_by('order')),
                'group_memberships__group__members__meal_plan',
            ).order_by('date', meal_time_order)
        else:
            # Pour retrieve : préfetch minimal (pas de steps ni recipe_ingredients détaillés)
            if self.action == 'retrieve':
                qs = qs.select_related('user', 'recipe').prefetch_related(
                    Prefetch('invitations', queryset=MealInvitation.objects.select_related('invitee')),
                    Prefetch('meal_plan_recipes', queryset=MealPlanRecipe.objects.select_related('recipe').order_by('order')),
                    'group_memberships__group__members__meal_plan',
                ).order_by('date', meal_time_order)
            else:
                # Pour update, etc. : préfetch complet si nécessaire
                qs = qs.select_related('user', 'recipe').prefetch_related(
                    Prefetch('invitations', queryset=MealInvitation.objects.select_related('invitee')),
                    Prefetch('meal_plan_recipes', queryset=MealPlanRecipe.objects.select_related('recipe').order_by('order')),
                    Prefetch('recipe__steps', queryset=Step.objects.prefetch_related(
                        Prefetch('step_ingredients', queryset=StepIngredient.objects.select_related('ingredient'))
                    )),
                    'recipe__recipe_ingredients__ingredient',
                    'group_memberships__group__members__meal_plan',
                ).order_by('date', meal_time_order)
        return qs
    
    def _create_group_for_meal_plans(self, meal_plans, user=None):
        """
        Créer un MealPlanGroup pour une liste de meal plans et y rattacher les membres
        dans l'ordre chronologique.
        """
        from .models import MealPlanGroup, MealPlanGroupMember
        
        if not meal_plans:
            return None
        
        owner = user or self.request.user
        # Trier les meal plans par date, meal_time puis id pour garantir l'ordre
        sorted_meal_plans = sorted(
            meal_plans,
            key=lambda mp: (
                mp.date,
                mp.meal_time,
                mp.id or 0,
            )
        )
        
        group = MealPlanGroup.objects.create(user=owner)
        for order_index, meal_plan in enumerate(sorted_meal_plans):
            MealPlanGroupMember.objects.create(
                group=group,
                meal_plan=meal_plan,
                order=order_index
            )
        return group
    
    def _get_meal_plans_with_prefetch(self, meal_plan_ids):
        """Charger les meal plans avec les relations nécessaires pour la sérialisation."""
        if not meal_plan_ids:
            return []
        from django.db.models import Prefetch
        from .models import MealPlanRecipe
        
        return MealPlan.objects.filter(id__in=meal_plan_ids).select_related(
            'user', 'recipe'
        ).prefetch_related(
            Prefetch('invitations', queryset=MealInvitation.objects.select_related('invitee')),
            Prefetch('meal_plan_recipes', queryset=MealPlanRecipe.objects.select_related('recipe').order_by('order')),
            'group_memberships__group__members__meal_plan',
        )
    
    def create(self, request, *args, **kwargs):
        """
        Créer un ou plusieurs meal plans. Dans tous les cas, un MealPlanGroup est créé
        et contient les meal plans nouvellement créés.
        """
        is_bulk = isinstance(request.data, list)
        
        if is_bulk:
            serializer = self.get_serializer(data=request.data, many=True)
            serializer.is_valid(raise_exception=True)
            
            with transaction.atomic():
                meal_plans = serializer.save()
                self._create_group_for_meal_plans(meal_plans, user=request.user)
            
            ordered_ids = [meal_plan.id for meal_plan in meal_plans]
            prefetched = list(self._get_meal_plans_with_prefetch(ordered_ids))
            prefetched.sort(key=lambda mp: ordered_ids.index(mp.id))
            response_serializer = self.get_serializer(prefetched, many=True)
            return Response(response_serializer.data, status=status.HTTP_201_CREATED)
        
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        
        with transaction.atomic():
            meal_plan = serializer.save()
            self._create_group_for_meal_plans([meal_plan], user=request.user)
        
        prefetched = self._get_meal_plans_with_prefetch([meal_plan.id])
        response_serializer = self.get_serializer(prefetched[0])
        headers = self.get_success_headers(response_serializer.data)
        return Response(response_serializer.data, status=status.HTTP_201_CREATED, headers=headers)
    
    def list(self, request, *args, **kwargs):
        """
        Log détaillé des temps pour diagnostiquer lenteurs:
        - construction/évaluation du queryset
        - sérialisation
        """
        if settings.DEBUG:
            from django.db import reset_queries
            reset_queries()
            t0 = perf_counter()
        
        queryset = self.get_queryset()
        
        if settings.DEBUG:
            t_qs_start = perf_counter()
            # Forcer l'évaluation pour mesurer le temps DB
            objects = list(queryset)
            t_qs_end = perf_counter()
            db_queries = len(connection.queries)
            db_time_ms = sum(float(q.get('time', 0)) for q in connection.queries) * 1000
        else:
            objects = list(queryset)
        
        # NOUVELLE LOGIQUE : Utiliser les groupes explicites (MealPlanGroup) au lieu du groupement automatique
        from collections import defaultdict
        from .models import MealInvitation, MealPlanGroupMember
        
        # Grouper les meal plans par groupe explicite
        groups_by_explicit_group = defaultdict(list)
        meal_plans_without_group = []
        
        for mp in objects:
            # Vérifier si le meal plan fait partie d'un groupe explicite
            group_membership = mp.group_memberships.first() if hasattr(mp, 'group_memberships') else None
            if group_membership:
                group = group_membership.group
                groups_by_explicit_group[group].append(mp)
            else:
                meal_plans_without_group.append(mp)
        
        # Pour chaque groupe explicite, calculer les totaux
        for group, group_meal_plans in groups_by_explicit_group.items():
            if len(group_meal_plans) > 1:
                # Plusieurs meal plans dans le groupe, calculer les totaux
                # Calculer la somme des guest_count
                total_guest_count = sum(mp.guest_count or 0 for mp in group_meal_plans)
                
                # Récupérer tous les participants de tous les meal plans du groupe
                all_participants = []
                for mp in group_meal_plans:
                    # Utiliser invitations préchargées (via prefetch_related)
                    invitations = mp.invitations.all() if hasattr(mp, 'invitations') else []
                    for inv in invitations:
                        all_participants.append({
                            'user': inv.invitee,
                            'status': inv.status,
                        })
                
                # Compter les participants actifs (accepted ou pending) en dédupliquant par utilisateur
                active_participants_by_user = {}
                for p in all_participants:
                    if p.get('status') in ['accepted', 'pending']:
                        user_id = p['user'].id if hasattr(p['user'], 'id') else p['user']['id'] if isinstance(p['user'], dict) else None
                        if user_id:
                            # Garder le meilleur statut (accepted > pending)
                            existing_status = active_participants_by_user.get(user_id)
                            if not existing_status or (p.get('status') == 'accepted' and existing_status != 'accepted'):
                                active_participants_by_user[user_id] = p.get('status')
                
                active_participants_count = len(active_participants_by_user)
                days_count = len(group_meal_plans)
                
                # Utiliser la fonction utilitaire unifiée
                total_servings = calculate_meal_plan_servings(group_meal_plans[0], group_meal_plans)
                
                # Mettre en cache sur chaque meal plan du groupe
                for mp in group_meal_plans:
                    mp._total_guest_count = total_guest_count
                    mp._total_participants = all_participants
                    mp._total_servings = total_servings
        
        # Pour les meal plans sans groupe explicite, utiliser leurs propres valeurs
        for mp in meal_plans_without_group:
            if not hasattr(mp, '_total_guest_count'):
                mp._total_guest_count = mp.guest_count or 0
                # Utiliser invitations préchargées
                invitations = mp.invitations.all() if hasattr(mp, 'invitations') else []
                mp._total_participants = [{'user': inv.invitee, 'status': inv.status} for inv in invitations]
                # Calculer total_servings pour un meal plan simple
                active_participants_count = sum(
                    1 for p in mp._total_participants
                    if p.get('status') in ['accepted', 'pending']
                )
                mp._total_servings = 1 + active_participants_count + mp._total_guest_count
        
        serializer = self.get_serializer(objects, many=True)
        
        if settings.DEBUG:
            t_ser_end = perf_counter()
            total_ms = (t_ser_end - t0) * 1000
            qs_ms = (t_qs_end - t_qs_start) * 1000 if 't_qs_end' in locals() else 0
            ser_ms = (t_ser_end - (t_qs_end if 't_qs_end' in locals() else t0)) * 1000
            print(f"[MealPlanViewSet.list] items={len(objects)} qs_ms={qs_ms:.1f} ser_ms={ser_ms:.1f} "
                  f"db_queries={db_queries} db_time_ms={db_time_ms:.1f} total_ms={total_ms:.1f}")
        
        return Response(serializer.data)
    
    @action(detail=False, methods=['get'])
    def cooked(self, request):
        """
        Retourner les meal plans cuisinés de l'utilisateur avec pagination.
        Pour le carnet de l'utilisateur.
        Un meal plan est considéré comme "cuisiné" si :
        - is_cooked=True OU
        - il a un post publié
        """
        from django.core.paginator import Paginator, EmptyPage
        from django.db.models import Prefetch, Q
        
        # Filtrer les meal plans cuisinés avec une recette
        # Inclure ceux avec is_cooked=True OU avec un post publié
        qs = MealPlan.objects.filter(
            user=request.user,
            meal_type='recipe',
            recipe__isnull=False
        ).filter(
            Q(is_cooked=True) | Q(posts__is_published=True)
        ).distinct().select_related('recipe').prefetch_related(
            Prefetch('draft_photos', queryset=PostPhoto.objects.order_by('-created_at')),
            Prefetch('posts', queryset=Post.objects.filter(is_published=True))
        ).order_by('-date', '-created_at')
        
        # Filtrer par recette si demandé
        recipe_id = request.query_params.get('recipe')
        if recipe_id:
            qs = qs.filter(recipe_id=recipe_id)
        
        # Pagination : 12 par page
        page = request.query_params.get('page', 1)
        try:
            page = int(page)
        except (TypeError, ValueError):
            page = 1
        
        paginator = Paginator(qs, 12)
        try:
            meal_plans = paginator.page(page)
        except EmptyPage:
            meal_plans = paginator.page(paginator.num_pages)
        
        # Construire la réponse avec les données nécessaires
        results = []
        for mp in meal_plans:
            # Déterminer quelle photo afficher (priorité : photos user > photo recette)
            photo_url = None
            has_published_post = mp.posts.filter(is_published=True).exists()
            
            # Chercher d'abord une photo de l'utilisateur
            user_photos = mp.draft_photos.all()
            if user_photos:
                photo_url = user_photos[0].image_url
            elif mp.recipe.image_url:
                # Sinon prendre la photo de la recette
                photo_url = mp.recipe.image_url
            
            results.append({
                'id': mp.id,
                'date': mp.date,
                'meal_time': mp.meal_time,
                'meal_time_display': mp.get_meal_time_display(),
                'recipe': {
                    'id': mp.recipe.id,
                    'title': mp.recipe.title,
                    'image_url': mp.recipe.image_url,
                },
                'photo_url': photo_url,
                'is_shared': has_published_post,
            })
        
        return Response({
            'results': results,
            'count': paginator.count,
            'num_pages': paginator.num_pages,
            'current_page': meal_plans.number,
            'has_next': meal_plans.has_next(),
            'has_previous': meal_plans.has_previous(),
        })
    
    @action(detail=False, methods=['get'])
    def by_dates(self, request):
        """
        Renvoyer les meal-plans pour plusieurs dates en un seul appel.
        Query param: dates=YYYY-MM-DD,YYYY-MM-DD
        """
        dates_param = request.query_params.get('dates', '')
        if not dates_param:
            return Response({'error': 'dates is required (comma-separated YYYY-MM-DD)'}, status=status.HTTP_400_BAD_REQUEST)
        try:
            date_strings = [d.strip() for d in dates_param.split(',') if d.strip()]
            # Validation simple du format, sans construire des objets date coûteux
            for ds in date_strings:
                if len(ds) != 10 or ds[4] != '-' or ds[7] != '-':
                    raise ValueError('invalid date format')
        except Exception:
            return Response({'error': 'Invalid dates format. Use comma-separated YYYY-MM-DD'}, status=status.HTTP_400_BAD_REQUEST)
        
        qs = MealPlan.objects.filter(user=request.user, date__in=date_strings).select_related(
            'user', 'recipe'
        ).order_by('-date', 'meal_time')
        serializer = self.get_serializer(qs, many=True)
        return Response(serializer.data)
    
    @action(detail=False, methods=['get'])
    def bulk(self, request):
        """
        Récupérer plusieurs meal-plans par IDs en un seul appel.
        Query param: ids=1,2,3
        """
        ids_param = request.query_params.get('ids', '')
        if not ids_param:
            return Response({'error': 'ids is required (comma-separated integers)'}, status=status.HTTP_400_BAD_REQUEST)
        try:
            ids = [int(x) for x in ids_param.split(',') if x.strip()]
        except ValueError:
            return Response({'error': 'ids must be integers'}, status=status.HTTP_400_BAD_REQUEST)
        
        qs = MealPlan.objects.filter(user=request.user, id__in=ids).select_related(
            'user', 'recipe'
        )
        serializer = self.get_serializer(qs, many=True)
        return Response(serializer.data)
    
    def retrieve(self, request, *args, **kwargs):
        """Logging détaillé pour le GET d'un objet"""
        if settings.DEBUG:
            from django.db import reset_queries
            reset_queries()
            t0 = perf_counter()
        
        instance = self.get_object()
        
        # DEBUG : Vérifier les invitations chargées
        if settings.DEBUG:
            import logging
            logger = logging.getLogger(__name__)
            # Vérifier si les invitations sont préchargées
            invitations_count = instance.invitations.count() if hasattr(instance, 'invitations') else 0
            logger.debug(f"[MealPlanViewSet.retrieve] Meal plan {instance.id} - invitations count: {invitations_count}")
            if hasattr(instance, 'invitations'):
                for inv in instance.invitations.all():
                    logger.debug(f"  - Invitation {inv.id}: user_id={inv.invitee_id}, status={inv.status}")
        
        # NOUVELLE LOGIQUE : Utiliser les groupes explicites (MealPlanGroup) au lieu du groupement automatique
        # Vérifier si le meal plan fait partie d'un groupe explicite
        from .models import MealPlanGroupMember, MealInvitation
        from django.db.models import Prefetch
        
        group_membership = instance.group_memberships.first() if hasattr(instance, 'group_memberships') else None
        
        if group_membership:
            # Meal plan groupé : calculer les totaux pour tout le groupe
            group = group_membership.group
            # Récupérer tous les meal plans du groupe avec leurs invitations préchargées
            group_meal_plans = list(
                MealPlan.objects.filter(
                    group_memberships__group=group
                ).prefetch_related(
                    Prefetch('invitations', queryset=MealInvitation.objects.select_related('invitee'))
                ).order_by('date', 'meal_time')
            )
            
            if len(group_meal_plans) > 1:
                # Calculer les totaux pour le groupe
                total_guest_count = sum(mp.guest_count or 0 for mp in group_meal_plans)
                all_participants = []
                
                for mp in group_meal_plans:
                    invitations = list(mp.invitations.all())
                    for inv in invitations:
                        all_participants.append({
                            'user': inv.invitee,
                            'status': inv.status,
                        })
                
                # Compter les participants actifs (accepted ou pending) en dédupliquant par utilisateur
                active_participants_by_user = {}
                for p in all_participants:
                    if p.get('status') in ['accepted', 'pending']:
                        user_id = p['user'].id if hasattr(p['user'], 'id') else p['user']['id'] if isinstance(p['user'], dict) else None
                        if user_id:
                            # Garder le meilleur statut (accepted > pending)
                            existing_status = active_participants_by_user.get(user_id)
                            if not existing_status or (p.get('status') == 'accepted' and existing_status != 'accepted'):
                                active_participants_by_user[user_id] = p.get('status')
                
                active_participants_count = len(active_participants_by_user)
                days_count = len(group_meal_plans)
                
                # Utiliser la fonction utilitaire unifiée
                total_servings = calculate_meal_plan_servings(instance, group_meal_plans)
                
                instance._total_guest_count = total_guest_count
                instance._total_participants = all_participants
                instance._total_servings = total_servings
        
        # Si pas de groupe explicite, le meal plan est simple
        # Le serializer calculera total_servings automatiquement (1 + participants + guest_count)
        
        if settings.DEBUG:
            t_qs_end = perf_counter()
            db_queries = len(connection.queries)
            db_time_ms = sum(float(q.get('time', 0)) for q in connection.queries) * 1000
        
        serializer = self.get_serializer(instance)
        
        if settings.DEBUG:
            t_ser_end = perf_counter()
            qs_ms = (t_qs_end - t0) * 1000
            ser_ms = (t_ser_end - t_qs_end) * 1000
            total_ms = (t_ser_end - t0) * 1000
            print(f"[MealPlanViewSet.retrieve] qs_ms={qs_ms:.1f} ser_ms={ser_ms:.1f} "
                  f"db_queries={db_queries} db_time_ms={db_time_ms:.1f} total_ms={total_ms:.1f}")
        
        return Response(serializer.data)
    
    @action(detail=True, methods=['get'])
    def steps(self, request, pk=None):
        """
        Endpoint séparé pour charger les steps d'une recette associée au meal plan.
        Chargé de manière lazy quand l'utilisateur clique sur "Go".
        """
        meal_plan = self.get_object()
        
        # Récupérer la recette (peut être via recipe ou recipes)
        recipe = None
        if meal_plan.recipe:
            recipe = meal_plan.recipe
        elif meal_plan.meal_plan_recipes.exists():
            # Prendre la première recette si plusieurs
            recipe = meal_plan.meal_plan_recipes.first().recipe
        
        if not recipe:
            return Response({'error': 'No recipe found for this meal plan'}, status=status.HTTP_404_NOT_FOUND)
        
        # Charger les steps avec leurs step_ingredients
        from .models import Step, StepIngredient
        from django.db.models import Prefetch
        
        steps = Step.objects.filter(recipe=recipe).prefetch_related(
            Prefetch('step_ingredients', queryset=StepIngredient.objects.select_related('ingredient'))
        ).order_by('order')
        
        from .serializers import StepSerializer
        serializer = StepSerializer(steps, many=True, context={'request': request})
        return Response(serializer.data)
    
    @action(detail=True, methods=['get'])
    def ingredients(self, request, pk=None):
        """
        Endpoint séparé pour charger les ingrédients détaillés d'une recette associée au meal plan.
        Chargé de manière lazy si nécessaire.
        """
        meal_plan = self.get_object()
        
        # Récupérer la recette
        recipe = None
        if meal_plan.recipe:
            recipe = meal_plan.recipe
        elif meal_plan.meal_plan_recipes.exists():
            recipe = meal_plan.meal_plan_recipes.first().recipe
        
        if not recipe:
            return Response({'error': 'No recipe found for this meal plan'}, status=status.HTTP_404_NOT_FOUND)
        
        # Charger les recipe_ingredients
        from .models import RecipeIngredient
        from django.db.models import Prefetch
        
        ingredients = RecipeIngredient.objects.filter(recipe=recipe).select_related('ingredient')
        
        from .serializers import RecipeIngredientSerializer
        serializer = RecipeIngredientSerializer(ingredients, many=True, context={'request': request})
        return Response(serializer.data)
    
    def get_serializer_context(self):
        context = super().get_serializer_context()
        context['request'] = self.request
        return context
    
    def perform_create(self, serializer):
        serializer.save(user=self.request.user)
    
    @action(detail=False, methods=['get'])
    def by_date(self, request):
        """Récupérer les repas planifiés pour une date spécifique"""
        date_str = request.query_params.get('date', None)
        if not date_str:
            return Response({'error': 'Date parameter is required'}, status=status.HTTP_400_BAD_REQUEST)
        
        try:
            target_date = datetime.strptime(date_str, '%Y-%m-%d').date()
        except ValueError:
            return Response({'error': 'Invalid date format. Use YYYY-MM-DD'}, status=status.HTTP_400_BAD_REQUEST)
        
        # Utiliser get_queryset() pour bénéficier des optimisations (prefetch, etc.)
        meal_plans = list(self.get_queryset().filter(date=target_date))
        
        # NOUVELLE LOGIQUE : Utiliser les groupes explicites (MealPlanGroup) au lieu du groupement automatique
        from collections import defaultdict
        from .models import MealInvitation, MealPlanGroupMember
        
        # Grouper les meal plans par groupe explicite
        groups_by_explicit_group = defaultdict(list)
        meal_plans_without_group = []
        
        for mp in meal_plans:
            # Vérifier si le meal plan fait partie d'un groupe explicite
            group_membership = mp.group_memberships.first() if hasattr(mp, 'group_memberships') else None
            if group_membership:
                group = group_membership.group
                groups_by_explicit_group[group].append(mp)
            else:
                meal_plans_without_group.append(mp)
        
        # Pour chaque groupe explicite, calculer les totaux
        for group, group_meal_plans in groups_by_explicit_group.items():
            if len(group_meal_plans) > 1:
                # Plusieurs meal plans dans le groupe, calculer les totaux
                # Calculer la somme des guest_count
                total_guest_count = sum(mp.guest_count or 0 for mp in group_meal_plans)
                
                # Récupérer tous les participants de tous les meal plans du groupe
                all_participants = []
                for mp in group_meal_plans:
                    invitations = mp.invitations.all() if hasattr(mp, 'invitations') else MealInvitation.objects.filter(meal_plan=mp).select_related('invitee')
                    for inv in invitations:
                        all_participants.append({
                            'user': inv.invitee,
                            'status': inv.status,
                        })
                
                # Compter les participants actifs (accepted ou pending) en dédupliquant par utilisateur
                active_participants_by_user = {}
                for p in all_participants:
                    if p.get('status') in ['accepted', 'pending']:
                        user_id = p['user'].id if hasattr(p['user'], 'id') else p['user']['id'] if isinstance(p['user'], dict) else None
                        if user_id:
                            # Garder le meilleur statut (accepted > pending)
                            existing_status = active_participants_by_user.get(user_id)
                            if not existing_status or (p.get('status') == 'accepted' and existing_status != 'accepted'):
                                active_participants_by_user[user_id] = p.get('status')
                
                active_participants_count = len(active_participants_by_user)
                days_count = len(group_meal_plans)
                
                # Utiliser la fonction utilitaire unifiée pour calculer total_servings
                total_servings = calculate_meal_plan_servings(group_meal_plans[0], group_meal_plans)
                
                # Log pour debug (uniquement en mode DEBUG)
                if settings.DEBUG:
                    import logging
                    logger = logging.getLogger(__name__)
                    logger.debug(f"[MealPlanViewSet.by_date] Calcul total_servings pour groupe explicite {group.id}: "
                               f"days_count={days_count}, active_participants_count={active_participants_count}, "
                               f"total_guest_count={total_guest_count}, total_servings={total_servings}")
                    logger.debug(f"[MealPlanViewSet.by_date] Participants dédupliqués: {list(active_participants_by_user.keys())}")
                    logger.debug(f"[MealPlanViewSet.by_date] Tous les participants (avant déduplication): {len(all_participants)}")
                    logger.debug(f"[MealPlanViewSet.by_date] Détail des guest_count par meal plan: {[(mp.id, mp.guest_count) for mp in group_meal_plans]}")
                    logger.debug(f"[MealPlanViewSet.by_date] Calcul détaillé: {days_count} (jours) + {active_participants_count} (participants) + {total_guest_count} (guests) = {total_servings}")
                
                # Mettre en cache sur chaque meal plan du groupe
                for mp in group_meal_plans:
                    mp._total_guest_count = total_guest_count
                    mp._total_participants = all_participants
                    mp._total_servings = total_servings
        
        # Pour les meal plans sans groupe explicite, utiliser leurs propres valeurs
        for mp in meal_plans_without_group:
            if not hasattr(mp, '_total_guest_count'):
                mp._total_guest_count = mp.guest_count or 0
                invitations = mp.invitations.all() if hasattr(mp, 'invitations') else MealInvitation.objects.filter(meal_plan=mp).select_related('invitee')
                mp._total_participants = [{'user': inv.invitee, 'status': inv.status} for inv in invitations]
                
                # Calculer total_servings pour un meal plan simple
                active_participants_count = sum(
                    1 for inv in invitations
                    if inv.status in ['accepted', 'pending']
                )
                mp._total_servings = 1 + active_participants_count + (mp.guest_count or 0)
        
        serializer = self.get_serializer(meal_plans, many=True)
        return Response(serializer.data)
    
    @action(detail=False, methods=['get'])
    def by_week(self, request):
        """Récupérer les repas planifiés pour une semaine"""
        date_str = request.query_params.get('date', None)
        if not date_str:
            date_str = date.today().isoformat()
        
        try:
            start_date = datetime.strptime(date_str, '%Y-%m-%d').date()
        except ValueError:
            return Response({'error': 'Invalid date format. Use YYYY-MM-DD'}, status=status.HTTP_400_BAD_REQUEST)
        
        from datetime import timedelta
        end_date = start_date + timedelta(days=6)
        
        meal_plans = MealPlan.objects.filter(
            user=request.user,
            date__gte=start_date,
            date__lte=end_date
        )
        serializer = self.get_serializer(meal_plans, many=True)
        return Response(serializer.data)
    
    @action(detail=True, methods=['post'])
    def confirm(self, request, pk=None):
        """Confirmer un repas planifié"""
        meal_plan = self.get_object()
        meal_plan.confirmed = True
        meal_plan.save()
        serializer = self.get_serializer(meal_plan)
        return Response(serializer.data)
    
    @action(detail=False, methods=['get'])
    def shared_with_me(self, request):
        """Récupérer les repas partagés avec l'utilisateur connecté"""
        invitations = MealInvitation.objects.filter(invitee=request.user, status='accepted').select_related('meal_plan', 'meal_plan__user', 'meal_plan__recipe')
        meal_plans = [inv.meal_plan for inv in invitations]
        serializer = self.get_serializer(meal_plans, many=True)
        return Response(serializer.data)

    @action(detail=True, methods=['get'])
    def photos(self, request, pk=None):
        """Galerie de photos associées au meal_plan (version légère)"""
        meal_plan = self.get_object()
        photos = PostPhoto.objects.filter(meal_plan=meal_plan).select_related('step')
        from .serializers import PostPhotoLightSerializer
        serializer = PostPhotoLightSerializer(photos, many=True, context={'request': request})
        return Response(serializer.data)
    
    @action(detail=True, methods=['get'], url_path='published-post')
    def published_post(self, request, pk=None):
        """Récupérer le post publié associé à ce meal_plan"""
        meal_plan = self.get_object()
        try:
            post = Post.objects.filter(meal_plan=meal_plan, is_published=True).first()
            if post:
                from .serializers import PostSerializer
                serializer = PostSerializer(post, context={'request': request})
                return Response(serializer.data)
            else:
                return Response({'exists': False}, status=status.HTTP_200_OK)
        except Exception as e:
            return Response({'error': str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    @action(detail=True, methods=['post'], url_path='publish-post')
    def publish_post(self, request, pk=None):
        """Créer et publier un post à partir d'une sélection de photos"""
        meal_plan = self.get_object()
        photo_ids = request.data.get('photo_ids', [])
        comment = request.data.get('comment', '')

        if not isinstance(photo_ids, list) or len(photo_ids) == 0:
            return Response({'error': 'photo_ids must be a non-empty list'}, status=status.HTTP_400_BAD_REQUEST)
        try:
            photo_ids = [int(pid) for pid in photo_ids]
        except (TypeError, ValueError):
            return Response({'error': 'photo_ids must contain integers'}, status=status.HTTP_400_BAD_REQUEST)

        if len(photo_ids) > 10:
            return Response({'error': 'You can select up to 10 photos'}, status=status.HTTP_400_BAD_REQUEST)

        # Récupérer les photos dans l'ordre de sélection (ordre des photo_ids)
        photos_dict = {p.id: p for p in PostPhoto.objects.filter(meal_plan=meal_plan, id__in=photo_ids)}
        if len(photos_dict) != len(photo_ids):
            return Response({'error': 'Some photos are invalid or do not belong to this meal plan'}, status=status.HTTP_400_BAD_REQUEST)
        
        # Préserver l'ordre de sélection
        photos = [photos_dict[pid] for pid in photo_ids]

        post = Post.objects.create(
            user=request.user,
            recipe=meal_plan.recipe,
            meal_plan=meal_plan,
            cooking_progress=None,
            comment=comment,
            is_published=True
        )

        # Associer les photos au post dans l'ordre de sélection et définir l'ordre
        for order_index, photo in enumerate(photos, start=1):
            photo.post = post
            photo.order = order_index
            photo.save(update_fields=['post', 'order'])

        serializer = PostSerializer(post, context={'request': request})
        return Response(serializer.data, status=status.HTTP_201_CREATED)
    
    @action(detail=True, methods=['post'], url_path='apply-to-dates')
    def apply_to_dates(self, request, pk=None):
        """Appliquer un meal plan à plusieurs dates et les regrouper automatiquement"""
        from django.db import transaction
        from django.db.models import Max
        from decimal import Decimal
        from .models import MealPlanRecipe, MealPlanGroup, MealPlanGroupMember
        
        source_meal_plan = self.get_object()
        
        # Vérifier que le meal plan source n'est pas déjà cuisiné
        if source_meal_plan.is_cooked:
            return Response(
                {'error': 'Cannot apply a meal plan that has already been cooked'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        date_keys = request.data.get('date_keys', [])
        meal_time = request.data.get('meal_time')
        
        if not date_keys or not isinstance(date_keys, list):
            return Response(
                {'error': 'date_keys must be a non-empty list'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        if not meal_time:
            return Response(
                {'error': 'meal_time is required'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        created_meal_plans = []
        
        with transaction.atomic():
            # Récupérer ou créer le groupe du meal plan source
            source_membership = source_meal_plan.group_memberships.first()
            if source_membership:
                group = source_membership.group
                max_order = group.members.aggregate(Max('order'))['order__max'] or -1
            else:
                # Créer un nouveau groupe pour le meal plan source
                group = MealPlanGroup.objects.create(user=request.user)
                MealPlanGroupMember.objects.create(group=group, meal_plan=source_meal_plan, order=0)
                max_order = 0
            
            # Récupérer les recettes et ratios du meal plan source
            source_recipes = source_meal_plan.meal_plan_recipes.all().select_related('recipe')
            recipe_data = [(mpr.recipe_id, float(mpr.ratio), mpr.order) for mpr in source_recipes]
            
            for date_key in date_keys:
                try:
                    target_date = datetime.strptime(date_key, '%Y-%m-%d').date()
                except ValueError:
                    continue
                
                # Vérifier si un meal plan existe déjà pour cette date + meal_time
                existing_meal_plan = MealPlan.objects.filter(
                    user=request.user,
                    date=target_date,
                    meal_time=meal_time
                ).first()
                
                # Calculer le nombre total de participants pour cette date
                # Somme de tous les meal plans existants pour cette date + meal_time
                total_participants = 1  # L'utilisateur lui-même
                if existing_meal_plan:
                    # Compter les invitations acceptées/pending du meal plan existant
                    total_participants += existing_meal_plan.invitations.filter(
                        status__in=['pending', 'accepted']
                    ).count()
                else:
                    # Compter toutes les invitations de tous les meal plans de cette date
                    all_meal_plans = MealPlan.objects.filter(
                        user=request.user,
                        date=target_date,
                        meal_time=meal_time
                    )
                    for mp in all_meal_plans:
                        total_participants += mp.invitations.filter(
                            status__in=['pending', 'accepted']
                        ).count()
                
                # Créer ou mettre à jour le meal plan
                if existing_meal_plan:
                    # Supprimer les anciennes recettes
                    existing_meal_plan.meal_plan_recipes.all().delete()
                    meal_plan = existing_meal_plan
                    
                    # Vérifier si le meal plan existant est déjà dans le groupe
                    existing_membership = existing_meal_plan.group_memberships.filter(group=group).first()
                    if not existing_membership:
                        max_order += 1
                        MealPlanGroupMember.objects.create(
                            group=group,
                            meal_plan=existing_meal_plan,
                            order=max_order
                        )
                else:
                    # Créer un nouveau meal plan
                    meal_plan = MealPlan.objects.create(
                        user=request.user,
                        date=target_date,
                        meal_time=meal_time,
                        meal_type=source_meal_plan.meal_type,
                        confirmed=source_meal_plan.confirmed,
                    )
                    # Ajouter au groupe existant au lieu de créer un nouveau groupe
                    max_order += 1
                    MealPlanGroupMember.objects.create(
                        group=group,
                        meal_plan=meal_plan,
                        order=max_order
                    )
                
                # Ajouter les recettes avec leurs ratios
                for recipe_id, ratio, order in recipe_data:
                    MealPlanRecipe.objects.create(
                        meal_plan=meal_plan,
                        recipe_id=recipe_id,
                        ratio=Decimal(str(ratio)),
                        order=order
                    )
                
                created_meal_plans.append(meal_plan)
        
        # Sérialiser les meal plans créés
        serializer = self.get_serializer(created_meal_plans, many=True)
        return Response(serializer.data, status=status.HTTP_201_CREATED)
    
    @action(detail=True, methods=['post'], url_path='remove-from-group')
    def remove_from_group(self, request, pk=None):
        """Retirer un meal plan de son groupe"""
        from django.db import transaction
        from .models import MealPlanGroup, MealPlanGroupMember
        
        meal_plan = self.get_object()
        membership = meal_plan.group_memberships.first()
        
        if not membership:
            return Response({'error': 'Meal plan is not in a group'}, status=status.HTTP_400_BAD_REQUEST)
        
        group = membership.group
        remaining_count = group.members.count() - 1
        
        with transaction.atomic():
            membership.delete()
            
            if remaining_count == 1:
                # Si il ne reste qu'un membre, créer un nouveau groupe pour lui
                remaining_member = group.members.first()
                if remaining_member:
                    new_group = MealPlanGroup.objects.create(user=request.user)
                    MealPlanGroupMember.objects.create(
                        group=new_group,
                        meal_plan=remaining_member.meal_plan,
                        order=0
                    )
                    # Supprimer l'ancien membership
                    remaining_member.delete()
                group.delete()
            elif remaining_count == 0:
                group.delete()
        
        serializer = self.get_serializer(meal_plan)
        return Response(serializer.data)
    
    @action(detail=True, methods=['post'])
    def invite(self, request, pk=None):
        """Inviter des utilisateurs à un repas"""
        from django.contrib.auth import get_user_model
        from django.db import transaction
        from accounts.models import Follow, Notification
        User = get_user_model()
        
        meal_plan = self.get_object()
        invitee_ids = request.data.get('invitee_ids', [])
        
        if not invitee_ids:
            return Response({'error': 'invitee_ids is required'}, status=status.HTTP_400_BAD_REQUEST)
        
        # Vérifier que les utilisateurs sont des complices
        following_ids = Follow.objects.filter(follower=request.user).values_list('following_id', flat=True)
        followers_ids = Follow.objects.filter(following=request.user).values_list('follower_id', flat=True)
        complice_ids = set(list(following_ids) + list(followers_ids))
        
        valid_invitee_ids = [user_id for user_id in invitee_ids if user_id in complice_ids]
        
        if not valid_invitee_ids:
            return Response({'error': 'No valid complices found'}, status=status.HTTP_400_BAD_REQUEST)
        
        # Précharger les utilisateurs pour éviter les requêtes N+1
        invitees = {user.id: user for user in User.objects.filter(id__in=valid_invitee_ids)}
        
        # Créer les invitations
        invitations = []
        notification_data = []  # Stocker les données de notification pour les créer après commit
        
        for invitee_id in valid_invitee_ids:
            invitee = invitees.get(invitee_id)
            if not invitee:
                continue
                
            invitation, created = MealInvitation.objects.get_or_create(
                inviter=request.user,
                invitee=invitee,
                meal_plan=meal_plan,
                defaults={'status': 'pending'}
            )
            if created:
                invitations.append(invitation)
                # Stocker les données de notification pour les créer après commit (asynchrone)
                notification_data.append({
                    'user': invitee,
                    'notification_type': 'meal_invitation',
                    'title': f"{request.user.username} vous invite à un repas",
                    'message': f"{request.user.username} vous invite à {meal_plan.get_meal_time_display()} le {meal_plan.date.strftime('%d/%m/%Y')}",
                    'related_user': request.user
                })
        
        # Créer les notifications après le commit de la transaction (asynchrone)
        # Cela rend l'endpoint plus rapide car les notifications sont créées en arrière-plan
        if notification_data:
            def create_notifications():
                for notif_data in notification_data:
                    Notification.objects.create(**notif_data)
            
            transaction.on_commit(create_notifications)
        
        # Rafraîchir le meal_plan depuis la DB pour avoir les invitations à jour
        # (nécessaire car le serializer utilise obj.invitations.all() qui peut être mis en cache)
        meal_plan.refresh_from_db()
        
        # Retourner le meal plan mis à jour avec les participants pour que le frontend ait les données à jour
        from .serializers import MealPlanSerializer
        meal_plan_serializer = MealPlanSerializer(meal_plan, context={'request': request})
        
        serializer = MealInvitationSerializer(invitations, many=True, context={'request': request})
        return Response({
            'invitations': serializer.data,
            'meal_plan': meal_plan_serializer.data  # Inclure le meal plan mis à jour
        }, status=status.HTTP_201_CREATED)


class MealInvitationViewSet(viewsets.ModelViewSet):
    """ViewSet pour les invitations à des repas"""
    serializer_class = MealInvitationSerializer
    permission_classes = [IsAuthenticated]
    
    def get_queryset(self):
        # L'utilisateur peut voir les invitations qu'il a envoyées ou reçues
        qs = MealInvitation.objects.filter(
            Q(inviter=self.request.user) | Q(invitee=self.request.user)
        ).select_related('inviter', 'invitee', 'meal_plan', 'meal_plan__recipe')
        
        # Filtrer par meal_plan si fourni dans les query params
        meal_plan_id = self.request.query_params.get('meal_plan')
        if meal_plan_id:
            try:
                qs = qs.filter(meal_plan_id=meal_plan_id)
            except ValueError:
                # Si meal_plan_id n'est pas un entier valide, ignorer le filtre
                pass
        
        return qs
    
    def get_serializer_context(self):
        context = super().get_serializer_context()
        context['request'] = self.request
        return context
    
    @action(detail=True, methods=['post'])
    def accept(self, request, pk=None):
        """Accepter une invitation à un repas"""
        from accounts.models import Notification
        
        invitation = self.get_object()
        
        if invitation.invitee != request.user:
            return Response({'error': 'You can only accept invitations sent to you'}, status=status.HTTP_403_FORBIDDEN)
        
        if invitation.status != 'pending':
            return Response({'error': 'Invitation already processed'}, status=status.HTTP_400_BAD_REQUEST)
        
        invitation.status = 'accepted'
        invitation.save()
        
        # Créer un meal plan pour l'invité (sans écraser ce qu'il a déjà)
        meal_plan = invitation.meal_plan
        user_meal_plan, created = MealPlan.objects.get_or_create(
            user=request.user,
            date=meal_plan.date,
            meal_time=meal_plan.meal_time,
            defaults={
                'meal_type': meal_plan.meal_type,
                'recipe': meal_plan.recipe,
            }
        )
        
        # Pas de shared_with: l'acceptation est portée par l'invitation (source of truth)
        
        # Créer une notification pour l'inviteur
        Notification.objects.create(
            user=invitation.inviter,
            notification_type='meal_invitation',
            title=f"{request.user.username} a accepté votre invitation",
            message=f"{request.user.username} a accepté votre invitation pour {meal_plan.get_meal_time_display()} le {meal_plan.date.strftime('%d/%m/%Y')}",
            related_user=request.user
        )
        
        serializer = self.get_serializer(invitation)
        return Response(serializer.data)
    
    @action(detail=True, methods=['post'])
    def decline(self, request, pk=None):
        """Refuser une invitation à un repas"""
        invitation = self.get_object()
        
        if invitation.invitee != request.user:
            return Response({'error': 'You can only decline invitations sent to you'}, status=status.HTTP_403_FORBIDDEN)
        
        if invitation.status != 'pending':
            return Response({'error': 'Invitation already processed'}, status=status.HTTP_400_BAD_REQUEST)
        
        invitation.status = 'declined'
        invitation.save()
        
        serializer = self.get_serializer(invitation)
        return Response(serializer.data)
    
    @action(detail=False, methods=['get'])
    def pending(self, request):
        """Récupérer les invitations en attente pour l'utilisateur connecté"""
        invitations = MealInvitation.objects.filter(
            invitee=request.user,
            status='pending'
        ).select_related('inviter', 'meal_plan', 'meal_plan__recipe')
        serializer = self.get_serializer(invitations, many=True)
        return Response(serializer.data)


class CookingProgressViewSet(viewsets.ModelViewSet):
    """ViewSet pour la progression de cuisson"""
    permission_classes = [IsAuthenticated]
    
    def get_queryset(self):
        queryset = CookingProgress.objects.filter(user=self.request.user)
        
        # Filtrer par statut si fourni
        status_filter = self.request.query_params.get('status', None)
        if status_filter:
            queryset = queryset.filter(status=status_filter)
        
        # Filtrer par recette si fourni
        recipe_id = self.request.query_params.get('recipe', None)
        if recipe_id:
            queryset = queryset.filter(recipe_id=recipe_id)
        
        # Filtrer par meal_plan si fourni
        meal_plan_id = self.request.query_params.get('meal_plan', None)
        if meal_plan_id:
            queryset = queryset.filter(meal_plan_id=meal_plan_id)
        
        return queryset.select_related('recipe', 'meal_plan').order_by('-updated_at')
    
    def get_serializer_class(self):
        if self.action in ['create', 'update', 'partial_update']:
            return CookingProgressCreateUpdateSerializer
        return CookingProgressSerializer
    
    def create(self, request, *args, **kwargs):
        """Override create pour gérer le get_or_create"""
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        
        validated_data = serializer.validated_data
        recipe = validated_data.get('recipe')
        meal_plan = validated_data.get('meal_plan')
        
        # Chercher une progression existante en cours
        existing_progress = CookingProgress.objects.filter(
            user=request.user,
            recipe=recipe,
            meal_plan=meal_plan,
            status='in_progress'
        ).first()
        
        if existing_progress:
            # Mettre à jour la progression existante au lieu d'en créer une nouvelle
            # Vérifier si on reprend après une longue pause (plus de 1 heure)
            from django.utils import timezone
            from datetime import timedelta
            
            time_since_start = timezone.now() - existing_progress.started_at
            # Si plus d'1 heure s'est écoulée, réinitialiser le temps de départ
            if time_since_start > timedelta(hours=1):
                existing_progress.started_at = timezone.now()
            
            # Mettre à jour les autres champs
            for key, value in validated_data.items():
                if key != 'started_at':  # Ne pas écraser started_at si on vient de le réinitialiser
                    setattr(existing_progress, key, value)
            existing_progress.save()
            # Utiliser le serializer complet pour retourner les données mises à jour
            response_serializer = CookingProgressSerializer(existing_progress)
            return Response(response_serializer.data, status=status.HTTP_200_OK)
        else:
            # Créer une nouvelle progression
            self.perform_create(serializer)
            headers = self.get_success_headers(serializer.data)
            return Response(serializer.data, status=status.HTTP_201_CREATED, headers=headers)
    
    def perform_create(self, serializer):
        serializer.save(user=self.request.user)
    
    @action(detail=False, methods=['get'])
    def current(self, request):
        """Récupérer la progression en cours pour une recette et un meal_plan"""
        recipe_id = request.query_params.get('recipe')
        meal_plan_id = request.query_params.get('meal_plan')
        
        if not recipe_id:
            return Response(
                {'error': 'recipe parameter is required'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        progress = CookingProgress.objects.filter(
            user=request.user,
            recipe_id=recipe_id,
            status='in_progress'
        )
        
        if meal_plan_id:
            progress = progress.filter(meal_plan_id=meal_plan_id)
        else:
            progress = progress.filter(meal_plan__isnull=True)
        
        progress = progress.first()
        
        if progress:
            serializer = self.get_serializer(progress)
            return Response(serializer.data)
        else:
            # Retourner 200 avec null au lieu de 404 pour indiquer qu'il n'y a pas de progression
            return Response(None, status=status.HTTP_200_OK)
    
    @action(detail=True, methods=['post'])
    def complete(self, request, pk=None):
        """Marquer une progression comme terminée"""
        progress = self.get_object()
        progress.complete()
        serializer = self.get_serializer(progress)
        return Response(serializer.data)


class TimerViewSet(viewsets.ModelViewSet):
    """ViewSet pour les minuteurs actifs"""
    permission_classes = [IsAuthenticated]
    
    def get_queryset(self):
        from django.utils import timezone
        from datetime import timedelta
        from django.db.models import Q
        # Inclure les timers actifs ou expirés depuis moins d'1 heure
        now = timezone.now()
        one_hour_ago = now - timedelta(hours=1)
        queryset = Timer.objects.filter(
            user=self.request.user,
            is_completed=False,
            expires_at__gte=one_hour_ago  # Expiré depuis moins d'1 heure OU pas encore expiré
        )
        return queryset.select_related('recipe', 'step', 'cooking_progress').order_by('expires_at')
    
    def get_serializer_class(self):
        if self.action in ['create']:
            return TimerCreateSerializer
        return TimerSerializer
    
    def perform_create(self, serializer):
        serializer.save(user=self.request.user)
    
    @action(detail=False, methods=['get'])
    def active(self, request):
        """Récupérer tous les minuteurs actifs de l'utilisateur"""
        timers = self.get_queryset()
        serializer = self.get_serializer(timers, many=True)
        return Response(serializer.data)
    
    @action(detail=True, methods=['post'])
    def complete(self, request, pk=None):
        """Marquer un minuteur comme terminé"""
        timer = self.get_object()
        timer.is_completed = True
        timer.save()
        serializer = self.get_serializer(timer)
        return Response(serializer.data)
    
    @action(detail=True, methods=['patch'])
    def update_remaining(self, request, pk=None):
        """Mettre à jour le temps restant du minuteur"""
        timer = self.get_object()
        remaining_seconds = request.data.get('remaining_seconds')
        if remaining_seconds is not None:
            from django.utils import timezone
            timer.remaining_seconds = remaining_seconds
            timer.expires_at = timezone.now() + timezone.timedelta(seconds=remaining_seconds)
            timer.save()
        serializer = self.get_serializer(timer)
        return Response(serializer.data)
    
    @action(detail=True, methods=['patch'])
    def add_time(self, request, pk=None):
        """Ajouter du temps au minuteur"""
        from django.utils import timezone
        timer = self.get_object()
        minutes = request.data.get('minutes', 0)
        
        if minutes > 0:
            # Calculer le temps restant actuel
            now = timezone.now()
            elapsed = (now - timer.started_at).total_seconds()
            current_remaining = max(0, (timer.duration_minutes * 60) - elapsed)
            
            # Ajouter les minutes
            new_duration_minutes = timer.duration_minutes + minutes
            new_remaining_seconds = current_remaining + (minutes * 60)
            
            # Mettre à jour
            timer.duration_minutes = new_duration_minutes
            timer.remaining_seconds = int(new_remaining_seconds)
            timer.expires_at = now + timezone.timedelta(seconds=new_remaining_seconds)
            timer.save()
        
        serializer = self.get_serializer(timer)
        return Response(serializer.data)


class PostViewSet(viewsets.ModelViewSet):
    """ViewSet pour les posts"""
    permission_classes = [IsAuthenticated]
    
    def _user_can_manage_photo(self, photo, user):
        owner = None
        if photo.meal_plan_id and photo.meal_plan:
            owner = photo.meal_plan.user
        elif photo.post_id and photo.post:
            owner = photo.post.user
        return owner == user
    
    def get_queryset(self):
        # Si on demande les posts publiés, montrer tous les posts publiés de tous les utilisateurs
        # Sinon, montrer uniquement les posts de l'utilisateur connecté
        is_published = self.request.query_params.get('is_published')
        friends_only = self.request.query_params.get('friends_only')
        
        if is_published is not None and is_published.lower() == 'true':
            queryset = Post.objects.filter(is_published=True)

            # Filtrer uniquement les posts des amis
            if friends_only and friends_only.lower() == 'true':
                user = self.request.user
                friend_ids = set()
                # Utilisateurs que je suis
                friend_ids.update(
                    Follow.objects.filter(follower=user).values_list('following_id', flat=True)
                )
                # Utilisateurs qui me suivent
                friend_ids.update(
                    Follow.objects.filter(following=user).values_list('follower_id', flat=True)
                )
                queryset = queryset.filter(user_id__in=list(friend_ids))
        else:
            queryset = Post.objects.filter(user=self.request.user)
        
        # Filtrer par recette
        recipe_id = self.request.query_params.get('recipe')
        if recipe_id:
            queryset = queryset.filter(recipe_id=recipe_id)
        
        # Filtrer par meal_plan
        meal_plan_id = self.request.query_params.get('meal_plan')
        if meal_plan_id:
            queryset = queryset.filter(meal_plan_id=meal_plan_id)
        
        # Optimisation : pour les listes, limiter les champs chargés
        if self.action == 'list':
            queryset = queryset.select_related(
                'user', 'recipe', 'meal_plan'
            ).prefetch_related(
                'photos', 'cookies'
            ).only(
                'id', 'user', 'recipe', 'meal_plan', 'comment', 'is_published',
                'created_at', 'updated_at',
                'user__id', 'user__username', 'user__email', 'user__avatar_url',
                'recipe__id', 'recipe__title', 'recipe__image_path',
                'meal_plan__id', 'meal_plan__date', 'meal_plan__meal_time'
            ).order_by('-created_at')
        else:
            queryset = queryset.select_related(
                'user', 'recipe', 'meal_plan', 'cooking_progress'
            ).prefetch_related('photos', 'cookies').order_by('-created_at')
        
        return queryset
    
    def list(self, request, *args, **kwargs):
        """Liste optimisée des posts avec pagination"""
        queryset = self.filter_queryset(self.get_queryset())
        
        # Utiliser la pagination DRF
        page = self.paginate_queryset(queryset)
        if page is not None:
            serializer = self.get_serializer(page, many=True)
            return self.get_paginated_response(serializer.data)
        
        # Fallback si pas de pagination (ne devrait pas arriver)
        serializer = self.get_serializer(queryset, many=True)
        return Response(serializer.data)
    
    def get_serializer_class(self):
        if self.action in ['create', 'update', 'partial_update']:
            return PostCreateUpdateSerializer
        return PostSerializer
    
    def perform_create(self, serializer):
        serializer.save(user=self.request.user)
    
    @action(detail=False, methods=['post'])
    def get_upload_presigned_url(self, request):
        """Générer une URL pré-signée pour uploader une photo directement vers S3"""
        meal_plan_id = request.data.get('meal_plan_id')
        photo_type = request.data.get('photo_type', 'spontaneous')
        
        if not meal_plan_id:
            return Response({'error': 'meal_plan_id is required'}, status=status.HTTP_400_BAD_REQUEST)
        
        try:
            meal_plan = MealPlan.objects.get(id=meal_plan_id, user=request.user)
        except MealPlan.DoesNotExist:
            return Response({'error': 'Meal plan not found'}, status=status.HTTP_404_NOT_FOUND)
        
        # Vérifier que le type de photo est valide
        if photo_type not in PHOTO_TYPES:
            return Response({'error': f'Invalid photo_type. Must be one of: {", ".join(PHOTO_TYPES)}'}, status=status.HTTP_400_BAD_REQUEST)
        
        # Vérifier l'unicité pour les types non-spontanés
        if photo_type in RESTRICTED_PHOTO_TYPES:
            existing_photo = PostPhoto.objects.filter(meal_plan=meal_plan, photo_type=photo_type).first()
            if existing_photo:
                return Response({'error': f'A {photo_type} photo already exists for this meal plan'}, status=status.HTTP_400_BAD_REQUEST)
        
        try:
            # Vérifier que les credentials S3 sont configurés
            aws_access_key = settings.AWS_ACCESS_KEY_ID
            aws_secret_key = settings.AWS_SECRET_ACCESS_KEY
            bucket_name = settings.AWS_BUCKET
            region = settings.AWS_S3_REGION_NAME
            
            print(f"🔍 S3 Config check:")
            print(f"  - AWS_ACCESS_KEY_ID: {'✅ Set' if aws_access_key else '❌ Missing'}")
            print(f"  - AWS_SECRET_ACCESS_KEY: {'✅ Set' if aws_secret_key else '❌ Missing'}")
            print(f"  - AWS_BUCKET: {bucket_name if bucket_name else '❌ Missing'}")
            print(f"  - AWS_S3_REGION_NAME: {region}")
            
            if not aws_access_key or not aws_secret_key or not bucket_name:
                return Response({
                    'error': 'S3 configuration is missing. Please configure AWS credentials in .env file.',
                    'details': {
                        'has_access_key': bool(aws_access_key),
                        'has_secret_key': bool(aws_secret_key),
                        'has_bucket_name': bool(bucket_name),
                    }
                }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
            
            # Nettoyer le nom du bucket
            bucket_name = bucket_name.strip()
            if not bucket_name:
                return Response({
                    'error': 'AWS_BUCKET is empty'
                }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
            
            s3_client = build_s3_client()
            
            # Générer un nom de fichier unique (sans caractères spéciaux)
            unique_id = str(uuid.uuid4()).replace('-', '')
            file_name = f"meal_plans/{meal_plan.id}/{unique_id}.jpg"
            
            print(f"🔑 Generating presigned URL for bucket: {bucket_name}, key: {file_name}")
            
            # Générer l'URL pré-signée pour l'upload (valide 5 minutes)
            # Note: ACL est déprécié dans certaines régions, on l'enlève
            try:
                presigned_url = s3_client.generate_presigned_url(
                    'put_object',
                    Params={
                        'Bucket': bucket_name,
                        'Key': file_name,
                        'ContentType': 'image/jpeg',
                    },
                    ExpiresIn=300  # 5 minutes
                )
                print(f"✅ Presigned URL generated successfully")
            except Exception as url_error:
                print(f"❌ Error generating presigned URL: {url_error}")
                # Essayer sans ContentType si ça échoue
                presigned_url = s3_client.generate_presigned_url(
                    'put_object',
                    Params={
                        'Bucket': bucket_name,
                        'Key': file_name,
                    },
                    ExpiresIn=300
                )
                print(f"✅ Presigned URL generated (without ContentType)")
            
            # Retourner le chemin relatif (image_path) au lieu de l'URL complète
            return Response({
                'presigned_url': presigned_url,
                'file_name': file_name,
                'image_path': file_name,  # Chemin relatif à stocker en base
                'meal_plan_id': meal_plan_id,
                'photo_type': photo_type
            }, status=status.HTTP_200_OK)
            
        except Exception as e:
            import traceback
            error_details = traceback.format_exc()
            print(f"❌ Error generating presigned URL: {str(e)}")
            print(f"Traceback: {error_details}")
            return Response({
                'error': f'Error generating presigned URL: {str(e)}',
                'details': error_details if settings.DEBUG else None
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
    
    @action(detail=False, methods=['post'])
    def confirm_photo_upload(self, request):
        """Confirmer qu'une photo a été uploadée et créer l'objet PostPhoto"""
        meal_plan_id = request.data.get('meal_plan_id')
        image_path = request.data.get('image_path') or request.data.get('file_name')  # Support des deux pour compatibilité
        photo_type = request.data.get('photo_type', 'spontaneous')
        step_id = request.data.get('step_id', None)
        
        if not meal_plan_id or not image_path:
            return Response({'error': 'meal_plan_id and image_path (or file_name) are required'}, status=status.HTTP_400_BAD_REQUEST)
        
        try:
            meal_plan = MealPlan.objects.get(id=meal_plan_id, user=request.user)
        except MealPlan.DoesNotExist:
            return Response({'error': 'Meal plan not found'}, status=status.HTTP_404_NOT_FOUND)
        
        # Vérifier l'unicité pour certains types
        if photo_type in RESTRICTED_PHOTO_TYPES:
            existing_photo = PostPhoto.objects.filter(meal_plan=meal_plan, photo_type=photo_type).first()
            if existing_photo:
                return Response({'error': f'A {photo_type} photo already exists for this meal plan'}, status=status.HTTP_400_BAD_REQUEST)
        
        # Créer l'objet PostPhoto avec image_path
        photo_data = {
            'meal_plan': meal_plan,
            'photo_type': photo_type,
            'image_path': image_path
        }
        if step_id:
            try:
                photo_data['step'] = Step.objects.get(id=step_id)
            except Step.DoesNotExist:
                pass
        
        post_photo = PostPhoto.objects.create(**photo_data)
        
        serializer = PostPhotoSerializer(post_photo, context={'request': request})
        return Response(serializer.data, status=status.HTTP_201_CREATED)
    
    @action(detail=False, methods=['post'])
    def get_edit_presigned_url(self, request):
        """Obtenir une URL pré-signée pour remplacer l'image d'une photo existante"""
        photo_id = request.data.get('photo_id')
        extension = request.data.get('extension', 'jpg')
        
        if not photo_id:
            return Response({'error': 'photo_id is required'}, status=status.HTTP_400_BAD_REQUEST)
        
        extension = str(extension).lower().replace('.', '')
        if extension not in ['jpg', 'jpeg', 'png', 'webp']:
            extension = 'jpg'
        content_type = 'image/jpeg' if extension in ['jpg', 'jpeg'] else f'image/{extension}'
        
        try:
            photo = PostPhoto.objects.select_related('meal_plan', 'post').get(id=photo_id)
        except PostPhoto.DoesNotExist:
            return Response({'error': 'Photo not found'}, status=status.HTTP_404_NOT_FOUND)
        
        if not self._user_can_manage_photo(photo, request.user):
            return Response({'error': 'Permission denied'}, status=status.HTTP_403_FORBIDDEN)
        
        base_path = 'photos'
        if photo.meal_plan_id:
            base_path = f"meal_plans/{photo.meal_plan_id}"
        elif photo.post_id:
            base_path = f"posts/{photo.post_id}"
        
        file_name = f"{base_path}/edits/{uuid.uuid4().hex}.{extension}"
        
        try:
            s3_client = build_s3_client()
            presigned_url = s3_client.generate_presigned_url(
                'put_object',
                Params={
                    'Bucket': settings.AWS_BUCKET,
                    'Key': file_name,
                    'ContentType': content_type,
                },
                ExpiresIn=300
            )
        except Exception as e:
            return Response({'error': f'Unable to generate presigned URL: {str(e)}'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
        
        # Retourner le chemin relatif (image_path) au lieu de l'URL complète
        return Response({
            'photo_id': photo.id,
            'presigned_url': presigned_url,
            'file_name': file_name,
            'image_path': file_name,  # Chemin relatif à stocker en base
            'content_type': content_type,
        })
    
    @action(detail=False, methods=['post'])
    def confirm_photo_edit(self, request):
        """Confirmer qu'une photo a été retouchée et créer une nouvelle photo avec l'image modifiée"""
        photo_id = request.data.get('photo_id')
        file_name = request.data.get('file_name')
        
        if not photo_id or not file_name:
            return Response({'error': 'photo_id and file_name are required'}, status=status.HTTP_400_BAD_REQUEST)
        
        try:
            original_photo = PostPhoto.objects.select_related('meal_plan', 'post').get(id=photo_id)
        except PostPhoto.DoesNotExist:
            return Response({'error': 'Photo not found'}, status=status.HTTP_404_NOT_FOUND)
        
        if not self._user_can_manage_photo(original_photo, request.user):
            return Response({'error': 'Permission denied'}, status=status.HTTP_403_FORBIDDEN)
        
        # Nettoyer le chemin (enlever le préfixe s3:/ si présent)
        new_path = file_name.replace('s3:/', '').lstrip('/')
        
        # Créer une nouvelle photo avec l'image modifiée
        # On copie toutes les propriétés de la photo originale SAUF le post (mis à null)
        # et on conserve la date de création
        new_photo = PostPhoto(
            post=None,  # La nouvelle photo n'est pas associée à un post
            meal_plan=original_photo.meal_plan,
            photo_type=original_photo.photo_type,
            image_path=new_path,
            step=original_photo.step,
            created_at=original_photo.created_at,  # Conserver la même date de création
        )
        new_photo.save()
        
        serializer = PostPhotoSerializer(new_photo, context={'request': request})
        return Response(serializer.data, status=status.HTTP_201_CREATED)
    
    @action(detail=False, methods=['post'])
    def upload_photo_to_meal_plan(self, request):
        """Uploader une photo associée à un meal_plan (avant publication)"""
        
        meal_plan_id = request.data.get('meal_plan_id')
        if not meal_plan_id:
            return Response({'error': 'meal_plan_id is required'}, status=status.HTTP_400_BAD_REQUEST)
        
        try:
            meal_plan = MealPlan.objects.get(id=meal_plan_id, user=request.user)
        except MealPlan.DoesNotExist:
            return Response({'error': 'Meal plan not found'}, status=status.HTTP_404_NOT_FOUND)
        
        # Vérifier que la photo est fournie
        if 'photo' not in request.FILES:
            return Response({'error': 'Photo is required'}, status=status.HTTP_400_BAD_REQUEST)
        
        photo_file = request.FILES['photo']
        photo_type = request.data.get('photo_type', 'spontaneous')
        step_id = request.data.get('step_id', None)
        
        # Vérifier que le type de photo est valide
        if photo_type not in PHOTO_TYPES:
            return Response({'error': f'Invalid photo_type. Must be one of: {", ".join(PHOTO_TYPES)}'}, status=status.HTTP_400_BAD_REQUEST)
        
        # Vérifier l'unicité pour certains types
        if photo_type in RESTRICTED_PHOTO_TYPES:
            existing_photo = PostPhoto.objects.filter(meal_plan=meal_plan, photo_type=photo_type).first()
            if existing_photo:
                return Response({'error': f'A {photo_type} photo already exists for this meal plan'}, status=status.HTTP_400_BAD_REQUEST)
        
        # Upload vers S3
        try:
            s3_client = build_s3_client()
            
            # Générer un nom de fichier unique
            # Nettoyer le nom du fichier pour éviter les caractères invalides
            original_filename = photo_file.name if hasattr(photo_file, 'name') and photo_file.name else 'photo.jpg'
            file_extension = original_filename.split('.')[-1].lower() if '.' in original_filename else 'jpg'
            # S'assurer que l'extension est valide
            if file_extension not in ['jpg', 'jpeg', 'png', 'gif', 'webp']:
                file_extension = 'jpg'
            
            # Créer un nom de fichier propre avec UUID (sans caractères spéciaux)
            unique_id = str(uuid.uuid4()).replace('-', '')
            file_name = f"meal_plans/{meal_plan.id}/{unique_id}.{file_extension}"
            
            # Déterminer le content type
            content_type = getattr(photo_file, 'content_type', None) or f'image/{file_extension}'
            if content_type == 'image/jpg':
                content_type = 'image/jpeg'
            elif not content_type.startswith('image/'):
                content_type = f'image/{file_extension}'
            
            # S'assurer que le fichier est en mode lecture
            if hasattr(photo_file, 'seek'):
                photo_file.seek(0)
            
            # Upload vers S3
            s3_client.upload_fileobj(
                photo_file,
                settings.AWS_BUCKET,
                file_name,
                ExtraArgs={
                    'ACL': 'public-read',
                    'ContentType': content_type
                }
            )
            
            # Créer l'objet PostPhoto avec image_path (chemin relatif)
            photo_data = {
                'meal_plan': meal_plan,
                'photo_type': photo_type,
                'image_path': file_name  # Stocker le chemin relatif
            }
            if step_id:
                try:
                    photo_data['step'] = Step.objects.get(id=step_id)
                except Step.DoesNotExist:
                    pass
            
            post_photo = PostPhoto.objects.create(**photo_data)
            
            serializer = PostPhotoSerializer(post_photo, context={'request': request})
            return Response(serializer.data, status=status.HTTP_201_CREATED)
            
        except Exception as e:
            import traceback
            error_details = traceback.format_exc()
            print(f"❌ Error uploading photo to S3 (meal_plan): {str(e)}")
            print(f"Traceback: {error_details}")
            print(f"Photo file type: {type(photo_file)}")
            if hasattr(photo_file, 'name'):
                print(f"Photo file name: {photo_file.name}")
            if hasattr(photo_file, 'content_type'):
                print(f"Photo file content_type: {photo_file.content_type}")
            return Response({
                'error': f'Error uploading photo: {str(e)}',
                'details': error_details if settings.DEBUG else None
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
    
    @action(detail=False, methods=['post'])
    def publish_from_meal_plan(self, request):
        """Créer et publier un post à partir des photos d'un meal_plan"""
        meal_plan_id = request.data.get('meal_plan_id')
        comment = request.data.get('comment', '')
        photo_ids = request.data.get('photo_ids', [])
        
        if not meal_plan_id:
            return Response({'error': 'meal_plan_id is required'}, status=status.HTTP_400_BAD_REQUEST)
        
        try:
            meal_plan = MealPlan.objects.get(id=meal_plan_id, user=request.user)
        except MealPlan.DoesNotExist:
            return Response({'error': 'Meal plan not found'}, status=status.HTTP_404_NOT_FOUND)
        
        photos_qs = PostPhoto.objects.filter(meal_plan=meal_plan)
        if isinstance(photo_ids, list) and photo_ids:
            try:
                photo_ids = [int(pid) for pid in photo_ids]
            except (TypeError, ValueError):
                return Response({'error': 'photo_ids must contain integers'}, status=status.HTTP_400_BAD_REQUEST)
            photos_qs = photos_qs.filter(id__in=photo_ids)
        
        photos = list(photos_qs.order_by('created_at'))
        
        if not photos:
            return Response({'error': 'No photos selected for this meal plan'}, status=status.HTTP_400_BAD_REQUEST)
        
        if len(photos) > 10:
            return Response({'error': 'You can select up to 10 photos per post'}, status=status.HTTP_400_BAD_REQUEST)
        
        # Créer le post
        post = Post.objects.create(
            user=request.user,
            recipe=meal_plan.recipe,
            meal_plan=meal_plan,
            cooking_progress=None,  # Peut être mis à jour plus tard
            comment=comment,
            is_published=True
        )
        
        # Associer toutes les photos au post (tout en conservant l'association au meal_plan)
        PostPhoto.objects.filter(id__in=[p.id for p in photos]).update(post=post)
        
        serializer = PostSerializer(post, context={'request': request})
        return Response(serializer.data, status=status.HTTP_201_CREATED)
    
    @action(detail=True, methods=['post'])
    def upload_photo(self, request, pk=None):
        """Uploader une photo pour un post"""
        post = self.get_object()
        
        if post.user != request.user:
            return Response({'error': 'Permission denied'}, status=status.HTTP_403_FORBIDDEN)
        
        # Vérifier que la photo est fournie
        if 'photo' not in request.FILES:
            return Response({'error': 'Photo is required'}, status=status.HTTP_400_BAD_REQUEST)
        
        photo_file = request.FILES['photo']
        photo_type = request.data.get('photo_type', 'spontaneous')
        step_id = request.data.get('step_id', None)
        
        # Vérifier que le type de photo est valide
        if photo_type not in PHOTO_TYPES:
            return Response({'error': f'Invalid photo_type. Must be one of: {", ".join(PHOTO_TYPES)}'}, status=status.HTTP_400_BAD_REQUEST)
        
        # Vérifier l'unicité pour les types non-spontanés
        if photo_type in RESTRICTED_PHOTO_TYPES:
            existing_photo = PostPhoto.objects.filter(post=post, photo_type=photo_type).first()
            if existing_photo:
                return Response({'error': f'A {photo_type} photo already exists for this post'}, status=status.HTTP_400_BAD_REQUEST)
        
        # Upload vers S3
        try:
            s3_client = build_s3_client()
            
            # Générer un nom de fichier unique
            file_extension = photo_file.name.split('.')[-1] if '.' in photo_file.name else 'jpg'
            file_name = f"posts/{post.id}/{uuid.uuid4()}.{file_extension}"
            
            # Upload vers S3
            s3_client.upload_fileobj(
                photo_file,
                settings.AWS_BUCKET,
                file_name,
                ExtraArgs={'ACL': 'public-read', 'ContentType': photo_file.content_type}
            )
            
            # Créer l'objet PostPhoto avec image_path (chemin relatif)
            photo_data = {
                'post': post,
                'photo_type': photo_type,
                'image_path': file_name  # Stocker le chemin relatif
            }
            if step_id:
                try:
                    photo_data['step'] = Step.objects.get(id=step_id)
                except Step.DoesNotExist:
                    pass
            
            post_photo = PostPhoto.objects.create(**photo_data)
            
            serializer = PostPhotoSerializer(post_photo, context={'request': request})
            return Response(serializer.data, status=status.HTTP_201_CREATED)
            
        except Exception as e:
            return Response({'error': f'Error uploading photo: {str(e)}'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
    
    @action(detail=True, methods=['post'])
    def publish(self, request, pk=None):
        """Publier un post (nécessite les 3 photos)"""
        post = self.get_object()
        
        if post.user != request.user:
            return Response({'error': 'Permission denied'}, status=status.HTTP_403_FORBIDDEN)
        
        if post.photos.count() == 0:
            return Response(
                {'error': 'At least one photo is required before publishing'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        post.is_published = True
        post.save()
        
        serializer = self.get_serializer(post)
        return Response(serializer.data)
    
    @action(detail=True, methods=['delete'])
    def delete_photo(self, request, pk=None):
        """Supprimer une photo d'un post"""
        post = self.get_object()
        
        if post.user != request.user:
            return Response({'error': 'Permission denied'}, status=status.HTTP_403_FORBIDDEN)
        
        photo_id = request.data.get('photo_id')
        if not photo_id:
            return Response({'error': 'photo_id is required'}, status=status.HTTP_400_BAD_REQUEST)
        
        try:
            photo = PostPhoto.objects.get(id=photo_id, post=post)
            
            # Supprimer de S3
            try:
                s3_client = build_s3_client()
                # Utiliser directement image_path (nettoyer le préfixe s3:/ si présent)
                file_path = photo.image_path.replace('s3:/', '').lstrip('/') if photo.image_path else None
                if file_path:
                    s3_client.delete_object(Bucket=settings.AWS_BUCKET, Key=file_path)
            except Exception as e:
                print(f"Error deleting from S3: {str(e)}")
            
            # Supprimer de la base de données
            photo.delete()
            
            return Response({'message': 'Photo deleted successfully'}, status=status.HTTP_200_OK)
            
        except PostPhoto.DoesNotExist:
            return Response({'error': 'Photo not found'}, status=status.HTTP_404_NOT_FOUND)
    
    @action(detail=True, methods=['post'])
    def send_cookie(self, request, pk=None):
        """Envoyer un cookie (like) à un post"""
        post = self.get_object()
        user = request.user
        
        # Vérifier si l'utilisateur a déjà donné un cookie
        cookie, created = PostCookie.objects.get_or_create(
            user=user,
            post=post
        )
        
        if created:
            serializer = PostSerializer(post, context={'request': request})
            return Response({
                'message': 'Cookie sent successfully',
                'post': serializer.data
            }, status=status.HTTP_201_CREATED)
        else:
            # Cookie déjà existant
            serializer = PostSerializer(post, context={'request': request})
            return Response({
                'message': 'Cookie already sent',
                'post': serializer.data
            }, status=status.HTTP_200_OK)
    
    @action(detail=True, methods=['delete'])
    def remove_cookie(self, request, pk=None):
        """Retirer un cookie (like) d'un post"""
        post = self.get_object()
        user = request.user
        
        try:
            cookie = PostCookie.objects.get(user=user, post=post)
            cookie.delete()
            serializer = PostSerializer(post, context={'request': request})
            return Response({
                'message': 'Cookie removed successfully',
                'post': serializer.data
            }, status=status.HTTP_200_OK)
        except PostCookie.DoesNotExist:
            return Response({'error': 'Cookie not found'}, status=status.HTTP_404_NOT_FOUND)


class ShoppingListViewSet(viewsets.ModelViewSet):
    """ViewSet pour les listes de courses"""
    serializer_class = ShoppingListSerializer
    permission_classes = [IsAuthenticated]
    
    def get_queryset(self):
        """Filtrer par utilisateur"""
        from django.db.models import Prefetch
        
        queryset = ShoppingList.objects.filter(user=self.request.user)
        
        # Filtrer par liste active
        is_active = self.request.query_params.get('is_active')
        if is_active == 'true':
            queryset = queryset.filter(is_active=True)
        
        # Filtrer les listes archivées (par défaut, ne pas les afficher)
        include_archived = self.request.query_params.get('include_archived')
        if include_archived != 'true':
            queryset = queryset.filter(is_archived=False)
        
        # Optimisation : précharger toutes les relations nécessaires
        from .models import MealPlanRecipe
        return queryset.prefetch_related(
            Prefetch('meal_plans', queryset=MealPlan.objects.select_related('recipe').prefetch_related(
                Prefetch('meal_plan_recipes', queryset=MealPlanRecipe.objects.select_related('recipe'))
            )),
            Prefetch('items', queryset=ShoppingListItem.objects.select_related('ingredient__category'))
        ).order_by('-created_at')
    
    @action(detail=True, methods=['post'])
    def archive(self, request, pk=None):
        """Archiver ou désarchiver une liste"""
        shopping_list = self.get_object()
        shopping_list.is_archived = not shopping_list.is_archived
        shopping_list.save()
        serializer = self.get_serializer(shopping_list)
        return Response(serializer.data, status=status.HTTP_200_OK)
    
    @action(detail=True, methods=['post'])
    def generate_items(self, request, pk=None):
        """Générer automatiquement les items d'ingrédients à partir des meal plans"""
        from django.db.models import Prefetch
        from .models import MealPlanRecipe
        
        shopping_list = ShoppingList.objects.prefetch_related(
            Prefetch('meal_plans', queryset=MealPlan.objects.select_related(
                'recipe'
            ).prefetch_related(
                'invitations',
                Prefetch('meal_plan_recipes', queryset=MealPlanRecipe.objects.select_related(
                    'recipe'
                ).prefetch_related(
                    Prefetch('recipe__recipe_ingredients', queryset=RecipeIngredient.objects.select_related('ingredient__category'))
                )),
                Prefetch('recipe__recipe_ingredients', queryset=RecipeIngredient.objects.select_related('ingredient__category'))
            ))
        ).get(id=pk, user=request.user)
        
        # Agréger les ingrédients de tous les meal plans
        ingredients_map = {}
        
        for meal_plan in shopping_list.meal_plans.all():
            # Utiliser la fonction helper pour calculer total_servings (gère les meal plans groupés)
            servings = calculate_meal_plan_servings(meal_plan)
            
            # Parcourir les MealPlanRecipe (nouvelles relations multi-recettes)
            meal_plan_recipes = meal_plan.meal_plan_recipes.all()
            
            if meal_plan_recipes.exists():
                # Utiliser les MealPlanRecipe
                for meal_plan_recipe in meal_plan_recipes:
                    recipe = meal_plan_recipe.recipe
                    base_servings = recipe.servings or 1
                    servings_ratio = servings / base_servings
                    # Ratio effectif = ratio de la recette * ratio des servings
                    effective_ratio = float(meal_plan_recipe.ratio) * servings_ratio
                    
                    # Parcourir les ingrédients de la recette
                    for recipe_ingredient in recipe.recipe_ingredients.all():
                        ingredient_id = recipe_ingredient.ingredient.id
                        quantity = float(recipe_ingredient.quantity) * effective_ratio
                        unit = recipe_ingredient.unit
                        
                        if ingredient_id not in ingredients_map:
                            ingredients_map[ingredient_id] = {
                                'quantity': 0,
                                'unit': unit,
                            }
                        
                        if ingredients_map[ingredient_id]['unit'] == unit:
                            ingredients_map[ingredient_id]['quantity'] += quantity
                        else:
                            ingredients_map[ingredient_id]['quantity'] += quantity
            elif meal_plan.recipe:
                # Fallback pour compatibilité : utiliser meal_plan.recipe (ancienne API)
                base_servings = meal_plan.recipe.servings or 1
                ratio = servings / base_servings
                
                # Parcourir les ingrédients de la recette (déjà préchargés)
                for recipe_ingredient in meal_plan.recipe.recipe_ingredients.all():
                    ingredient_id = recipe_ingredient.ingredient.id
                    quantity = float(recipe_ingredient.quantity) * ratio
                    unit = recipe_ingredient.unit
                    
                    if ingredient_id not in ingredients_map:
                        ingredients_map[ingredient_id] = {
                            'quantity': 0,
                            'unit': unit,
                        }
                    
                    if ingredients_map[ingredient_id]['unit'] == unit:
                        ingredients_map[ingredient_id]['quantity'] += quantity
                    else:
                        ingredients_map[ingredient_id]['quantity'] += quantity
        
        # Créer ou mettre à jour les items en bulk
        created_items = []
        items_to_create = []
        items_to_update = []
        
        existing_items = {
            item.ingredient_id: item 
            for item in shopping_list.items.select_related('ingredient__category').all()
        }
        
        for ingredient_id, data in ingredients_map.items():
            if ingredient_id in existing_items:
                # Item existe déjà, on ne fait rien (on garde le statut et pantry_quantity)
                item = existing_items[ingredient_id]
            else:
                # Créer un nouvel item
                items_to_create.append(
                    ShoppingListItem(
                        shopping_list=shopping_list,
                        ingredient_id=ingredient_id,
                        status='to_buy',
                        pantry_quantity=0,
                    )
                )
        
        # Créer en bulk
        if items_to_create:
            ShoppingListItem.objects.bulk_create(items_to_create)
        
        # Recharger les items créés pour les serializer
        all_items = shopping_list.items.select_related('ingredient__category').all()
        created_items = [ShoppingListItemSerializer(item).data for item in all_items]
        
        return Response(created_items, status=status.HTTP_200_OK)


class ShoppingListItemViewSet(viewsets.ModelViewSet):
    """ViewSet pour les items de liste de courses"""
    serializer_class = ShoppingListItemSerializer
    permission_classes = [IsAuthenticated]
    
    def get_queryset(self):
        """Filtrer par shopping list de l'utilisateur"""
        from django.db.models import Prefetch
        
        shopping_list_id = self.request.query_params.get('shopping_list_id')
        
        if shopping_list_id:
            # Vérifier que la shopping list appartient à l'utilisateur
            try:
                shopping_list = ShoppingList.objects.get(
                    id=shopping_list_id,
                    user=self.request.user
                )
                queryset = ShoppingListItem.objects.filter(shopping_list=shopping_list)
            except ShoppingList.DoesNotExist:
                return ShoppingListItem.objects.none()
        else:
            # Retourner les items de toutes les listes de l'utilisateur
            user_lists = ShoppingList.objects.filter(user=self.request.user)
            queryset = ShoppingListItem.objects.filter(shopping_list__in=user_lists)
        
        # Filtres optionnels
        ingredient_id = self.request.query_params.get('ingredient_id')
        status = self.request.query_params.get('status')
        
        if ingredient_id:
            queryset = queryset.filter(ingredient_id=ingredient_id)
        if status:
            queryset = queryset.filter(status=status)
        
        # Optimisation : précharger toutes les relations nécessaires
        return queryset.select_related(
            'ingredient__category',
            'shopping_list'
        ).order_by('-updated_at')
    
    @action(detail=False, methods=['get'])
    def with_quantities(self, request):
        """Retourne les ingrédients avec leurs quantités calculées depuis les meal plans"""
        from django.db.models import Prefetch
        
        shopping_list_id = request.query_params.get('shopping_list_id')
        
        if not shopping_list_id:
            return Response({'error': 'shopping_list_id required'}, status=status.HTTP_400_BAD_REQUEST)
        
        try:
            # Optimisation maximale : précharger toutes les relations en une seule requête
            from .models import MealPlanRecipe
            shopping_list = ShoppingList.objects.prefetch_related(
                Prefetch(
                    'meal_plans',
                    queryset=MealPlan.objects.select_related('recipe').prefetch_related(
                        'invitations',
                        Prefetch(
                            'meal_plan_recipes',
                            queryset=MealPlanRecipe.objects.select_related('recipe').prefetch_related(
                                Prefetch(
                                    'recipe__recipe_ingredients',
                                    queryset=RecipeIngredient.objects.select_related('ingredient__category')
                                )
                            )
                        ),
                        Prefetch(
                            'recipe__recipe_ingredients',
                            queryset=RecipeIngredient.objects.select_related('ingredient__category')
                        )
                    )
                ),
                Prefetch(
                    'items',
                    queryset=ShoppingListItem.objects.select_related('ingredient__category')
                )
            ).get(id=shopping_list_id, user=request.user)
        except ShoppingList.DoesNotExist:
            return Response({'error': 'Shopping list not found'}, status=status.HTTP_404_NOT_FOUND)
        
        # Agréger les ingrédients depuis les meal plans
        ingredients_map = {}
        
        for meal_plan in shopping_list.meal_plans.all():
            # Utiliser la fonction helper pour calculer total_servings (gère les meal plans groupés)
            servings = calculate_meal_plan_servings(meal_plan)
            
            # Parcourir les MealPlanRecipe (nouvelles relations multi-recettes)
            meal_plan_recipes = meal_plan.meal_plan_recipes.all()
            
            if meal_plan_recipes.exists():
                # Utiliser les MealPlanRecipe
                for meal_plan_recipe in meal_plan_recipes:
                    recipe = meal_plan_recipe.recipe
                    base_servings = recipe.servings or 1
                    servings_ratio = servings / base_servings
                    # Ratio effectif = ratio de la recette * ratio des servings
                    effective_ratio = float(meal_plan_recipe.ratio) * servings_ratio
                    
                    # Parcourir les ingrédients de la recette
                    for recipe_ingredient in recipe.recipe_ingredients.all():
                        ingredient = recipe_ingredient.ingredient
                        ingredient_id = ingredient.id
                        quantity = float(recipe_ingredient.quantity) * effective_ratio
                        unit = recipe_ingredient.unit
                        
                        if ingredient_id not in ingredients_map:
                            ingredients_map[ingredient_id] = {
                                'id': ingredient_id,
                                'name': ingredient.name,
                                'quantity': 0,
                                'unit': unit,
                                'category': {
                                    'id': ingredient.category.id if ingredient.category else None,
                                    'name': ingredient.category.name if ingredient.category else 'Autres',
                                } if ingredient.category else {'id': None, 'name': 'Autres'},
                                'item': None,
                            }
                        
                        if ingredients_map[ingredient_id]['unit'] == unit:
                            ingredients_map[ingredient_id]['quantity'] += quantity
                        else:
                            ingredients_map[ingredient_id]['quantity'] += quantity
            elif meal_plan.recipe:
                # Fallback pour compatibilité : utiliser meal_plan.recipe (ancienne API)
                base_servings = meal_plan.recipe.servings or 1
                ratio = servings / base_servings
                
                # Parcourir les ingrédients de la recette
                for recipe_ingredient in meal_plan.recipe.recipe_ingredients.all():
                    ingredient = recipe_ingredient.ingredient
                    ingredient_id = ingredient.id
                    quantity = float(recipe_ingredient.quantity) * ratio
                    unit = recipe_ingredient.unit
                    
                    if ingredient_id not in ingredients_map:
                        ingredients_map[ingredient_id] = {
                            'id': ingredient_id,
                            'name': ingredient.name,
                            'quantity': 0,
                            'unit': unit,
                            'category': {
                                'id': ingredient.category.id if ingredient.category else None,
                                'name': ingredient.category.name if ingredient.category else 'Autres',
                            } if ingredient.category else {'id': None, 'name': 'Autres'},
                            'item': None,
                        }
                    
                    if ingredients_map[ingredient_id]['unit'] == unit:
                        ingredients_map[ingredient_id]['quantity'] += quantity
                    else:
                        ingredients_map[ingredient_id]['quantity'] += quantity
                
                if ingredients_map[ingredient_id]['unit'] == unit:
                    ingredients_map[ingredient_id]['quantity'] += quantity
                else:
                    ingredients_map[ingredient_id]['quantity'] += quantity
        
        # Enrichir avec les items existants (statut, pantry_quantity)
        for item in shopping_list.items.all():
            ingredient_id = item.ingredient.id
            if ingredient_id in ingredients_map:
                ingredients_map[ingredient_id]['item'] = {
                    'id': item.id,
                    'status': item.status,
                    'pantry_quantity': float(item.pantry_quantity) if item.pantry_quantity else 0,
                    'pantry_unit': item.pantry_unit or '',
                }
        
        # Convertir en liste
        ingredients_list = list(ingredients_map.values())
        
        return Response(ingredients_list, status=status.HTTP_200_OK)


class CollectionViewSet(viewsets.ModelViewSet):
    """ViewSet pour les collections de recettes"""
    permission_classes = [IsAuthenticated]
    
    def get_serializer_class(self):
        if self.action == 'create':
            return CollectionCreateSerializer
        if self.action in ['update', 'partial_update']:
            return CollectionUpdateSerializer
        if self.action == 'my_collections':
            from .serializers import CollectionListSerializer
            return CollectionListSerializer
        return CollectionSerializer
    
    def get_queryset(self):
        """Filtrer les collections : publiques + celles de l'utilisateur"""
        user = self.request.user
        queryset = Collection.objects.filter(
            Q(is_public=True) | Q(owner=user) | Q(members__user=user)
        ).distinct()
        
        # Précharger les relations
        queryset = queryset.select_related('owner').prefetch_related(
            'collection_recipes__recipe',
            'members__user'
        )
        
        queryset = queryset.annotate(
            last_activity=Max('collection_recipes__added_at')
        )
        
        return queryset.order_by('-last_activity', '-updated_at')
    
    def perform_create(self, serializer):
        """Créer une collection avec l'utilisateur connecté comme owner"""
        # Le serializer.create() gère déjà la création du CollectionMember
        serializer.save(owner=self.request.user)
    
    def perform_update(self, serializer):
        """Vérifier que l'utilisateur est le propriétaire"""
        collection = self.get_object()
        if collection.owner != self.request.user:
            return Response(
                {'error': 'Vous n\'êtes pas le propriétaire de cette collection'},
                status=status.HTTP_403_FORBIDDEN
            )
        serializer.save()
    
    def perform_destroy(self, instance):
        """Vérifier que l'utilisateur est le propriétaire"""
        if instance.owner != self.request.user:
            return Response(
                {'error': 'Vous n\'êtes pas le propriétaire de cette collection'},
                status=status.HTTP_403_FORBIDDEN
            )
        instance.delete()
    
    @action(detail=True, methods=['post'])
    def add_recipe(self, request, pk=None):
        """Ajouter une recette à la collection"""
        collection = self.get_object()
        user = request.user
        
        # Vérifier les permissions
        is_owner = collection.owner == user
        is_member = collection.members.filter(user=user).exists()
        is_public = collection.is_public
        
        if not (is_owner or (is_member and collection.is_collaborative) or is_public):
            return Response(
                {'error': 'Vous n\'avez pas la permission d\'ajouter des recettes à cette collection'},
                status=status.HTTP_403_FORBIDDEN
            )
        
        recipe_id = request.data.get('recipe_id')
        if not recipe_id:
            return Response(
                {'error': 'recipe_id est requis'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        try:
            recipe = Recipe.objects.get(id=recipe_id)
        except Recipe.DoesNotExist:
            return Response(
                {'error': 'Recette non trouvée'},
                status=status.HTTP_404_NOT_FOUND
            )
        
        # Vérifier si la recette est déjà dans la collection
        if CollectionRecipe.objects.filter(collection=collection, recipe=recipe).exists():
            return Response(
                {'error': 'Cette recette est déjà dans la collection'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        # Ajouter la recette
        CollectionRecipe.objects.create(
            collection=collection,
            recipe=recipe,
            added_by=user
        )
        
        serializer = self.get_serializer(collection)
        return Response(serializer.data, status=status.HTTP_200_OK)
    
    @action(detail=True, methods=['post'])
    def remove_recipe(self, request, pk=None):
        """Retirer une recette de la collection"""
        collection = self.get_object()
        user = request.user
        
        # Vérifier les permissions (owner ou collaborateur)
        is_owner = collection.owner == user
        is_collaborator = collection.members.filter(user=user, role='collaborator').exists()
        
        if not (is_owner or is_collaborator):
            return Response(
                {'error': 'Vous n\'avez pas la permission de retirer des recettes de cette collection'},
                status=status.HTTP_403_FORBIDDEN
            )
        
        recipe_id = request.data.get('recipe_id')
        if not recipe_id:
            return Response(
                {'error': 'recipe_id est requis'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        try:
            collection_recipe = CollectionRecipe.objects.get(
                collection=collection,
                recipe_id=recipe_id
            )
            collection_recipe.delete()
        except CollectionRecipe.DoesNotExist:
            return Response(
                {'error': 'Cette recette n\'est pas dans la collection'},
                status=status.HTTP_404_NOT_FOUND
            )
        
        serializer = self.get_serializer(collection)
        return Response(serializer.data, status=status.HTTP_200_OK)
    
    @action(detail=True, methods=['post'])
    def add_member(self, request, pk=None):
        """Ajouter un membre à la collection (si collaborative)"""
        collection = self.get_object()
        user = request.user
        
        # Vérifier que l'utilisateur est le propriétaire
        if collection.owner != user:
            return Response(
                {'error': 'Seul le propriétaire peut ajouter des membres'},
                status=status.HTTP_403_FORBIDDEN
            )
        
        # Vérifier que la collection est collaborative
        if not collection.is_collaborative:
            return Response(
                {'error': 'Cette collection n\'est pas collaborative'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        user_id = request.data.get('user_id')
        if not user_id:
            return Response(
                {'error': 'user_id est requis'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        try:
            member_user = User.objects.get(id=user_id)
        except User.DoesNotExist:
            return Response(
                {'error': 'Utilisateur non trouvé'},
                status=status.HTTP_404_NOT_FOUND
            )
        
        # Vérifier si l'utilisateur est déjà membre
        if CollectionMember.objects.filter(collection=collection, user=member_user).exists():
            return Response(
                {'error': 'Cet utilisateur est déjà membre de la collection'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        # Ajouter le membre
        CollectionMember.objects.create(
            collection=collection,
            user=member_user,
            role='collaborator'
        )
        
        serializer = self.get_serializer(collection)
        return Response(serializer.data, status=status.HTTP_200_OK)
    
    @action(detail=True, methods=['post'])
    def remove_member(self, request, pk=None):
        """Retirer un membre de la collection"""
        collection = self.get_object()
        user = request.user
        
        # Vérifier que l'utilisateur est le propriétaire
        if collection.owner != user:
            return Response(
                {'error': 'Seul le propriétaire peut retirer des membres'},
                status=status.HTTP_403_FORBIDDEN
            )
        
        user_id = request.data.get('user_id')
        if not user_id:
            return Response(
                {'error': 'user_id est requis'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        try:
            member = CollectionMember.objects.get(
                collection=collection,
                user_id=user_id
            )
            # Ne pas permettre de retirer le propriétaire
            if member.role == 'owner':
                return Response(
                    {'error': 'Impossible de retirer le propriétaire'},
                    status=status.HTTP_400_BAD_REQUEST
                )
            member.delete()
        except CollectionMember.DoesNotExist:
            return Response(
                {'error': 'Cet utilisateur n\'est pas membre de la collection'},
                status=status.HTTP_404_NOT_FOUND
            )
        
        serializer = self.get_serializer(collection)
        return Response(serializer.data, status=status.HTTP_200_OK)
    
    @action(detail=False, methods=['get'])
    def my_collections(self, request):
        """Récupérer les collections de l'utilisateur connecté"""
        try:
            collections = Collection.objects.filter(
                owner=request.user
            ).select_related('owner').prefetch_related(
                'collection_recipes__recipe'
            ).annotate(
                total_recipes=Count('collection_recipes', distinct=True),
                last_activity=Max('collection_recipes__added_at')
            ).order_by('-last_activity', '-updated_at')
            
            serializer = self.get_serializer(collections, many=True)
            return Response(serializer.data)
        except Exception as e:
            import traceback
            error_trace = traceback.format_exc()
            print(f"Error in my_collections: {str(e)}")
            print(error_trace)
            return Response(
                {'error': f'Erreur serveur: {str(e)}', 'details': error_trace if settings.DEBUG else None},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

    @action(detail=True, methods=['get'])
    def recipes(self, request, pk=None):
        """Lister les recettes d'une collection (paginé)"""
        collection = self.get_object()
        queryset = CollectionRecipe.objects.filter(
            collection=collection
        ).select_related('recipe').order_by('-added_at')
        
        page = self.paginate_queryset(queryset)
        serializer = CollectionRecipeSerializer(
            page if page is not None else queryset,
            many=True,
            context={'request': request}
        )
        if page is not None:
            return self.get_paginated_response(serializer.data)
        return Response(serializer.data)

    @action(detail=True, methods=['get'])
    def suggestions(self, request, pk=None):
        """Proposer des recettes similaires à ajouter dans la collection"""
        collection = self.get_object()
        existing_ids = collection.collection_recipes.values_list('recipe_id', flat=True)
        
        queryset = Recipe.objects.filter(
            Q(is_public=True) | Q(created_by=request.user)
        ).exclude(
            id__in=existing_ids
        ).order_by('-created_at')
        
        page = self.paginate_queryset(queryset)
        serializer = RecipeLightSerializer(
            page if page is not None else queryset,
            many=True,
            context={'request': request}
        )
        if page is not None:
            return self.get_paginated_response(serializer.data)
        return Response(serializer.data)


class MealPlanGroupViewSet(viewsets.ModelViewSet):
    """ViewSet pour gérer les groupes de meal plans"""
    serializer_class = MealPlanGroupSerializer
    permission_classes = [IsAuthenticated]
    
    def get_queryset(self):
        """Retourner uniquement les groupes de l'utilisateur connecté"""
        return MealPlanGroup.objects.filter(
            user=self.request.user
        ).prefetch_related(
            'members__meal_plan'
        ).order_by('-created_at')
    
    def perform_create(self, serializer):
        """Créer un groupe avec l'utilisateur connecté comme propriétaire"""
        serializer.save(user=self.request.user)
    
    def perform_update(self, serializer):
        """Vérifier que l'utilisateur est le propriétaire du groupe"""
        group = self.get_object()
        if group.user != self.request.user:
            return Response(
                {'error': 'Vous n\'êtes pas le propriétaire de ce groupe'},
                status=status.HTTP_403_FORBIDDEN
            )
        serializer.save()
    
    def perform_destroy(self, instance):
        """Vérifier que l'utilisateur est le propriétaire du groupe"""
        if instance.user != self.request.user:
            return Response(
                {'error': 'Vous n\'êtes pas le propriétaire de ce groupe'},
                status=status.HTTP_403_FORBIDDEN
            )
        instance.delete()
    
    @action(detail=True, methods=['post'])
    def add_meal_plan(self, request, pk=None):
        """Ajouter un meal plan au groupe"""
        group = self.get_object()
        if group.user != request.user:
            return Response(
                {'error': 'Vous n\'êtes pas le propriétaire de ce groupe'},
                status=status.HTTP_403_FORBIDDEN
            )
        
        meal_plan_id = request.data.get('meal_plan_id')
        if not meal_plan_id:
            return Response(
                {'error': 'meal_plan_id est requis'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        try:
            meal_plan = MealPlan.objects.get(id=meal_plan_id, user=request.user)
        except MealPlan.DoesNotExist:
            return Response(
                {'error': 'Meal plan non trouvé'},
                status=status.HTTP_404_NOT_FOUND
            )
        
        # Vérifier si le meal plan n'est pas déjà dans le groupe
        if MealPlanGroupMember.objects.filter(group=group, meal_plan=meal_plan).exists():
            return Response(
                {'error': 'Ce meal plan est déjà dans le groupe'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        # Déterminer l'ordre (dernier + 1)
        max_order = group.members.aggregate(Max('order'))['order__max'] or -1
        
        MealPlanGroupMember.objects.create(
            group=group,
            meal_plan=meal_plan,
            order=max_order + 1
        )
        
        serializer = self.get_serializer(group)
        return Response(serializer.data)
    
    @action(detail=True, methods=['post'])
    def remove_meal_plan(self, request, pk=None):
        """Retirer un meal plan du groupe"""
        group = self.get_object()
        if group.user != request.user:
            return Response(
                {'error': 'Vous n\'êtes pas le propriétaire de ce groupe'},
                status=status.HTTP_403_FORBIDDEN
            )
        
        meal_plan_id = request.data.get('meal_plan_id')
        if not meal_plan_id:
            return Response(
                {'error': 'meal_plan_id est requis'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        try:
            member = MealPlanGroupMember.objects.get(
                group=group,
                meal_plan_id=meal_plan_id
            )
            member.delete()
        except MealPlanGroupMember.DoesNotExist:
            return Response(
                {'error': 'Ce meal plan n\'est pas dans le groupe'},
                status=status.HTTP_404_NOT_FOUND
            )
        
        serializer = self.get_serializer(group)
        return Response(serializer.data)
