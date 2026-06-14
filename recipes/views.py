from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from django.db.models import Q, Count, Max, Case, When, IntegerField, Prefetch, Exists, OuterRef, F
from decimal import Decimal, ROUND_HALF_UP
from datetime import datetime, date, timedelta, time
from time import perf_counter
from django.conf import settings
from django.db import connection, transaction
from django.shortcuts import get_object_or_404
from django.utils import timezone
from urllib.parse import urlparse
from pydantic_ai.exceptions import UserError as PydanticAIUserError
from typing import Optional
import re
import uuid
import logging
import traceback
import random
from channels.layers import get_channel_layer
from asgiref.sync import async_to_sync
from savr_back.settings import build_s3_client, build_s3_url, build_presigned_get_url
from .services.ingredient_matcher import get_batch_embeddings
from .services.recipe_search import fuzzy_recipe_queryset, hybrid_recipe_queryset
from .services.recipe_search_index import schedule_recipe_search_reindex
from .services.meal_plan_service import (
    create_composer_slot,
    relink_composer_photos_to_meal_plan,
    update_composer_slot,
    add_recipes_to_meal_plan,
    infer_meal_time_from_hour,
    complete_recipe_batch_workflow,
    complete_meal_plan_batches_for_publish,
)
from .models import (
    Category, Recipe, Step, Ingredient, RecipeIngredient, StepIngredient,
    MealPlan, MealInvitation, CookingProgress, Timer, Post, PostPhoto, PostCookie, PostComment, PostReport,
    ShoppingList, ShoppingListMember, ShoppingListBatch, ShoppingListLoyaltyCard, ShoppingListItem, ShoppingListItemQuantity,
    ShoppingListInvitation,
    Collection, CollectionRecipe, CollectionMember, CollectionFollower,
    RecipeImportRequest, RecipeBatch, MealPlanRecipeBatch, PostCommentLike,
)
from accounts.models import Follow, Notification, LoyaltyCard, PushDevice
from django.contrib.auth import get_user_model
PHOTO_TYPES = [choice[0] for choice in PostPhoto.PHOTO_TYPE_CHOICES]
RESTRICTED_PHOTO_TYPES = PostPhoto.UNIQUE_TYPES
from .serializers import (
    RecipeSerializer, RecipeDetailSerializer, RecipeCreateSerializer, RecipeLightSerializer,
    StepSerializer, IngredientSerializer, CategorySerializer,
    MealPlanSerializer, MealPlanDetailSerializer, MealInvitationSerializer, MealInvitationListSerializer,
    MealPlanListSerializer, MealPlanRangeListSerializer, MealPlanByDateSerializer,
    MealPlanMinimalListSerializer,
    CookingProgressSerializer, CookingProgressCreateUpdateSerializer,
    TimerSerializer, TimerCreateSerializer,
    PostSerializer, PostCreateUpdateSerializer, PostPhotoSerializer,
    PostCommentSerializer, PostCommentCreateSerializer,
    ShoppingListSerializer, ShoppingListItemSerializer,
    CollectionSerializer, CollectionCreateSerializer, CollectionUpdateSerializer,
    CollectionRecipeSerializer, CollectionMemberSerializer,
    RecipeFormalizeSerializer, RecipeGenerateFromIdeaSerializer,
    RecipeImportRequestSerializer, RecipeImportRequestLightSerializer,
    RecipeBatchLightSerializer,
    UserLightSerializer
)
from accounts.serializers import LoyaltyCardSerializer
from accounts.services.expo_push import send_expo_push_notifications
from .tasks import process_recipe_import
from accounts.tasks import send_timer_almost_finished_push, send_meal_time_photo_reminder_push
from savr_back.celery import app as celery_app
from .utils import get_accessible_meal_plan_filter, get_invited_recipe_filter, shopping_list_item_quantity_is_stale, meal_plan_slot_api_fields
from .dietary_filters import apply_dietary_exclusion, conflict_reasons_by_recipe_id


logger = logging.getLogger(__name__)


class RecipeBatchViewSet(viewsets.ReadOnlyModelViewSet):
    """Lister / récupérer les batches (préparation partagée)"""
    serializer_class = RecipeBatchLightSerializer
    permission_classes = [IsAuthenticated]
    
    def get_queryset(self):
        user = self.request.user
        
        # Filtrer les RecipeBatch liés aux MealPlan accessibles par l'utilisateur
        # (propriétaire ou invité accepté)
        accessible_meal_plan_filter = get_accessible_meal_plan_filter(user)
        qs = RecipeBatch.objects.filter(
            meal_plan_recipe_batches__meal_plan__in=MealPlan.objects.filter(
                accessible_meal_plan_filter
            )
        ).select_related('recipe').order_by('-created_at')
        
        date_gte = self.request.query_params.get('date__gte')
        date_lte = self.request.query_params.get('date__lte')
        exclude_cooked = self.request.query_params.get('exclude_cooked') == 'true'

        if exclude_cooked:
            in_date_range = Q()
            if date_gte:
                in_date_range &= Q(
                    meal_plan_recipe_batches__meal_plan__date__gte=date_gte
                )
            if date_lte:
                in_date_range &= Q(
                    meal_plan_recipe_batches__meal_plan__date__lte=date_lte
                )
            if date_gte or date_lte:
                qs = qs.filter(in_date_range & Q(is_cooked=False))
            else:
                qs = qs.filter(is_cooked=False)
        else:
            if date_gte:
                qs = qs.filter(
                    meal_plan_recipe_batches__meal_plan__date__gte=date_gte
                )
            if date_lte:
                qs = qs.filter(
                    meal_plan_recipe_batches__meal_plan__date__lte=date_lte
                )
        
        # Annoter groupedDates et total_servings_batch
        qs = qs.select_related('created_by').prefetch_related(
            Prefetch(
                'meal_plan_recipe_batches',
                queryset=MealPlanRecipeBatch.objects.select_related('meal_plan')
            )
        ).distinct()
        return qs
    
    def list(self, request, *args, **kwargs):
        # post-process for groupedDates & total_servings_batch
        queryset = self.filter_queryset(self.get_queryset())
        data = []
        # Calculer le filtre d'accessibilité une seule fois pour tous les batches
        accessible_meal_plan_filter = get_accessible_meal_plan_filter(request.user)
        from .models import MealInvitation
        for batch in queryset:
            # Filtrer les meal plans accessibles par l'utilisateur pour ce batch
            meal_plans_accessible = MealPlan.objects.filter(
                meal_plan_recipe_batches__recipe_batch=batch
            ).filter(accessible_meal_plan_filter).distinct()
            
            # Pour total_servings_batch, calculer avec TOUS les meal plans du batch
            # (même ceux auxquels l'utilisateur n'est pas invité)
            all_meal_plans = MealPlan.objects.filter(
                meal_plan_recipe_batches__recipe_batch=batch
            ).prefetch_related(
                Prefetch('invitations', queryset=MealInvitation.objects.select_related('invitee', 'inviter'))
            ).distinct()
            
            grouped_dates = sorted({mp.date.isoformat() for mp in meal_plans_accessible})
            servings_breakdown_all = []
            servings_breakdown_accessible = []
            for mp in all_meal_plans:
                _, breakdown = calculate_meal_plan_servings(mp, include_breakdown=True, recipe_batch_id=batch.id)
                servings_breakdown_all.extend(breakdown)
            for mp in meal_plans_accessible:
                _, breakdown = calculate_meal_plan_servings(mp, include_breakdown=True, recipe_batch_id=batch.id)
                servings_breakdown_accessible.extend(breakdown)
            total_servings_all = sum(e.get('portions', 0) for e in servings_breakdown_all)
            total_servings_accessible = sum(e.get('portions', 0) for e in servings_breakdown_accessible)
            
            # Préparer un mapping des invitations acceptées pour l'utilisateur courant,
            # indexées par meal_plan_id pour enrichir les métadonnées côté front.
            accepted_invitations_for_user = MealInvitation.objects.filter(
                meal_plan__in=meal_plans_accessible,
                invitee=request.user,
                status='accepted',
            ).select_related('inviter')
            invitations_by_meal_plan_id = {
                inv.meal_plan_id: inv for inv in accepted_invitations_for_user
            }
            
            meals = []
            meal_plan_ids = []
            batch_is_cooked = bool(getattr(batch, 'is_cooked', False))
            # Mais ne retourner que les meal plans accessibles dans meals et meal_plan_ids
            for mp in meal_plans_accessible:
                meal_plan_ids.append(mp.id)
                invitation = invitations_by_meal_plan_id.get(mp.id)
                is_guest = invitation is not None
                inviter_name = None
                if invitation and invitation.inviter:
                    inviter_name = (
                        getattr(invitation.inviter, 'username', None)
                        or getattr(invitation.inviter, 'email', None)
                    )
                meals.append({
                    'id': mp.id,
                    'date': mp.date,
                    'meal_time': mp.meal_time,
                    **meal_plan_slot_api_fields(mp),
                    'is_cooked': batch_is_cooked,
                    'is_guest': is_guest,
                    'inviter_name': inviter_name,
                })
            payload = RecipeBatchLightSerializer(batch, context={'request': request}).data
            payload['groupedDates'] = grouped_dates
            payload['total_servings_batch'] = total_servings_all  # Total de TOUS les meal plans
            payload['servings_breakdown'] = servings_breakdown_all
            payload['total_servings_batch_accessible'] = total_servings_accessible
            payload['servings_breakdown_accessible'] = servings_breakdown_accessible
            payload['meal_plan_ids'] = meal_plan_ids  # Seulement les accessibles
            payload['meals'] = meals  # Seulement les accessibles
            payload['is_cooked'] = batch_is_cooked
            data.append(payload)
        return Response(data)
    
    def retrieve(self, request, *args, **kwargs):
        batch = self.get_object()
        # Filtrer les meal plans accessibles par l'utilisateur pour ce batch
        accessible_meal_plan_filter = get_accessible_meal_plan_filter(request.user)
        meal_plans_accessible = MealPlan.objects.filter(
            meal_plan_recipe_batches__recipe_batch=batch
        ).filter(accessible_meal_plan_filter).distinct()
        
        # Pour total_servings_batch, calculer avec TOUS les meal plans du batch
        # (même ceux auxquels l'utilisateur n'est pas invité)
        # Précharger les invitations pour pouvoir calculer correctement les servings
        from .models import MealInvitation
        all_meal_plans = MealPlan.objects.filter(
            meal_plan_recipe_batches__recipe_batch=batch
        ).prefetch_related(
            Prefetch('invitations', queryset=MealInvitation.objects.select_related('invitee'))
        ).distinct()
        
        grouped_dates = sorted({mp.date.isoformat() for mp in meal_plans_accessible})
        servings_breakdown = []
        accessible_servings_breakdown = []
        all_meal_plans_list = list(all_meal_plans)
        for mp in all_meal_plans_list:
            _, breakdown = calculate_meal_plan_servings(mp, include_breakdown=True, recipe_batch_id=batch.id)
            servings_breakdown.extend(breakdown)
        for mp in meal_plans_accessible:
            _, breakdown = calculate_meal_plan_servings(mp, include_breakdown=True, recipe_batch_id=batch.id)
            accessible_servings_breakdown.extend(breakdown)
        total_servings = sum(e.get('portions', 0) for e in servings_breakdown)
        accessible_total_servings = sum(e.get('portions', 0) for e in accessible_servings_breakdown)
        
        meals = []
        meal_plan_ids = []
        batch_is_cooked = bool(getattr(batch, 'is_cooked', False))
        # Mais ne retourner que les meal plans accessibles dans meals et meal_plan_ids
        for mp in meal_plans_accessible:
            meal_plan_ids.append(mp.id)
            meals.append({
                'id': mp.id,
                'date': mp.date,
                'meal_time': mp.meal_time,
                **meal_plan_slot_api_fields(mp),
                'is_cooked': batch_is_cooked,
            })
        serializer = RecipeBatchLightSerializer(batch, context={'request': request})
        payload = serializer.data
        payload['groupedDates'] = grouped_dates
        payload['total_servings_batch'] = total_servings  # Total de TOUS les meal plans
        payload['servings_breakdown'] = servings_breakdown
        payload['total_servings_batch_accessible'] = accessible_total_servings
        payload['servings_breakdown_accessible'] = accessible_servings_breakdown
        payload['meal_plan_ids'] = meal_plan_ids  # Seulement les accessibles
        payload['meals'] = meals  # Seulement les accessibles
        payload['is_cooked'] = batch_is_cooked
        return Response(payload)

    @action(detail=False, methods=['get'], url_path='finalize-candidates')
    def finalize_candidates(self, request):
        """
        Batches « à finaliser » pour CTA (léger).

        Contraintes produit:
        - batch lié à au moins un meal plan accessible (même base query)
        - created_by = utilisateur courant
        - is_cooked = True
        - pas de post publié
        - récent (< 24 h via updated_at)
        """
        accessible_meal_plan_filter = get_accessible_meal_plan_filter(request.user)
        recent_cutoff = timezone.now() - timedelta(hours=24)

        qs = (
            RecipeBatch.objects.filter(
                meal_plan_recipe_batches__meal_plan__in=MealPlan.objects.filter(
                    accessible_meal_plan_filter
                ),
                created_by=request.user,
                is_cooked=True,
                updated_at__gte=recent_cutoff,
            )
            .annotate(
                has_published_post=Exists(
                    Post.objects.filter(recipe_batch_id=OuterRef('pk'), is_published=True)
                )
            )
            .filter(has_published_post=False)
            .select_related('recipe')
            .distinct()
            .order_by('-updated_at')
        )

        # Réponse légère (pour CTA). Pas besoin des groupedDates/servings ici.
        results = []
        for b in qs[:20]:
            r = getattr(b, 'recipe', None)
            results.append(
                {
                    'id': b.id,
                    'recipe': {
                        'id': getattr(r, 'id', None),
                        'title': getattr(r, 'title', None),
                        'image_url': getattr(r, 'image_url', None),
                    }
                    if r
                    else None,
                    'updated_at': b.updated_at,
                }
            )
        return Response({'results': results}, status=status.HTTP_200_OK)
    
    @action(detail=True, methods=['get'])
    def steps(self, request, pk=None):
        batch = self.get_object()
        from .models import Step, StepIngredient
        # Les steps sont liés à la recette, pas au batch directement
        steps = Step.objects.filter(recipe=batch.recipe).prefetch_related(
            Prefetch('step_ingredients', queryset=StepIngredient.objects.select_related('ingredient'))
        ).order_by('order')
        
        from .serializers import StepSerializer
        serializer = StepSerializer(steps, many=True, context={'request': request})
        return Response(serializer.data)
    
    @action(detail=True, methods=['post'], url_path='complete_cooking')
    def complete_cooking(self, request, pk=None):
        """
        Marquer la cuisson d'un batch comme terminée pour l'utilisateur courant.
        - Si une CookingProgress 'in_progress' existe pour ce batch, on l'utilise.
        - Sinon, on crée une CookingProgress minimale puis on la complète.
        """
        batch = self.get_object()
        user = request.user

        # Vérifier que l'utilisateur a accès au meal plan du batch
        accessible_meal_plan_filter = get_accessible_meal_plan_filter(user)
        has_access = MealPlan.objects.filter(
            meal_plan_recipe_batches__recipe_batch=batch
        ).filter(accessible_meal_plan_filter).exists()
        if not has_access:
            return Response({'detail': "Accès refusé à ce batch."}, status=status.HTTP_403_FORBIDDEN)

        # 1) Chercher une progression existante en cours pour ce batch
        progress = CookingProgress.objects.filter(
            user=user,
            recipe_batch=batch,
            status='in_progress',
        ).first()

        # 2) Si pas de progression, en créer une minimale
        if not progress:
            recipe = batch.recipe
            total_steps = recipe.steps.count()
            last_index = max(total_steps - 1, 0)
            progress = CookingProgress.objects.create(
                user=user,
                recipe_batch=batch,
                current_step_index=last_index,
                status='in_progress',
            )

        # 3) Compléter la progression (met aussi batch.is_cooked = True)
        progress.complete()
        serializer = CookingProgressSerializer(progress, context={'request': request})

        batch_pk = batch.id
        user_pk = user.id

        def schedule_meal_photo_push_reminder():
            """
            Nouveau workflow: 1 notif par meal plan.

            Stratégie simple (v1):
            - on choisit un meal plan "propriétaire" de l'utilisateur qui contient ce batch
              et qui est le plus proche (date croissante), puis on planifie la push pour ce meal plan.
            - la tâche elle-même applique les règles produit (skip si batch partagé / post déjà existant).
            """
            try:
                today = timezone.localdate()
                meal_plan = (
                    MealPlan.objects.filter(
                        user_id=user_pk,
                        meal_plan_recipe_batches__recipe_batch_id=batch_pk,
                        date__gte=today,
                    )
                    .distinct()
                    .order_by('date', 'meal_time')
                    .first()
                )
                if not meal_plan:
                    # fallback: dernier meal plan (même si passé) pour ne pas perdre l'info
                    meal_plan = (
                        MealPlan.objects.filter(
                            user_id=user_pk,
                            meal_plan_recipe_batches__recipe_batch_id=batch_pk,
                        )
                        .distinct()
                        .order_by('-date', '-id')
                        .first()
                    )
                if not meal_plan:
                    return

                mp_pk = meal_plan.id
                mp = MealPlan.objects.only('meal_time_photo_reminder_task_id').get(pk=mp_pk)
                old_tid = (mp.meal_time_photo_reminder_task_id or '').strip()
                if old_tid:
                    try:
                        celery_app.control.revoke(old_tid, terminate=False)
                    except Exception as exc:
                        logger.warning(
                            'meal_photo_reminder: revoke failed task_id=%s meal_plan=%s: %s',
                            old_tid,
                            mp_pk,
                            exc,
                        )

                delay = int(getattr(settings, 'MEAL_TIME_PHOTO_REMINDER_DELAY_SECONDS', 10800))
                eta = timezone.now() + timedelta(seconds=max(30, delay))
                result = send_meal_time_photo_reminder_push.apply_async(
                    args=[user_pk, mp_pk],
                    eta=eta,
                )
                new_tid = getattr(result, 'id', None)
                if new_tid:
                    MealPlan.objects.filter(pk=mp_pk).update(
                        meal_time_photo_reminder_task_id=str(new_tid)
                    )
            except Exception as exc:
                logger.exception(
                    'meal_photo_reminder: schedule failed batch=%s user=%s: %s',
                    batch_pk,
                    user_pk,
                    exc,
                )

        transaction.on_commit(schedule_meal_photo_push_reminder)

        return Response(serializer.data, status=status.HTTP_200_OK)
    
    @action(detail=True, methods=['post'], url_path='mark_shopping_done')
    def mark_shopping_done(self, request, pk=None):
        """Marque les courses comme terminées pour ce batch (ex. « J'ai déjà fait les courses »)."""
        batch = self.get_object()
        batch.shopping_done = True
        batch.save(update_fields=['shopping_done', 'updated_at'])
        serializer = RecipeBatchLightSerializer(batch, context={'request': request})
        return Response(serializer.data)
    
    @action(detail=True, methods=['get'])
    def ingredients(self, request, pk=None):
        batch = self.get_object()
        ingredients = RecipeIngredient.objects.filter(recipe=batch.recipe).select_related('ingredient')
        from .serializers import RecipeIngredientSerializer
        serializer = RecipeIngredientSerializer(ingredients, many=True, context={'request': request})
        return Response(serializer.data)
    
    @action(detail=True, methods=['get'])
    def photos(self, request, pk=None):
        """Galerie de photos associées au batch"""
        batch = self.get_object()
        step_order = request.query_params.get('step_order')
        photo_type = request.query_params.get('photo_type')
        
        photos = PostPhoto.objects.filter(recipe_batch=batch).select_related('step').order_by('-created_at')
        
        # Filtrer par photo_type si fourni
        if photo_type:
            photos = photos.filter(photo_type=photo_type)
        
        # Filtrer par step_order si fourni (seulement pour during_cooking)
        if step_order:
            try:
                step_order_int = int(step_order)
                photos = photos.filter(step__order=step_order_int)
            except ValueError:
                pass
        
        from .serializers import PostPhotoLightSerializer
        serializer = PostPhotoLightSerializer(photos, many=True, context={'request': request})
        return Response(serializer.data)
    
    @action(detail=True, methods=['post'], url_path='create-photo-draft')
    def create_photo_draft(self, request, pk=None):
        """Créer un PostPhoto draft pour une étape photo"""
        batch = self.get_object()
        step_order = request.data.get('step_order')
        step_id = request.data.get('step_id')
        photo_type = request.data.get('photo_type', 'during_cooking')
        
        # Pour after_cooking, step_order/step_id sont optionnels
        if photo_type != 'after_cooking' and not step_order and not step_id:
            return Response({'error': 'step_order or step_id is required'}, status=status.HTTP_400_BAD_REQUEST)
        
        # Vérifier que step_order est dans photo_step_orders (seulement pour during_cooking)
        if step_order is not None and photo_type == 'during_cooking' and step_order not in batch.photo_step_orders:
            return Response({'error': 'Invalid step_order'}, status=status.HTTP_400_BAD_REQUEST)
        
        # Trouver la step correspondante (optionnel pour after_cooking)
        step = None
        if step_id:
            step = batch.recipe.steps.filter(id=step_id).first()
        elif step_order:
            step = batch.recipe.steps.filter(order=step_order).first()
        else:
            step = None
            
        # Pour during_cooking, step est requis
        if photo_type == 'during_cooking' and not step:
            return Response({'error': 'Step not found'}, status=status.HTTP_404_NOT_FOUND)
        
        # Vérifier que step_order correspond bien (seulement si step existe)
        if step and step_order and step.order != step_order:
            return Response({'error': 'Step order mismatch'}, status=status.HTTP_400_BAD_REQUEST)
        
        # Créer le draft
        draft = PostPhoto.objects.create(
            recipe_batch=batch,
            photo_type=photo_type,
            step=step,  # Peut être None pour after_cooking
            is_draft=True,
            image_path='',
            uploaded_by=request.user,
        )
        
        from .serializers import PostPhotoSerializer
        serializer = PostPhotoSerializer(draft, context={'request': request})
        return Response(serializer.data, status=status.HTTP_201_CREATED)
    
    @action(detail=True, methods=['delete'], url_path='delete-photo')
    def delete_photo(self, request, pk=None):
        """Supprimer une photo d'un batch"""
        logger = logging.getLogger(__name__)
        batch = self.get_object()
        # Accepter photo_id depuis query params (recommandé pour DELETE) ou body (compatibilité)
        photo_id = request.query_params.get('photo_id') or request.data.get('photo_id')
        
        if not photo_id:
            return Response({'error': 'photo_id is required'}, status=status.HTTP_400_BAD_REQUEST)
        
        try:
            photo = PostPhoto.objects.get(id=photo_id, recipe_batch=batch)

            if photo.post_id:
                return Response(
                    {
                        'error': 'Cette photo fait partie d\'un post. Supprime d\'abord le post.',
                    },
                    status=status.HTTP_400_BAD_REQUEST,
                )
            if photo.uploaded_by_id and photo.uploaded_by_id != request.user.id:
                return Response(
                    {'error': 'Seul l\'utilisateur qui a ajouté cette photo peut la supprimer.'},
                    status=status.HTTP_403_FORBIDDEN,
                )

            # Sauvegarder le chemin de l'image avant suppression
            image_path_to_delete = photo.image_path
            
            # Supprimer de S3 AVANT de supprimer de la DB
            s3_deleted = False
            if image_path_to_delete:
                try:
                    s3_client = build_s3_client()
                    # Nettoyer le chemin (enlever s3:/ et les slashes en début)
                    file_path = image_path_to_delete.replace('s3:/', '').lstrip('/')
                    
                    if file_path and settings.AWS_BUCKET:
                        logger.info(f"Deleting S3 object: Bucket={settings.AWS_BUCKET}, Key={file_path}")
                        s3_client.delete_object(Bucket=settings.AWS_BUCKET, Key=file_path)
                        s3_deleted = True
                        logger.info(f"Successfully deleted S3 object: {file_path}")
                    else:
                        logger.warning(f"Cannot delete S3 object: file_path={file_path}, bucket={settings.AWS_BUCKET}")
                except Exception as e:
                    logger.error(f"Error deleting from S3: {str(e)}")
                    logger.error(traceback.format_exc())
                    # On continue quand même pour supprimer de la DB
            
            # Supprimer de la base de données
            photo.delete()
            
            return Response({
                'message': 'Photo deleted successfully',
                's3_deleted': s3_deleted
            }, status=status.HTTP_200_OK)
            
        except PostPhoto.DoesNotExist:
            return Response({'error': 'Photo not found'}, status=status.HTTP_404_NOT_FOUND)
        except Exception as e:
            logger.error(f"Unexpected error deleting photo: {str(e)}")
            logger.error(traceback.format_exc())
            return Response({'error': f'Error deleting photo: {str(e)}'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
    
    @action(detail=True, methods=['post'], url_path='publish-post')
    def publish_post(self, request, pk=None):
        """Créer et publier un post à partir d'une sélection de photos"""
        batch = self.get_object()
        photo_ids = request.data.get('photo_ids', [])
        comment = request.data.get('comment', '')

        if photo_ids is None:
            photo_ids = []
        if not isinstance(photo_ids, list):
            return Response({'error': 'photo_ids must be a list'}, status=status.HTTP_400_BAD_REQUEST)

        photos = []
        if photo_ids:
            try:
                photo_ids = [int(pid) for pid in photo_ids]
            except (TypeError, ValueError):
                return Response({'error': 'photo_ids must contain integers'}, status=status.HTTP_400_BAD_REQUEST)

            if len(photo_ids) > 10:
                return Response({'error': 'You can select up to 10 photos'}, status=status.HTTP_400_BAD_REQUEST)

            # Récupérer les photos dans l'ordre de sélection (ordre des photo_ids)
            photos_dict = {p.id: p for p in PostPhoto.objects.filter(recipe_batch=batch, id__in=photo_ids)}
            if len(photos_dict) != len(photo_ids):
                return Response({'error': 'Some photos are invalid or do not belong to this batch'}, status=status.HTTP_400_BAD_REQUEST)

            # Préserver l'ordre de sélection
            photos = [photos_dict[pid] for pid in photo_ids]

        complete_recipe_batch_workflow(request.user, batch)

        post = Post.objects.create(
            user=request.user,
            recipe_batch=batch,
            comment=comment,
            is_published=True
        )

        # Associer les photos au post dans l'ordre de sélection et définir l'ordre
        for order_index, photo in enumerate(photos, start=1):
            photo.post = post
            photo.order = order_index
            photo.save(update_fields=['post', 'order'])

        # Retourner une réponse simplifiée pour éviter les timeouts
        # (les presigned URLs seront générées lors de la récupération du post)
        return Response({
            'id': post.id,
            'comment': post.comment,
            'is_published': post.is_published,
            'created_at': post.created_at,
            'photo_ids': photo_ids,
            'message': 'Post créé avec succès'
        }, status=status.HTTP_201_CREATED)
    
    @action(detail=True, methods=['get'], url_path='published-post')
    def published_post(self, request, pk=None):
        """Récupérer le post publié associé à ce batch"""
        batch = self.get_object()
        try:
            post = Post.objects.filter(recipe_batch=batch, is_published=True).first()
            if post:
                from .serializers import PostSerializer
                serializer = PostSerializer(post, context={'request': request})
                return Response(serializer.data)
            else:
                return Response({}, status=status.HTTP_200_OK)
        except Exception as e:
            return Response({'error': str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
    
    @action(detail=True, methods=['post'], url_path='apply-to-dates')
    def apply_to_dates(self, request, pk=None):
        """Appliquer un batch à plusieurs dates en créant les meal plans nécessaires."""
        from django.db import transaction
        from decimal import Decimal
        from datetime import datetime
        
        batch = self.get_object()
        
        # Vérifier que le batch n'est pas déjà cuisiné
        if batch.is_cooked:
            return Response(
                {'error': 'Cannot apply a batch that has already been cooked'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        date_keys = request.data.get('date_keys', [])
        meal_time_param = request.data.get('meal_time')
        portions = request.data.get('portions')  # optional; None = suit le nombre de personnes
        
        if not date_keys or not isinstance(date_keys, list):
            return Response(
                {'error': 'date_keys must be a non-empty list'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        if not meal_time_param:
            return Response(
                {'error': 'meal_time is required'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        if portions is not None:
            try:
                portions = int(portions)
                portions = max(0, portions)
            except (ValueError, TypeError):
                portions = None

        STANDARD = {'lunch', 'dinner', 'breakfast'}

        def _parse_scheduled_time(val):
            if not val:
                return None
            if isinstance(val, str):
                for fmt in ('%H:%M:%S', '%H:%M'):
                    try:
                        return datetime.strptime(val.strip(), fmt).time()
                    except ValueError:
                        continue
            return None

        slot_key_param = (request.data.get('slot_key') or '').strip()
        custom_label_param = (request.data.get('custom_label') or '').strip()
        scheduled_time = _parse_scheduled_time(request.data.get('scheduled_time'))

        if meal_time_param in STANDARD:
            db_meal_time = meal_time_param
            resolved_slot_key = meal_time_param
            custom_label_save = ''
            scheduled_save = None
        elif meal_time_param == 'other':
            resolved_slot_key = slot_key_param
            if not resolved_slot_key:
                return Response(
                    {'error': 'slot_key is required when meal_time is other'},
                    status=status.HTTP_400_BAD_REQUEST
                )
            db_meal_time = 'other'
            custom_label_save = custom_label_param or 'Repas'
            scheduled_save = scheduled_time
        else:
            # Client envoie la clé de créneau (ex. UUID) dans meal_time
            db_meal_time = 'other'
            resolved_slot_key = slot_key_param or str(meal_time_param).strip()
            if not resolved_slot_key:
                return Response(
                    {'error': 'Invalid meal_time / slot_key'},
                    status=status.HTTP_400_BAD_REQUEST
                )
            custom_label_save = custom_label_param or 'Repas'
            scheduled_save = scheduled_time
        
        created_meal_plans = []
        
        with transaction.atomic():
            for date_key in date_keys:
                try:
                    target_date = datetime.strptime(date_key, '%Y-%m-%d').date()
                except ValueError:
                    continue
                
                existing_meal_plan = MealPlan.objects.filter(
                    user=request.user,
                    date=target_date,
                    slot_key=resolved_slot_key,
                ).first()
                
                if existing_meal_plan:
                    if not existing_meal_plan.meal_plan_recipe_batches.filter(recipe_batch=batch).exists():
                        MealPlanRecipeBatch.objects.create(
                            meal_plan=existing_meal_plan,
                            recipe_batch=batch,
                            portions=portions,
                            is_portions_overridden=portions is not None,
                            order=existing_meal_plan.meal_plan_recipe_batches.count()
                        )
                    meal_plan = existing_meal_plan
                else:
                    meal_plan = MealPlan.objects.create(
                        user=request.user,
                        date=target_date,
                        meal_time=db_meal_time,
                        slot_key=resolved_slot_key,
                        custom_label=custom_label_save if db_meal_time == 'other' else '',
                        scheduled_time=scheduled_save if db_meal_time == 'other' else None,
                        meal_type='recipe',
                        confirmed=False,
                    )
                    MealPlanRecipeBatch.objects.create(
                        meal_plan=meal_plan,
                        recipe_batch=batch,
                        portions=portions,
                        is_portions_overridden=portions is not None,
                        order=0
                    )
                
                created_meal_plans.append(meal_plan)
        
        # Sérialiser les meal plans créés
        from .serializers import MealPlanSerializer
        serializer = MealPlanSerializer(created_meal_plans, many=True, context={'request': request})
        return Response(serializer.data, status=status.HTTP_201_CREATED)

PHOTO_TYPES = [choice[0] for choice in PostPhoto.PHOTO_TYPE_CHOICES]
RESTRICTED_PHOTO_TYPES = PostPhoto.UNIQUE_TYPES


def calculate_meal_plan_servings(meal_plan, group_meal_plans=None, include_breakdown=False, recipe_batch_id=None):
    """
    Calcule le nombre total de personnes pour un meal plan (sans ratio).
    Retourne ce nombre ; si include_breakdown=True, retourne aussi un breakdown
    avec people_count et portions effectives par batch.
    """
    if hasattr(meal_plan, '_total_servings'):
        people = meal_plan._total_servings
        if include_breakdown:
            breakdown = []
            for mprb in getattr(meal_plan, 'meal_plan_recipe_batches', MealPlanRecipeBatch.objects.none()).filter(recipe_batch_id=recipe_batch_id) if recipe_batch_id else getattr(meal_plan, 'meal_plan_recipe_batches', MealPlanRecipeBatch.objects.none()).all():
                portions = get_batch_portions(meal_plan, mprb, people_count=people)
                breakdown.append({
                    'meal_plan_id': meal_plan.id,
                    'recipe_batch_id': mprb.recipe_batch_id,
                    'base_servings': people,
                    'portions': portions,
                })
            return people, breakdown
        return (people, []) if include_breakdown else people

    if group_meal_plans and len(group_meal_plans) > 1:
        total_guest_count = sum(mp.guest_count or 0 for mp in group_meal_plans)
        all_participants = []
        for mp in group_meal_plans:
            invitations = mp.invitations.all() if hasattr(mp, 'invitations') else []
            for inv in invitations:
                all_participants.append({
                    'user': inv.invitee,
                    'status': inv.status,
                })
        active_participants_by_user = {}
        for p in all_participants:
            if p.get('status') in ['accepted', 'pending']:
                user_id = p['user'].id if hasattr(p['user'], 'id') else p['user']['id'] if isinstance(p['user'], dict) else None
                if user_id:
                    existing_status = active_participants_by_user.get(user_id)
                    if not existing_status or (p.get('status') == 'accepted' and existing_status != 'accepted'):
                        active_participants_by_user[user_id] = p.get('status')
        active_participants_count = len(active_participants_by_user)
        base_servings = len(group_meal_plans) + active_participants_count + total_guest_count
    else:
        participants_count = meal_plan.invitations.filter(
            status__in=['accepted', 'pending']
        ).count() if hasattr(meal_plan, 'invitations') else 0
        guest_count = meal_plan.guest_count or 0
        base_servings = 1 + participants_count + guest_count

    people_count = int(base_servings)
    if include_breakdown:
        breakdown = []
        mprbs_qs = getattr(meal_plan, 'meal_plan_recipe_batches', MealPlanRecipeBatch.objects.none()).all()
        if recipe_batch_id is not None:
            mprbs_qs = mprbs_qs.filter(recipe_batch_id=recipe_batch_id)
        for mprb in mprbs_qs:
            portions = get_batch_portions(meal_plan, mprb, people_count=people_count)
            breakdown.append({
                'meal_plan_id': meal_plan.id,
                'recipe_batch_id': mprb.recipe_batch_id,
                'base_servings': people_count,
                'portions': portions,
            })
        return float(people_count), breakdown
    return float(people_count)


def get_batch_portions(meal_plan, mprb, people_count=None):
    """Délègue à recipes.utils.get_batch_portions pour cohérence."""
    from .utils import get_batch_portions as _get_batch_portions
    return _get_batch_portions(meal_plan, mprb, people_count=people_count)


class RecipeViewSet(viewsets.ModelViewSet):
    """ViewSet pour les recettes"""
    queryset = Recipe.objects.all()
    permission_classes = [IsAuthenticated]
    
    def get_serializer_class(self):
        if self.action == 'create':
            return RecipeCreateSerializer
        # Utiliser RecipeLightSerializer pour les listes (pas besoin de steps/ingredients)
        if self.action in ['list', 'search', 'search_fuzzy', 'search_semantic', 'my_imports', 'my_favorites', 'my_recipes']:
            return RecipeLightSerializer
        # Utiliser RecipeDetailSerializer pour retrieve (léger, sans steps/ingredients)
        if self.action == 'retrieve':
            return RecipeDetailSerializer
        # Utiliser RecipeSerializer complet pour update, etc.
        return RecipeSerializer
    
    def get_queryset(self):
        """Filtrer selon is_public et user, puis appliquer les autres filtres"""
        user = self.request.user
        list_actions = ['list', 'search', 'search_fuzzy', 'search_semantic', 'my_imports', 'my_favorites', 'my_recipes']

        # Recherche / listes : publiques + propres recettes uniquement
        if user.is_authenticated:
            if self.action in list_actions:
                queryset = Recipe.objects.filter(
                    Q(is_public=True) | Q(created_by=user)
                )
            else:
                queryset = Recipe.objects.filter(
                    Q(is_public=True) | Q(created_by=user) | get_invited_recipe_filter(user)
                ).distinct()
        else:
            queryset = Recipe.objects.filter(is_public=True)
        
        meal_type = self.request.query_params.get('meal_type', None)
        difficulty = self.request.query_params.get('difficulty', None)
        search = self.request.query_params.get('search', None)
        max_total_time = self.request.query_params.get('max_total_time', None)
        mine = self.request.query_params.get('mine', '').lower() in ('1', 'true', 'yes')
        created_by = self.request.query_params.get('created_by', None)
        
        if meal_type:
            queryset = queryset.filter(meal_type=meal_type)
        if difficulty:
            queryset = queryset.filter(difficulty=difficulty)
        if mine:
            queryset = queryset.filter(created_by=user)
        if created_by:
            try:
                queryset = queryset.filter(created_by_id=int(created_by))
            except (ValueError, TypeError):
                pass
        if max_total_time:
            try:
                max_mins = int(max_total_time)
                queryset = queryset.annotate(
                    total_time_mins=F('prep_time') + F('cook_time')
                ).filter(total_time_mins__lte=max_mins)
            except (ValueError, TypeError):
                pass
        if search:
            # Pour les listes, chercher uniquement dans le titre (plus rapide)
            if self.action in ['list', 'search', 'search_fuzzy', 'search_semantic']:
                queryset = queryset.filter(title__icontains=search)
            else:
                queryset = queryset.filter(
                    Q(title__icontains=search) | Q(description__icontains=search)
                )
        
        # Pour les listes, ne pas précharger steps et ingredients (inutiles)
        # Utiliser defer() pour exclure les gros champs
        if self.action in ['list', 'search', 'search_fuzzy', 'search_semantic']:
            defer_fields = [
                'description',
                'created_at',
                'updated_at',
                'search_index_text',
                'search_context_tags',
                'search_index_hash',
            ]
            if self.action != 'search_fuzzy':
                defer_fields.append('created_by_id')
            queryset = queryset.defer(*defer_fields)
            if self.action == 'search_fuzzy':
                queryset = queryset.select_related('created_by')
        elif self.action == 'retrieve':
            # Pour retrieve : ne pas précharger steps et ingredients (chargés via endpoints séparés)
            # Juste select_related pour created_by
            queryset = queryset.select_related('created_by')
        else:
            # Pour update, etc. : précharger les steps avec leurs ingrédients
            queryset = queryset.prefetch_related(
                'recipe_ingredients__ingredient',
            ).select_related('created_by')

        queryset = queryset.order_by('-created_at')
        # IMPORTANT: on ne filtre pas en dur sur les listes “générales”.
        # Le mode strict est réservé aux endpoints de suggestions dédiés.
        return queryset
    
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
            
            # Ajouter les meal plans proches si demandé (suggestions de recettes issues d'autres jours)
            include_nearby = request.query_params.get('include_nearby_meal_plans', 'false').lower() == 'true'
            if include_nearby:
                target_date_str = request.query_params.get('date')
                meal_time = request.query_params.get('meal_time')
                slot_key_qp = (request.query_params.get('slot_key') or '').strip()
                
                if target_date_str and meal_time:
                    try:
                        target_date = datetime.strptime(target_date_str, '%Y-%m-%d').date()
                        current_slot_key = slot_key_qp or meal_time
                        nearby_meal_plans = self._get_nearby_meal_plans(
                            request.user, target_date, current_slot_key
                        )
                        
                        # Suggestions basées sur les batches (pas de meal_type/difficulty ici)
                        batch_suggestions = []
                        suggested_recipe_ids = set()  # Pour filtrer les doublons de recettes
                        seen_batches = set()
                        
                        def badge_label_for_date(target_date, earliest_date):
                            delta = (earliest_date - target_date).days
                            if delta == 0:
                                return "Aujourd'hui"
                            if delta == 1:
                                return "J+1"
                            if delta == -1:
                                return "J-1"
                            if delta > 1:
                                return f"J+{delta}"
                            return f"J{delta}"
                        
                        for meal_plan in nearby_meal_plans:
                            for mprb in meal_plan.meal_plan_recipe_batches.all().select_related('recipe_batch__recipe', 'recipe_batch__created_by'):
                                batch = mprb.recipe_batch
                                if not batch or not batch.recipe:
                                    continue
                                if batch.id in seen_batches:
                                    continue
                                # Filtrer : ne garder que les batches dont l'utilisateur est le créateur
                                if batch.created_by_id != request.user.id:
                                    continue
                                seen_batches.add(batch.id)
                                
                                recipe = batch.recipe
                                recipe_data = RecipeLightSerializer(recipe).data
                                # Nettoyer les infos non nécessaires
                                recipe_data.pop('meal_type', None)
                                recipe_data.pop('meal_type_display', None)
                                recipe_data.pop('difficulty', None)
                                recipe_data.pop('difficulty_display', None)
                                
                                # Dates liées à ce batch (toutes les meal plans qui l’utilisent)
                                related_mps = MealPlan.objects.filter(
                                    meal_plan_recipe_batches__recipe_batch_id=batch.id
                                ).distinct()
                                grouped_dates = sorted({mp.date for mp in related_mps})
                                earliest_date = grouped_dates[0] if grouped_dates else meal_plan.date
                                
                                # Nombre total de personnes : somme des servings de chaque meal plan lié
                                total_servings = 0
                                for mp in related_mps:
                                    total_servings += calculate_meal_plan_servings(mp)
                                
                                suggestion = {
                                    **recipe_data,
                                    'is_batch': True,
                                    'batch_id': batch.id,
                                    'batch_earliest_date': earliest_date.strftime('%Y-%m-%d'),
                                    'badge_label': badge_label_for_date(target_date, earliest_date),
                                    'total_servings': total_servings,
                                    'meal_time': meal_plan.meal_time,
                                    'original_date': earliest_date.strftime('%Y-%m-%d'),
                                    'earliest_date': earliest_date.strftime('%Y-%m-%d'),
                                    'groupedDates': [d.isoformat() for d in grouped_dates],
                                }
                                batch_suggestions.append(suggestion)
                                
                                if recipe_data.get('id'):
                                    suggested_recipe_ids.add(recipe_data['id'])
                        
                        # Mélanger les suggestions et limiter à 3
                        import random
                        random.shuffle(batch_suggestions)
                        batch_suggestions = batch_suggestions[:3]
                        
                        # Filtrer les recettes déjà suggérées via un batch
                        data = [item for item in data if item.get('id') not in suggested_recipe_ids]
                        
                        # Insérer les suggestions de batches au début de la liste
                        data = list(batch_suggestions) + list(data)
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

    @action(detail=False, methods=['get'], url_path='suggested')
    def suggested(self, request):
        """
        Suggestions personnalisées:
        - mode strict: exclusion allergies + régimes (jamais dislikes)
        - dislikes: dépriorisation légère (dans la page courante), sans exclusion
        """
        queryset = self.filter_queryset(self.get_queryset())
        queryset = apply_dietary_exclusion(queryset, request.user)

        page = self.paginate_queryset(queryset)
        if page is None:
            items = list(queryset[:50])
        else:
            items = list(page)

        # Déprioriser les dislikes: on conserve l’ordre relatif, mais on met d’abord les recettes
        # sans conflit "dislike" pour l’utilisateur courant (allergies/régimes déjà exclues).
        try:
            ids = [r.id for r in items if getattr(r, 'id', None) is not None]
            reasons_map = conflict_reasons_by_recipe_id(ids, request.user)
            disliked = {rid for rid, reasons in (reasons_map or {}).items() if 'dislike' in (reasons or [])}
        except Exception:
            disliked = set()

        items.sort(key=lambda r: (1 if r.id in disliked else 0))

        serializer = RecipeLightSerializer(items, many=True, context={'request': request})
        if page is not None:
            return self.get_paginated_response(serializer.data)
        return Response(serializer.data)
    
    def _get_nearby_meal_plans(self, user, target_date, current_slot_key, max_days=4, limit=10):
        """Récupérer les meal plans non cuisinés des jours proches, sauf le créneau courant (slot_key)."""
        from django.db.models import Prefetch, Q
        from .models import MealPlanRecipeBatch
        from django.utils import timezone
        from datetime import timedelta
        
        # Calculer la plage de dates : 4 jours en arrière et 4 jours en avant
        date_start = target_date - timedelta(days=max_days)
        date_end = target_date + timedelta(days=max_days)
        
        # Récupérer les meal plans non cuisinés dans la plage.
        # Nouvelle logique : proposer tous les batches des jours proches,
        # quel que soit le meal_time, à l’exception du meal plan du créneau courant.
        meal_plans = MealPlan.objects.filter(
            user=user,
            date__gte=date_start,
            date__lte=date_end,
        ).exclude(
            date=target_date,
            slot_key=current_slot_key,
        ).filter(
            meal_plan_recipe_batches__recipe_batch__is_cooked=False
        ).exclude(
            meal_plan_recipe_batches__recipe_batch__cooking_progresses__status='in_progress'
        ).prefetch_related(
            Prefetch('meal_plan_recipe_batches', queryset=MealPlanRecipeBatch.objects.select_related('recipe_batch__recipe').order_by('order')),
            'invitations',
            # Les groupes sont maintenant au niveau des recettes, pas des meal plans
        ).order_by('-date', 'meal_time').distinct()[:limit]  # Trier par date décroissante puis meal_time
        
        return meal_plans
    
    def perform_create(self, serializer):
        recipe = serializer.save(created_by=self.request.user)
        schedule_recipe_search_reindex(recipe.id)
    
    @action(detail=True, methods=['get'])
    def steps(self, request, pk=None):
        """
        Endpoint séparé pour charger les steps d'une recette.
        Chargé de manière lazy quand l'utilisateur clique sur "Go".
        """
        recipe = self.get_object()
        # Les steps sont directement liés à la recette
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
    
    @action(detail=True, methods=['get'])
    def summary(self, request, pk=None):
        """
        Récupérer la description et le résumé des étapes de la recette.
        Endpoint léger pour afficher ces infos dans RecipeSummaryModal.
        """
        recipe = self.get_object()
        return Response({
            'description': recipe.description,
            'steps_summary': recipe.steps_summary,
        })

    @action(detail=True, methods=['put'], url_path='edit')
    def edit(self, request, pk=None):
        """
        Mettre à jour une recette (métadonnées + ingrédients + steps) par son auteur.
        Attendu en payload :
        - champs meta (title, description, steps_summary, meal_type, difficulty, prep_time, cook_time, servings, image_path, is_public)
        - ingredients: [{ingredient_id?, ingredient_name?, quantity, unit}]
        - steps: [{title, instruction, tip?, has_timer?, timer_duration?}]
        """
        recipe = self.get_object()
        if recipe.created_by_id != request.user.id:
            return Response({'error': "Seul l'auteur peut modifier la recette."}, status=status.HTTP_403_FORBIDDEN)

        data = request.data or {}

        meta_fields = ['title', 'description', 'steps_summary', 'meal_type', 'difficulty', 'prep_time', 'cook_time', 'servings', 'image_path', 'is_public']

        with transaction.atomic():
            # Mettre à jour les métadonnées si présentes
            for field in meta_fields:
                if field in data:
                    setattr(recipe, field, data.get(field))
            recipe.save()

            # Mettre à jour les ingrédients
            if 'ingredients' in data:
                ingredients_payload = data.get('ingredients') or []
                RecipeIngredient.objects.filter(recipe=recipe).delete()

                for item in ingredients_payload:
                    if not isinstance(item, dict):
                        continue

                    ingredient_id = item.get('ingredient_id') or (item.get('ingredient') or {}).get('id')
                    ingredient_name = item.get('ingredient_name') or (item.get('ingredient') or {}).get('name') or item.get('name')

                    ingredient_obj = None
                    if ingredient_id:
                        ingredient_obj = Ingredient.objects.filter(pk=ingredient_id).first()

                    if not ingredient_obj and ingredient_name:
                        ingredient_name = ingredient_name.strip()
                        if ingredient_name:
                            ingredient_obj, _ = Ingredient.objects.get_or_create(name=ingredient_name)

                    if not ingredient_obj:
                        # Impossible de déterminer l'ingrédient, on ignore cette entrée
                        continue

                    quantity = item.get('quantity', 0)
                    try:
                        quantity_decimal = Decimal(str(quantity))
                    except Exception:
                        quantity_decimal = Decimal('0')

                    unit = item.get('unit') or 'g'
                    RecipeIngredient.objects.create(
                        recipe=recipe,
                        ingredient=ingredient_obj,
                        quantity=quantity_decimal,
                        unit=unit,
                    )

            # Mettre à jour les steps
            if 'steps' in data:
                steps_payload = data.get('steps') or []
                Step.objects.filter(recipe=recipe).delete()

                for idx, step_data in enumerate(steps_payload):
                    if not isinstance(step_data, dict):
                        continue

                    Step.objects.create(
                        recipe=recipe,
                        order=idx,
                        title=step_data.get('title', '') or '',
                        instruction=step_data.get('instruction') or step_data.get('text') or '',
                        tip=step_data.get('tip', '') or '',
                        has_timer=bool(step_data.get('has_timer', False)),
                        timer_duration=step_data.get('timer_duration'),
                    )

        schedule_recipe_search_reindex(recipe.id)

        serializer = RecipeSerializer(recipe, context={'request': request})
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
        """Récupérer uniquement les recettes importées de l'utilisateur"""
        recipes = Recipe.objects.filter(created_by=request.user)
        summary_only = request.query_params.get('summary')
        if summary_only:
            count = recipes.count()
            last_recipe = recipes.order_by('-updated_at').first()
            return Response({
                'count': count,
                'last_activity': last_recipe.updated_at if last_recipe else None,
            })
        # Trier par date de mise à jour (plus récentes en premier)
        recipes = recipes.order_by('-updated_at')
        page = self.paginate_queryset(recipes)
        serializer = self.get_serializer(page if page is not None else recipes, many=True)
        if page is not None:
            return self.get_paginated_response(serializer.data)
        return Response(serializer.data)
    
    @action(detail=False, methods=['get'])
    def user_imports(self, request):
        """
        Récupérer les recettes importées d'un utilisateur spécifique.
        Utilisé pour afficher le livre d'import d'un autre profil.
        """
        from django.shortcuts import get_object_or_404
        User = get_user_model()
        user_id = request.query_params.get('user')
        if not user_id:
            return Response({'error': 'Paramètre user requis'}, status=status.HTTP_400_BAD_REQUEST)
        
        target_user = get_object_or_404(User, id=user_id)
        recipes = Recipe.objects.filter(created_by=target_user)
        summary_only = request.query_params.get('summary')
        if summary_only:
            count = recipes.count()
            last_recipe = recipes.order_by('-updated_at').first()
            return Response({
                'count': count,
                'last_activity': last_recipe.updated_at if last_recipe else None,
            })
        
        recipes = recipes.order_by('-updated_at')
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
        serializer = RecipeImportRequestLightSerializer(qs, many=True, context={'request': request})
        return Response(serializer.data)

    @action(detail=False, methods=['post'], url_path='generate_from_idea')
    def generate_from_idea(self, request):
        """
        Génère une recette via IA à partir d'une idée libre (asynchrone via Celery).
        """
        logger = logging.getLogger(__name__)
        serializer = RecipeGenerateFromIdeaSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        data = serializer.validated_data
        try:
            import_request = RecipeImportRequest.objects.create(
                user=request.user,
                payload={
                    'idea_text': data['idea_text'],
                    'servings': data.get('servings'),
                    'job_type': 'generate',
                    'source_type': 'generated',
                },
                status=RecipeImportRequest.STATUS_PENDING,
            )

            from .tasks import process_recipe_generate_from_idea
            task = process_recipe_generate_from_idea.delay(str(import_request.id))
            import_request.task_id = task.id
            import_request.save(update_fields=['task_id'])

            logger.info(
                "[RecipeGenerate] Idea generation queued - request_id=%s",
                import_request.id,
            )

            return Response(
                {
                    'request_id': import_request.id,
                    'status': import_request.status,
                    'idea_text': data['idea_text'],
                    'job_type': 'generate',
                },
                status=status.HTTP_202_ACCEPTED,
            )
        except Exception as e:
            logger.error("Erreur lors de la génération de recette: %s", e, exc_info=True)
            return Response(
                {'error': f'Erreur lors de la génération: {str(e)}'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

    @action(detail=False, methods=['post'], url_path='import_from_url')
    def import_from_url(self, request):
        """
        Importe une recette depuis une URL externe (Bergamot, Marmiton, etc.)
        L'extraction et la formalisation sont faites de manière asynchrone via Celery.

        Avant de lancer un nouvel import, on vérifie si une recette importée depuis
        la même URL (normalisée) existe déjà et est accessible pour l'utilisateur.
        """
        logger = logging.getLogger(__name__)
        url = request.data.get('url', '').strip()
        if not url:
            return Response(
                {'error': 'URL requise'},
                status=status.HTTP_400_BAD_REQUEST
            )

        from .utils import canonicalize_import_url

        # Normaliser l'URL pour la déduplication
        canonical_url = canonicalize_import_url(url)

        # Gérer les anciennes valeurs import_source_url qui peuvent contenir ou non un slash final
        candidate_urls = {canonical_url, url}
        if canonical_url.endswith('/'):
            candidate_urls.add(canonical_url.rstrip('/'))
        else:
            candidate_urls.add(canonical_url + '/')

        # Chercher une recette déjà importée depuis cette URL, accessible par l'utilisateur
        existing_recipe = (
            Recipe.objects.filter(
                Q(is_public=True) | Q(created_by=request.user),
            )
            .filter(import_source_url__in=list(candidate_urls))
            .order_by('-created_at')
            .first()
        )

        if existing_recipe:
            from .serializers import RecipeDetailSerializer
            serializer = RecipeDetailSerializer(existing_recipe, context={'request': request})
            return Response(
                {
                    'already_imported': True,
                    'recipe_id': existing_recipe.id,
                    'recipe': serializer.data,
                },
                status=status.HTTP_200_OK
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

    @action(detail=False, methods=['get'], url_path='search_fuzzy')
    def search_fuzzy(self, request):
        """
        Recherche rapide par similarité trigram sur le titre (sans embedding).
        ``q`` est optionnel : sans texte, les filtres query (difficulty, meal_type,
        max_total_time, mine, created_by) suffisent ; sans rien, liste paginée.
        """
        query = request.query_params.get('q', '').strip()
        base_qs = self.filter_queryset(self.get_queryset())
        queryset = fuzzy_recipe_queryset(base_qs, query) if query else base_qs

        paginated_queryset = self.paginate_queryset(queryset)
        if paginated_queryset is not None:
            serializer = RecipeLightSerializer(
                paginated_queryset, many=True, context={'request': request},
            )
            return self.get_paginated_response(serializer.data)

        page_size = min(int(request.query_params.get('page_size', 20)), 50)
        serializer = RecipeLightSerializer(queryset[:page_size], many=True, context={'request': request})
        return Response(serializer.data)

    @action(detail=False, methods=['get'], url_path='search_semantic')
    def search_semantic(self, request):
        query = request.query_params.get('q', '').strip()

        if not query:
            return Response({'error': 'Paramètre q requis.'}, status=status.HTTP_400_BAD_REQUEST)

        embeddings = get_batch_embeddings([query], input_type='query')
        vector = embeddings[0] if embeddings else None
        if not vector:
            logging.getLogger(__name__).warning(
                '[search_semantic] embedding indisponible pour q=%r — recherche trigram seule',
                query,
            )

        base_qs = self.filter_queryset(self.get_queryset())
        queryset = hybrid_recipe_queryset(base_qs, query, vector)

        paginated_queryset = self.paginate_queryset(queryset)
        if paginated_queryset is not None:
            items = list(paginated_queryset)
            # Dépriorisation: pousser en bas ce qui est incompatible (allergy > diet > dislike),
            # sans jamais supprimer de résultats.
            try:
                ids = [r.id for r in items if getattr(r, 'id', None) is not None]
                reasons_map = conflict_reasons_by_recipe_id(ids, request.user) or {}

                def _penalty(reasons):
                    if not reasons:
                        return 0
                    if 'allergy' in reasons:
                        return 3
                    if 'diet' in reasons:
                        return 2
                    if 'dislike' in reasons:
                        return 1
                    return 0

                indexed = [(idx, r, _penalty(reasons_map.get(r.id))) for idx, r in enumerate(items)]
                indexed.sort(key=lambda t: (t[2], t[0]))
                items = [t[1] for t in indexed]
            except Exception:
                pass

            serializer = RecipeLightSerializer(items, many=True, context={'request': request})
            return self.get_paginated_response(serializer.data)
        
        # Fallback si pas de pagination
        page_size = min(int(request.query_params.get('page_size', 20)), 50)
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
    queryset = Category.objects.select_related('parent').all()
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
    
    @action(detail=False, methods=['get'])
    def frequent(self, request):
        """
        Récupère les ingrédients fréquemment ajoutés par l'utilisateur.
        Basé sur les ShoppingListItem créés par l'utilisateur (via checked_by ou shopping_list owner).
        """
        user = request.user
        limit = int(request.query_params.get('limit', 10))
        
        # Récupérer les ingrédients les plus fréquents dans les listes de l'utilisateur
        frequent_ingredients = Ingredient.objects.filter(
            shopping_list_items__shopping_list__members__user=user
        ).annotate(
            usage_count=Count('shopping_list_items', distinct=True)
        ).order_by('-usage_count', 'name')[:limit]
        
        serializer = self.get_serializer(frequent_ingredients, many=True)
        return Response(serializer.data)


class MealPlanViewSet(viewsets.ModelViewSet):
    """ViewSet pour les repas planifiés"""
    serializer_class = MealPlanSerializer
    permission_classes = [IsAuthenticated]
    
    def get_serializer_class(self):
        # Utiliser des serializers adaptés par action
        if self.action == 'retrieve':
            return MealPlanDetailSerializer  # Serializer léger pour retrieve

        view_mode = (self.request.query_params.get('view') or '').strip().lower()
        if self.action in ['list'] and view_mode == 'timeline':
            from .serializers import MealPlanTimelineSerializer
            return MealPlanTimelineSerializer
        
        # Détecter le mode minimal via paramètre query
        is_minimal = self.request.query_params.get('minimal', '').lower() == 'true'
        
        if self.action in ['list']:
            if is_minimal:
                return MealPlanMinimalListSerializer
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
        
        # Utiliser le filtre accessible qui inclut les meal plans où l'utilisateur est invité accepté
        accessible_meal_plan_filter = get_accessible_meal_plan_filter(self.request.user)
        qs = MealPlan.objects.filter(accessible_meal_plan_filter).distinct()
        
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
            # V2: un meal plan est considéré "dans une liste" si l'un de ses recipe_batches
            # est associé à une shopping list non archivée
            qs = qs.exclude(
                meal_plan_recipe_batches__recipe_batch__shopping_list_batch__shopping_list__is_archived=False
            )
        
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
        # Définir l'ordre des meal_time : lunch (0) avant dinner (1)
        meal_time_order = Case(
            When(meal_time='lunch', then=0),
            When(meal_time='dinner', then=1),
            default=2,
            output_field=IntegerField(),
        )
        
        # Chargement optimisé des relations utilisées par le serializer
        from django.db.models import Prefetch
        from .models import MealPlanRecipeBatch, StepIngredient
        
        # Détecter le mode minimal
        is_minimal = self.request.query_params.get('minimal', '').lower() == 'true'
        include_shopping_list = self.request.query_params.get('include_shopping_list', '').lower() == 'true'
        view_mode = (self.request.query_params.get('view') or '').strip().lower()
        
        if self.action in ['list']:
            if is_minimal:
        # Mode minimal : NE PAS charger recipe ni meal_plan_recipe_batches du tout
        # WeekPlanModal n'a besoin que de meal_type, donc pas besoin de recipes/batches
                # Utiliser only() pour limiter les champs chargés de la DB
                qs = qs.only(
                'id', 'date', 'meal_time', 'meal_type', 'confirmed'
                ).order_by('date', meal_time_order)
            else:
                # Mode complet : précharger les relations nécessaires (plus de recipe directe)
                mprb_select_related = ['recipe_batch__recipe']
                if include_shopping_list:
                    # OneToOne: permet au serializer de renvoyer shopping_list sans requêtes supplémentaires
                    mprb_select_related.append('recipe_batch__shopping_list_batch__shopping_list')
                # Timeline: pas besoin de shopping_list ni de champs user lourds, mais besoin de user + invitations + recipes thumbs
                if view_mode == 'timeline':
                    include_shopping_list = False
                    qs = qs.annotate(
                        is_guest_annot=Exists(
                            MealInvitation.objects.filter(
                                meal_plan_id=OuterRef('pk'),
                                invitee=self.request.user,
                                status='accepted',
                            )
                        ),
                        has_published_post_annot=Exists(
                            Post.objects.filter(
                                meal_plan_id=OuterRef('pk'),
                                is_published=True,
                            )
                        ),
                    )
                qs = qs.select_related('user').prefetch_related(
                    Prefetch(
                        'invitations',
                        queryset=MealInvitation.objects.filter(status__in=['accepted', 'pending']).select_related('invitee'),
                    ),
                    Prefetch(
                        'meal_plan_recipe_batches',
                        queryset=MealPlanRecipeBatch.objects.select_related(
                            *mprb_select_related,
                        ).order_by('order'),
                    ),
                ).order_by('date', meal_time_order)
        elif self.action in ['by_date']:
            mprb_select_related = ['recipe_batch__recipe']
            if include_shopping_list:
                mprb_select_related.append('recipe_batch__shopping_list_batch__shopping_list')
            qs = qs.select_related('user').prefetch_related(
                Prefetch('invitations', queryset=MealInvitation.objects.select_related('invitee')),
                Prefetch(
                    'meal_plan_recipe_batches',
                    queryset=MealPlanRecipeBatch.objects.select_related(
                        *mprb_select_related,
                    ).order_by('order'),
                ),
            ).order_by('date', meal_time_order)
        elif self.action in ['by_week', 'by_dates', 'bulk']:
            mprb_select_related = ['recipe_batch__recipe']
            if include_shopping_list:
                mprb_select_related.append('recipe_batch__shopping_list_batch__shopping_list')
            qs = qs.select_related('user').prefetch_related(
                Prefetch(
                    'meal_plan_recipe_batches',
                    queryset=MealPlanRecipeBatch.objects.select_related(
                        *mprb_select_related,
                    ).order_by('order'),
                ),
            ).order_by('date', meal_time_order)
        else:
            # Pour retrieve : préfetch minimal (pas de steps ni recipe_ingredients détaillés)
            mprb_select_related = ['recipe_batch__recipe']
            if include_shopping_list:
                mprb_select_related.append('recipe_batch__shopping_list_batch__shopping_list')
            qs = qs.select_related('user').prefetch_related(
                Prefetch('invitations', queryset=MealInvitation.objects.select_related('invitee')),
                Prefetch(
                    'meal_plan_recipe_batches',
                    queryset=MealPlanRecipeBatch.objects.select_related(
                        *mprb_select_related,
                    ).order_by('order'),
                ),
            ).order_by('date', meal_time_order)
        return qs

    @action(detail=False, methods=['get'], url_path='publish-candidates')
    def publish_candidates(self, request):
        """
        Repas "prêts à publier":
        - au moins une photo at_meal_time (non draft) sur un batch de ce meal plan
        - aucun post publié sur les batches du meal plan
        - accessible à l'utilisateur courant
        """
        try:
            days = int(request.query_params.get('days', 7))
        except (TypeError, ValueError):
            days = 7
        days = max(1, min(days, 30))

        now = timezone.localtime(timezone.now())
        today = timezone.localdate()
        start_date = today - timedelta(days=days)

        accessible_meal_plan_filter = get_accessible_meal_plan_filter(request.user)

        qs = (
            MealPlan.objects.filter(accessible_meal_plan_filter)
            .filter(date__gte=start_date, date__lte=today)
            .annotate(
                has_published_post=Exists(
                    Post.objects.filter(
                        recipe_batch__meal_plan_recipe_batches__meal_plan_id=OuterRef('pk'),
                        is_published=True,
                    )
                ),
                has_at_meal_photos=Exists(
                    PostPhoto.objects.filter(
                        recipe_batch__meal_plan_recipe_batches__meal_plan_id=OuterRef('pk'),
                        photo_type='at_meal_time',
                        is_draft=False,
                    )
                ),
            )
            .filter(has_published_post=False, has_at_meal_photos=True)
            .order_by('-date', '-id')
        )

        results = []
        for mp in qs[:30]:
            photos = list(
                PostPhoto.objects.filter(
                    recipe_batch__meal_plan_recipe_batches__meal_plan=mp,
                    photo_type='at_meal_time',
                    is_draft=False,
                )
                .order_by('-created_at')[:4]
            )
            results.append(
                {
                    'meal_plan_id': mp.id,
                    'date': mp.date,
                    'meal_time': mp.meal_time,
                    **meal_plan_slot_api_fields(mp),
                    'photos': [{'id': p.id, 'image_url': p.image_url} for p in photos if p.image_url],
                    'photo_count': len(photos),
                }
            )

        return Response({'results': results}, status=status.HTTP_200_OK)
    
    def _get_meal_plans_with_prefetch(self, meal_plan_ids):
        """Charger les meal plans avec les relations nécessaires pour la sérialisation."""
        if not meal_plan_ids:
            return []
        from django.db.models import Prefetch
        return MealPlan.objects.filter(id__in=meal_plan_ids).select_related(
            'user'
        ).prefetch_related(
            Prefetch('invitations', queryset=MealInvitation.objects.select_related('invitee')),
            Prefetch(
                'meal_plan_recipe_batches',
                queryset=MealPlanRecipeBatch.objects.select_related('recipe_batch__recipe').order_by('order')
            ),
        )
    def create(self, request, *args, **kwargs):
        """
        Créer un ou plusieurs meal plans. Pour chaque recette, un MealPlanRecipeGroup est créé
        automatiquement (même si groupe de 1).
        """
        is_bulk = isinstance(request.data, list)
        
        if is_bulk:
            serializer = self.get_serializer(data=request.data, many=True)
            serializer.is_valid(raise_exception=True)
            with transaction.atomic():
                meal_plans = serializer.save()
            ordered_ids = [meal_plan.id for meal_plan in meal_plans]
            prefetched = list(self._get_meal_plans_with_prefetch(ordered_ids))
            prefetched.sort(key=lambda mp: ordered_ids.index(mp.id))
            response_serializer = self.get_serializer(prefetched, many=True)
            return Response(response_serializer.data, status=status.HTTP_201_CREATED)
        
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        with transaction.atomic():
            meal_plan = serializer.save()
        prefetched = self._get_meal_plans_with_prefetch([meal_plan.id])
        response_serializer = self.get_serializer(prefetched[0])
        headers = self.get_success_headers(response_serializer.data)
        return Response(response_serializer.data, status=status.HTTP_201_CREATED, headers=headers)
    
    def update(self, request, *args, **kwargs):
        """
        Mettre à jour un meal plan. Créer des groupes pour les nouvelles recettes si nécessaire.
        """
        from django.db import transaction
        
        partial = kwargs.pop('partial', False)
        instance = self.get_object()
        serializer = self.get_serializer(instance, data=request.data, partial=partial)
        serializer.is_valid(raise_exception=True)
        
        with transaction.atomic():
            meal_plan = serializer.save()
        
        prefetched = self._get_meal_plans_with_prefetch([meal_plan.id])
        response_serializer = self.get_serializer(prefetched[0])
        return Response(response_serializer.data)

    @action(detail=True, methods=['post'], url_path='add-recipes')
    def add_recipes(self, request, pk=None):
        """
        Ajouter des recettes via batches à un meal plan sans supprimer les existantes.
        - recipe_ids : liste obligatoire (crée un batch par recette)
        - portions par défaut = None (suit le nombre de personnes).
        """
        from recipes.services.meal_plan_service import add_recipes_to_meal_plan

        meal_plan = self.get_object()
        recipe_ids = request.data.get('recipe_ids') or []

        if not isinstance(recipe_ids, list) or len(recipe_ids) == 0:
            return Response({'error': 'recipe_ids must be a non-empty list'}, status=status.HTTP_400_BAD_REQUEST)

        try:
            result = add_recipes_to_meal_plan(request.user, meal_plan.id, recipe_ids)
        except ValueError as exc:
            msg = str(exc)
            status_code = status.HTTP_404_NOT_FOUND if 'introuvable' in msg.lower() else status.HTTP_400_BAD_REQUEST
            return Response({'error': msg}, status=status_code)

        return Response(
            {
                'added_recipe_ids': result.added_recipe_ids,
                'already_present_recipe_ids': result.already_present_recipe_ids,
            },
            status=status.HTTP_200_OK,
        )

    @action(detail=True, methods=['post'], url_path='remove-recipe-batch')
    def remove_recipe_batch(self, request, pk=None):
        """
        Retire ce recipe_batch du repas planifié (supprime la ligne MealPlanRecipeBatch).
        Si le RecipeBatch n'est plus lié à aucun meal plan, le batch est supprimé.
        """
        from recipes.services.meal_plan_service import remove_recipe_from_meal_plan

        meal_plan = self.get_object()
        recipe_batch_id = request.data.get('recipe_batch_id')
        if recipe_batch_id is None:
            return Response({'error': 'recipe_batch_id required'}, status=status.HTTP_400_BAD_REQUEST)
        try:
            recipe_batch_id = int(recipe_batch_id)
        except (TypeError, ValueError):
            return Response({'error': 'Invalid recipe_batch_id'}, status=status.HTTP_400_BAD_REQUEST)

        try:
            result = remove_recipe_from_meal_plan(request.user, meal_plan.id, recipe_batch_id)
        except PermissionError as exc:
            return Response({'detail': str(exc)}, status=status.HTTP_403_FORBIDDEN)
        except ValueError as exc:
            msg = str(exc)
            status_code = status.HTTP_404_NOT_FOUND if 'introuvable' in msg.lower() else status.HTTP_400_BAD_REQUEST
            return Response({'error': msg}, status=status_code)

        return Response(result, status=status.HTTP_200_OK)

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
        
        # Le calcul de total_servings est maintenant fait dans le serializer
        # en sommant les servings de chaque recette (groupée ou non)
        # On n'a plus besoin de pré-calculer ici
        
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
        Retourner les batches cuisinés de l'utilisateur (ancien endpoint meal_plans).
        Un batch est "cuisiné" si is_cooked=True
        """
        from django.core.paginator import Paginator, EmptyPage
        from django.db.models import Prefetch, Q
        
        # Filtrer les batches cuisinés de l'utilisateur :
        # 1. Batches créés par l'utilisateur ET is_cooked=True
        # 2. OU batches liés à des meal plans accessibles ET is_cooked=True
        # Cela garantit que tous les batches cuisinés de l'utilisateur sont retournés,
        # même s'ils ne sont plus liés à un meal plan accessible
        accessible_meal_plan_filter = get_accessible_meal_plan_filter(request.user)
        qs = RecipeBatch.objects.filter(
            recipe__isnull=False
        ).filter(
            (
                # Batches créés par l'utilisateur ET is_cooked=True
                Q(created_by=request.user) & Q(is_cooked=True)
            ) | (
                # OU batches liés à des meal plans accessibles ET is_cooked=True
                Q(meal_plan_recipe_batches__meal_plan__in=MealPlan.objects.filter(
                    accessible_meal_plan_filter
                )) & Q(is_cooked=True)
            )
        ).distinct().select_related('recipe').prefetch_related(
            Prefetch(
                'posts',
                queryset=Post.objects.prefetch_related(
                    Prefetch('photos', queryset=PostPhoto.objects.order_by('order', 'created_at'))
                ).order_by('-created_at'),
            ),
            Prefetch(
                'draft_photos',
                queryset=PostPhoto.objects.filter(is_draft=False).exclude(image_path='').order_by('order', 'created_at'),
            ),
            Prefetch('meal_plan_recipe_batches', queryset=MealPlanRecipeBatch.objects.select_related('meal_plan')),
        ).order_by('-created_at')
        
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
            batches = paginator.page(page)
        except EmptyPage:
            batches = paginator.page(paginator.num_pages)
        
        def _first_photo_url_from_post(post):
            if not post:
                return None
            for ph in post.photos.all():
                url = getattr(ph, 'image_url', None)
                if url:
                    return url
            return None

        def _resolve_batch_cover_photo(batch, posts_list):
            pub = next((p for p in posts_list if p.is_published), None)
            if pub:
                u = _first_photo_url_from_post(pub)
                if u:
                    return u
            for p in posts_list:
                u = _first_photo_url_from_post(p)
                if u:
                    return u
            for ph in batch.draft_photos.all():
                u = getattr(ph, 'image_url', None)
                if u:
                    return u
            if batch.recipe_id and getattr(batch.recipe, 'image_url', None):
                return batch.recipe.image_url
            return None

        results = []
        accessible_meal_plan_filter = get_accessible_meal_plan_filter(request.user)
        for batch in batches:
            # Filtrer les meal plans accessibles par l'utilisateur pour ce batch
            meal_plans = MealPlan.objects.filter(
                meal_plan_recipe_batches__recipe_batch=batch
            ).filter(accessible_meal_plan_filter).distinct()
            grouped_dates = sorted({mp.date.isoformat() for mp in meal_plans})
            meals = []
            breakdown_list = []
            for mp in meal_plans:
                _, breakdown = calculate_meal_plan_servings(mp, include_breakdown=True, recipe_batch_id=batch.id)
                breakdown_list.extend(breakdown)
                meals.append({
                    'id': mp.id,
                    'date': mp.date,
                    'meal_time': mp.meal_time,
                    **meal_plan_slot_api_fields(mp),
                    'is_cooked': batch.is_cooked,
                })
            total_servings = sum(e.get('portions', 0) for e in breakdown_list)
            total_servings_accessible = total_servings

            posts_list = list(batch.posts.all())
            has_published_post = any(p.is_published for p in posts_list)
            photo_url = _resolve_batch_cover_photo(batch, posts_list)

            latest_post = posts_list[0] if posts_list else None
            post_comment = (latest_post.comment or '').strip() if latest_post else ''
            batch_notes = (batch.notes or '').strip()
            user_note = post_comment or batch_notes

            payload = RecipeBatchLightSerializer(batch, context={'request': request}).data
            payload.update({
                'groupedDates': grouped_dates,
                'total_servings_batch': total_servings,
                'total_servings_batch_accessible': total_servings_accessible,
                'meal_plan_ids': [mp.id for mp in meal_plans],
                'meals': meals,
                'is_shared': has_published_post,
                'photo_url': photo_url,
                'user_note': user_note,
            })
            results.append(payload)
        
        return Response({
            'results': results,
            'count': paginator.count,
            'num_pages': paginator.num_pages,
            'current_page': batches.number,
            'has_next': batches.has_next(),
            'has_previous': batches.has_previous(),
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
        
        # Les groupes explicites sont retirés au profit des batches
        # (les agrégations se feront via recipe_batch côté serializers)
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
        
        # Récupérer la recette via le batch
        recipe_batch = meal_plan.meal_plan_recipe_batches.select_related('recipe_batch__recipe').first()
        recipe = recipe_batch.recipe_batch.recipe if recipe_batch and recipe_batch.recipe_batch else None
        if not recipe:
            return Response({'error': 'No recipe found for this meal plan'}, status=status.HTTP_404_NOT_FOUND)
        
        # Charger les steps avec leurs step_ingredients depuis la recette
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
        
        # Récupérer la recette via le batch
        recipe_batch = meal_plan.meal_plan_recipe_batches.select_related('recipe_batch__recipe').first()
        recipe = recipe_batch.recipe_batch.recipe if recipe_batch and recipe_batch.recipe_batch else None
        if not recipe:
            return Response({'error': 'No recipe found for this meal plan'}, status=status.HTTP_404_NOT_FOUND)
        
        # Charger les recipe_ingredients
        from .models import RecipeIngredient
        from django.db.models import Prefetch
        
        ingredients = RecipeIngredient.objects.filter(recipe=recipe).select_related('ingredient')
        
        from .serializers import RecipeIngredientSerializer
        serializer = RecipeIngredientSerializer(ingredients, many=True, context={'request': request})
        return Response(serializer.data)
    
    def perform_create(self, serializer):
        serializer.save(user=self.request.user)

    @action(detail=True, methods=['get'], url_path='dietary-flags')
    def dietary_flags(self, request, pk=None):
        """Réponse légère pour badge planning : conflits goût / allergie / régime (invités avec compte)."""
        from .dietary_filters import meal_plan_dietary_flag_summary

        meal_plan = self.get_object()
        dislike_c, allergy_c, diet_c = meal_plan_dietary_flag_summary(meal_plan, request.user)
        return Response(
            {
                'dislike_conflict': dislike_c,
                'allergy_conflict': allergy_c,
                'diet_conflict': diet_c,
            }
        )

    def get_serializer_context(self):
        context = super().get_serializer_context()
        context['include_shopping_list'] = (
            self.request.query_params.get('include_shopping_list', '').lower() == 'true'
        )
        # Pour la liste, éviter les presigned URLs coûteuses : on renvoie image_url
        if self.action == 'list':
            context['skip_presign'] = True
            # Optim perf: éviter N+1 queries dans groupedDates
            if hasattr(self, '_grouped_dates_by_batch_id'):
                context['grouped_dates_by_batch_id'] = self._grouped_dates_by_batch_id
        return context
    
    def list(self, request, *args, **kwargs):
        """Liste optimisée avec pagination pour les meal plans"""
        queryset = self.filter_queryset(self.get_queryset())
        
        # En mode minimal ou timeline, désactiver la pagination (plage de dates limitée côté client).
        is_minimal = request.query_params.get('minimal', '').lower() == 'true'
        view_mode = (request.query_params.get('view') or '').strip().lower()
        
        if is_minimal or view_mode == 'timeline':
            serializer = self.get_serializer(queryset, many=True)
            return Response(serializer.data)
        
        # Mode complet : utiliser la pagination DRF
        page = self.paginate_queryset(queryset)
        objects = page if page is not None else queryset

        # Pré-calcul groupedDates pour tous les batches retournés (évite N+1 dans serializers)
        batch_ids = set()
        for mp in objects:
            for mprb in mp.meal_plan_recipe_batches.all():
                if mprb.recipe_batch_id:
                    batch_ids.add(mprb.recipe_batch_id)

        self._grouped_dates_by_batch_id = {}
        if batch_ids:
            from collections import defaultdict
            from .models import MealPlanRecipeBatch

            # Une seule requête pour toutes les dates par batch
            rows = MealPlanRecipeBatch.objects.filter(
                recipe_batch_id__in=batch_ids
            ).values_list('recipe_batch_id', 'meal_plan__date', 'meal_plan__meal_time')

            by_batch = defaultdict(set)
            for batch_id, mp_date, mp_meal_time in rows:
                by_batch[batch_id].add((mp_date, mp_meal_time))

            meal_time_rank = {'breakfast': 0, 'lunch': 1, 'dinner': 2, 'other': 3}
            for batch_id, tuples in by_batch.items():
                ordered = sorted(tuples, key=lambda t: (t[0], meal_time_rank.get(t[1], 99)))
                self._grouped_dates_by_batch_id[batch_id] = [d.isoformat() for d, _ in ordered]

        if page is not None:
            serializer = self.get_serializer(objects, many=True)
            return self.get_paginated_response(serializer.data)
        
        # Fallback si pas de pagination
        serializer = self.get_serializer(objects, many=True)
        return Response(serializer.data)
    
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
        
        # Le calcul de total_servings est maintenant fait dans le serializer
        # en sommant les servings de chaque recette (groupée ou non)
        
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

    @action(detail=False, methods=['post'], url_path='create-composer-slot')
    def create_composer_slot_action(self, request):
        """Crée un slot draft pour le composeur de post (date + créneau, conflit → other)."""
        date_str = request.data.get('date')
        meal_time = request.data.get('meal_time') or infer_meal_time_from_hour()
        scheduled_time = request.data.get('scheduled_time')
        try:
            result = create_composer_slot(
                request.user,
                date=date_str,
                meal_time=meal_time,
                scheduled_time=scheduled_time,
            )
        except ValueError as exc:
            return Response({'error': str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        meal_plan = MealPlan.objects.filter(id=result.meal_plan_id).first()
        return Response(
            {
                'meal_plan_id': result.meal_plan_id,
                'date': result.date,
                'meal_time': result.meal_time,
                'slot_key': meal_plan.slot_key if meal_plan else None,
                'scheduled_time': (
                    meal_plan.scheduled_time.strftime('%H:%M') if meal_plan and meal_plan.scheduled_time else None
                ),
                'created': result.created,
                'message': result.message,
            },
            status=status.HTTP_201_CREATED,
        )

    @action(detail=True, methods=['patch'], url_path='update-composer-slot')
    def update_composer_slot_action(self, request, pk=None):
        """Met à jour date/créneau d'un slot draft composeur."""
        meal_plan = self.get_object()
        if meal_plan.user_id != request.user.id:
            return Response({'error': 'Access denied'}, status=status.HTTP_403_FORBIDDEN)
        date_str = request.data.get('date')
        meal_time = request.data.get('meal_time')
        scheduled_time = request.data.get('scheduled_time')
        guest_count = request.data.get('guest_count')
        slot_label = request.data.get('slot_label')
        if not date_str or not meal_time:
            return Response(
                {'error': 'date and meal_time are required'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        try:
            guest_count_val = None
            if guest_count is not None and guest_count != '':
                guest_count_val = int(guest_count)
            result = update_composer_slot(
                request.user,
                meal_plan_id=meal_plan.id,
                date=date_str,
                meal_time=meal_time,
                scheduled_time=scheduled_time,
                guest_count=guest_count_val,
                update_scheduled_time='scheduled_time' in request.data,
                slot_label=slot_label,
            )
        except (ValueError, TypeError) as exc:
            return Response({'error': str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        meal_plan.refresh_from_db()
        return Response(
            {
                'meal_plan_id': result.meal_plan_id,
                'date': result.date,
                'meal_time': result.meal_time,
                'slot_key': meal_plan.slot_key,
                'scheduled_time': (
                    meal_plan.scheduled_time.strftime('%H:%M') if meal_plan.scheduled_time else None
                ),
                'custom_label': meal_plan.custom_label or '',
                'guest_count': meal_plan.guest_count,
                'message': result.message,
            },
            status=status.HTTP_200_OK,
        )

    @action(detail=True, methods=['post'], url_path='publish-post')
    def publish_meal_plan_post(self, request, pk=None):
        """
        Publier un post "du repas" (meal plan).

        - photo_ids: liste ordonnée (max 20)
        - comment: string (optionnel)
        - cooking_time_minutes: int (optionnel)
        - recipe_id: int (optionnel, lie un batch au repas)
        - meal_type: cantine | takeaway | unknown (repas sans recette)
        - custom_label: string (requis si pas de recipe_id)
        """
        meal_plan = self.get_object()
        if meal_plan.user_id != request.user.id:
            return Response({'error': 'Access denied'}, status=status.HTTP_403_FORBIDDEN)

        photo_ids = request.data.get('photo_ids', [])
        comment = request.data.get('comment', '')
        cooking_time_minutes = request.data.get('cooking_time_minutes')
        recipe_id = request.data.get('recipe_id')
        recipe_ids = request.data.get('recipe_ids')
        meal_type = request.data.get('meal_type')
        custom_label = (request.data.get('custom_label') or '').strip()

        if photo_ids is None:
            photo_ids = []
        if not isinstance(photo_ids, list):
            return Response({'error': 'photo_ids must be a list'}, status=status.HTTP_400_BAD_REQUEST)

        if Post.objects.filter(meal_plan=meal_plan, is_published=True).exists():
            return Response({'error': 'A post already exists for this meal plan'}, status=status.HTTP_400_BAD_REQUEST)

        ids_to_link = []
        if recipe_ids is not None:
            if not isinstance(recipe_ids, list):
                return Response({'error': 'recipe_ids must be a list'}, status=status.HTTP_400_BAD_REQUEST)
            try:
                ids_to_link = [int(rid) for rid in recipe_ids if rid is not None]
            except (TypeError, ValueError):
                return Response({'error': 'recipe_ids must contain integers'}, status=status.HTTP_400_BAD_REQUEST)
            if not ids_to_link:
                return Response(
                    {'error': 'recipe_ids must contain at least one valid recipe id'},
                    status=status.HTTP_400_BAD_REQUEST,
                )
        elif recipe_id is not None:
            try:
                ids_to_link = [int(recipe_id)]
            except (TypeError, ValueError):
                return Response({'error': 'recipe_id must be an integer'}, status=status.HTTP_400_BAD_REQUEST)

        if ids_to_link:
            for rid in ids_to_link:
                if not Recipe.objects.filter(id=rid).exists():
                    return Response({'error': f'Recipe {rid} not found'}, status=status.HTTP_404_NOT_FOUND)
            try:
                add_recipes_to_meal_plan(request.user, meal_plan.id, ids_to_link)
            except ValueError as exc:
                return Response({'error': str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        meal_plan.refresh_from_db()
        mprbs = list(meal_plan.meal_plan_recipe_batches.select_related('recipe_batch').all())
        batch_ids = [mprb.recipe_batch_id for mprb in mprbs if mprb.recipe_batch_id]

        if batch_ids:
            meal_plan.meal_type = 'recipe'
            if custom_label and not meal_plan.custom_label:
                meal_plan.custom_label = custom_label
        else:
            allowed_types = {'cantine', 'takeaway', 'unknown'}
            resolved_meal_type = meal_type if meal_type in allowed_types else 'unknown'
            if resolved_meal_type not in allowed_types:
                return Response(
                    {'error': f'meal_type must be one of: {", ".join(sorted(allowed_types))}'},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            if not custom_label:
                return Response(
                    {'error': 'custom_label is required when no recipe_id is provided'},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            meal_plan.meal_type = resolved_meal_type
            meal_plan.custom_label = custom_label

        meal_plan.confirmed = True
        meal_plan.save(update_fields=['meal_type', 'custom_label', 'confirmed', 'updated_at'])

        if ids_to_link and not batch_ids:
            return Response(
                {'error': 'Failed to link recipe to this meal plan'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        photos = []
        if photo_ids:
            try:
                photo_ids = [int(pid) for pid in photo_ids]
            except (TypeError, ValueError):
                return Response({'error': 'photo_ids must contain integers'}, status=status.HTTP_400_BAD_REQUEST)

            if len(photo_ids) > 20:
                return Response({'error': 'You can select up to 20 photos'}, status=status.HTTP_400_BAD_REQUEST)

            relink_composer_photos_to_meal_plan(request.user, meal_plan, photo_ids)

            photo_filter = Q(meal_plan_id=meal_plan.id)
            if batch_ids:
                photo_filter |= Q(recipe_batch_id__in=batch_ids)

            photos_qs = PostPhoto.objects.filter(
                id__in=photo_ids,
                is_draft=False,
            ).filter(photo_filter)
            photos_dict = {p.id: p for p in photos_qs}
            if len(photos_dict) != len(photo_ids):
                return Response({'error': 'Some photos are invalid or do not belong to this meal plan'}, status=status.HTTP_400_BAD_REQUEST)

            photos = [photos_dict[pid] for pid in photo_ids]
        else:
            return Response({'error': 'At least one photo is required'}, status=status.HTTP_400_BAD_REQUEST)

        cover_batch_id = batch_ids[0] if batch_ids else None

        if batch_ids:
            batches = RecipeBatch.objects.filter(id__in=batch_ids).select_related('recipe')
            for batch in batches:
                complete_recipe_batch_workflow(request.user, batch)

        cooking_time_value = None
        if cooking_time_minutes is not None and cooking_time_minutes != '':
            try:
                cooking_time_value = max(0, int(cooking_time_minutes))
            except (TypeError, ValueError):
                return Response({'error': 'cooking_time_minutes must be an integer'}, status=status.HTTP_400_BAD_REQUEST)

        post = Post.objects.create(
            user=request.user,
            meal_plan=meal_plan,
            recipe_batch_id=cover_batch_id,
            comment=comment,
            cooking_time_minutes=cooking_time_value,
            is_published=True,
        )

        for order_index, photo in enumerate(photos, start=1):
            photo.post = post
            photo.order = order_index
            photo.save(update_fields=['post', 'order'])

        return Response(
            {
                'id': post.id,
                'meal_plan_id': meal_plan.id,
                'comment': post.comment,
                'is_published': post.is_published,
                'created_at': post.created_at,
                'photo_ids': photo_ids,
                'message': 'Post créé avec succès',
            },
            status=status.HTTP_201_CREATED,
        )

    @action(detail=True, methods=['get'], url_path='published-post')
    def published_meal_plan_post(self, request, pk=None):
        """Récupérer le post publié associé à ce meal plan (workflow "post du repas")."""
        meal_plan = self.get_object()
        post = Post.objects.filter(meal_plan=meal_plan, is_published=True).first()
        if not post:
            return Response({}, status=status.HTTP_200_OK)
        from .serializers import PostSerializer
        serializer = PostSerializer(post, context={'request': request})
        return Response(serializer.data, status=status.HTTP_200_OK)

    @action(detail=True, methods=['post'], url_path='assign-temp-photos')
    def assign_temp_photos(self, request, pk=None):
        """
        Assigner des photos temporaires (uploadées sur le meal plan) à des recipe_batches du meal plan.

        Payload:
        - assignments: [{ photo_id, recipe_batch_id }]
        """
        meal_plan = self.get_object()
        assignments = request.data.get('assignments', [])
        if not isinstance(assignments, list) or len(assignments) == 0:
            return Response({'error': 'assignments must be a non-empty list'}, status=status.HTTP_400_BAD_REQUEST)

        # batches du meal plan
        batch_ids = list(
            meal_plan.meal_plan_recipe_batches.values_list('recipe_batch_id', flat=True)
        )
        batch_ids = [bid for bid in batch_ids if bid]
        if not batch_ids:
            return Response({'error': 'This meal plan has no recipe batches'}, status=status.HTTP_400_BAD_REQUEST)

        # valider mapping
        photo_ids = []
        mapping = {}
        for a in assignments:
            try:
                pid = int(a.get('photo_id'))
                bid = int(a.get('recipe_batch_id'))
            except Exception:
                return Response({'error': 'photo_id and recipe_batch_id must be integers'}, status=status.HTTP_400_BAD_REQUEST)
            if bid not in batch_ids:
                return Response({'error': 'Invalid recipe_batch_id for this meal plan'}, status=status.HTTP_400_BAD_REQUEST)
            photo_ids.append(pid)
            mapping[pid] = bid

        qs = PostPhoto.objects.filter(
            id__in=photo_ids,
            meal_plan=meal_plan,
            post__isnull=True,
            is_draft=False,
        )
        found = list(qs)
        if len(found) != len(set(photo_ids)):
            return Response({'error': 'Some photos are invalid or not temporary photos of this meal plan'}, status=status.HTTP_400_BAD_REQUEST)

        updated = []
        for p in found:
            bid = mapping.get(p.id)
            if not bid:
                continue
            p.recipe_batch_id = bid
            # IMPORTANT: on garde meal_plan_id pour pouvoir réassigner/supprimer
            # les photos via le meal plan même après association à une recette.
            p.save(update_fields=['recipe_batch_id'])
            updated.append(p.id)

        return Response({'ok': True, 'updated_photo_ids': updated}, status=status.HTTP_200_OK)

    @action(detail=True, methods=['post'], url_path='reassign-photos')
    def reassign_photos(self, request, pk=None):
        """
        Réassigner des photos d'un meal plan entre recettes (ou désassocier).

        Payload:
        - assignments: [{ photo_id, recipe_batch_id }] où recipe_batch_id peut être null pour "Non associées"
        """
        meal_plan = self.get_object()
        assignments = request.data.get('assignments', [])
        if not isinstance(assignments, list) or len(assignments) == 0:
            return Response({'error': 'assignments must be a non-empty list'}, status=status.HTTP_400_BAD_REQUEST)

        batch_ids = list(
            meal_plan.meal_plan_recipe_batches.values_list('recipe_batch_id', flat=True)
        )
        batch_ids = [bid for bid in batch_ids if bid]

        photo_ids = []
        mapping = {}
        for a in assignments:
            try:
                pid = int(a.get('photo_id'))
            except Exception:
                return Response({'error': 'photo_id must be an integer'}, status=status.HTTP_400_BAD_REQUEST)
            bid_raw = a.get('recipe_batch_id', None)
            bid = None
            if bid_raw not in [None, '', 'null']:
                try:
                    bid = int(bid_raw)
                except Exception:
                    return Response({'error': 'recipe_batch_id must be an integer or null'}, status=status.HTTP_400_BAD_REQUEST)
                if bid not in batch_ids:
                    return Response({'error': 'Invalid recipe_batch_id for this meal plan'}, status=status.HTTP_400_BAD_REQUEST)
            photo_ids.append(pid)
            mapping[pid] = bid

        # Compat: certaines photos associées à un batch ont historiquement perdu meal_plan_id,
        # ou ont un meal_plan_id incohérent alors que recipe_batch_id pointe bien vers ce repas.
        # On accepte:
        # - les photos dont meal_plan = ce meal plan
        # - OU toute photo dont recipe_batch_id est l’un des batches de ce meal plan
        #   (évite le 400 quand meal_plan est renseigné à tort mais le batch est le bon)
        from django.db.models import Q
        qs = PostPhoto.objects.filter(
            id__in=photo_ids,
            post__isnull=True,
            is_draft=False,
        ).filter(
            Q(meal_plan=meal_plan) |
            Q(recipe_batch_id__in=batch_ids)
        )
        found = list(qs)
        if len(found) != len(set(photo_ids)):
            return Response({'error': 'Some photos are invalid for this meal plan'}, status=status.HTTP_400_BAD_REQUEST)

        updated = []
        for p in found:
            p.recipe_batch_id = mapping.get(p.id, None)
            # Normaliser: garder meal_plan_id pour les photos gérées dans ce workflow.
            if p.meal_plan_id is None:
                p.meal_plan_id = meal_plan.id
                p.save(update_fields=['recipe_batch_id', 'meal_plan_id'])
            else:
                p.save(update_fields=['recipe_batch_id'])
            updated.append(p.id)

        # Mapping compact pour le front (photo_id -> recipe_batch_id, null si ambiance générale)
        photos_mapping = [{'photo_id': pid, 'recipe_batch_id': mapping.get(pid)} for pid in photo_ids]
        return Response(
            {'ok': True, 'updated_photo_ids': updated, 'photos_mapping': photos_mapping},
            status=status.HTTP_200_OK
        )

    @action(detail=True, methods=['delete'], url_path='delete-photo')
    def delete_meal_plan_photo(self, request, pk=None):
        """
        Supprimer une photo d'un meal plan (qu'elle soit associée à une recette ou non),
        tant qu'elle n'est pas attachée à un post.
        """
        meal_plan = self.get_object()
        photo_id = request.query_params.get('photo_id')
        try:
            photo_id = int(photo_id)
        except Exception:
            return Response({'error': 'photo_id is required'}, status=status.HTTP_400_BAD_REQUEST)

        try:
            photo = PostPhoto.objects.get(id=photo_id, meal_plan=meal_plan)
        except PostPhoto.DoesNotExist:
            return Response({'error': 'Photo not found'}, status=status.HTTP_404_NOT_FOUND)

        if photo.post_id is not None:
            return Response({'error': 'Cannot delete a photo already attached to a post'}, status=status.HTTP_400_BAD_REQUEST)

        photo.delete()
        return Response({'ok': True}, status=status.HTTP_200_OK)

    @action(detail=True, methods=['delete'], url_path='delete-temp-photo')
    def delete_temp_photo(self, request, pk=None):
        """Supprimer une photo temporaire (meal_plan) non rattachée à un batch."""
        meal_plan = self.get_object()
        photo_id = request.query_params.get('photo_id')
        try:
            photo_id = int(photo_id)
        except Exception:
            return Response({'error': 'photo_id is required'}, status=status.HTTP_400_BAD_REQUEST)

        try:
            photo = PostPhoto.objects.get(id=photo_id, meal_plan=meal_plan)
        except PostPhoto.DoesNotExist:
            return Response({'error': 'Photo not found'}, status=status.HTTP_404_NOT_FOUND)

        if photo.post_id is not None:
            return Response({'error': 'Cannot delete a photo already attached to a post'}, status=status.HTTP_400_BAD_REQUEST)

        # réutiliser la logique delete S3 de recipe-batches/delete-photo (simple: delete DB only here)
        # Le nettoyage S3 est best-effort; on peut l'ajouter plus tard si nécessaire.
        photo.delete()
        return Response({'ok': True}, status=status.HTTP_200_OK)

    @action(detail=True, methods=['get'], url_path='temp-photos')
    def temp_photos(self, request, pk=None):
        """
        Lister les photos temporaires "à table" d'un meal plan (pas encore rattachées à un batch).

        Retourne la même forme que /recipe-batches/{id}/photos/ (serializer light).
        """
        meal_plan = self.get_object()
        photos = (
            PostPhoto.objects.filter(
                meal_plan=meal_plan,
                recipe_batch_id__isnull=True,
                post__isnull=True,
                is_draft=False,
            )
            .select_related('step')
            .order_by('-created_at')
        )
        from .serializers import PostPhotoLightSerializer
        serializer = PostPhotoLightSerializer(photos, many=True, context={'request': request})
        return Response(serializer.data, status=status.HTTP_200_OK)
    
    @action(detail=False, methods=['get'])
    def shared_with_me(self, request):
        """Récupérer les repas partagés avec l'utilisateur connecté"""
        invitations = MealInvitation.objects.filter(invitee=request.user, status='accepted').select_related('meal_plan', 'meal_plan__user')
        meal_plans = [inv.meal_plan for inv in invitations]
        serializer = self.get_serializer(meal_plans, many=True)
        return Response(serializer.data)

    @action(detail=False, methods=['get'], url_path='recent-photo-candidates')
    def recent_photo_candidates(self, request):
        """
        Suggestions pour attribuer une photo « à table » à un repas récent.

        Retourne des candidats basés sur les meal plans accessibles de l'utilisateur, dans le passé proche,
        et uniquement pour des batches créés par l'utilisateur (cohérent avec l'UX « reprendre mon process »).
        """
        try:
            days = int(request.query_params.get('days', 7))
        except (TypeError, ValueError):
            days = 7
        days = max(1, min(days, 30))

        now = timezone.localtime(timezone.now())
        recent_cutoff = now - timedelta(hours=24)

        today = timezone.localdate()
        start_date = today - timedelta(days=days)

        # Approximation hour per slot. (UX: on veut filtrer sur "dernier 24h" côté back.)
        slot_hours = {
            'breakfast': 9,
            'lunch': 12,
            'dinner': 20,
        }

        accessible_meal_plan_filter = get_accessible_meal_plan_filter(request.user)
        mprbs = (
            MealPlanRecipeBatch.objects.filter(
                meal_plan__in=MealPlan.objects.filter(accessible_meal_plan_filter),
                meal_plan__date__gte=start_date,
                meal_plan__date__lte=today,
                recipe_batch__created_by=request.user,
            )
            .select_related('meal_plan', 'recipe_batch__recipe')
            .order_by('-meal_plan__date', '-meal_plan__id', 'order')
        )

        # Exclure complètement les meal plans qui ont déjà un post publié PAR l'utilisateur courant
        # (sur n'importe quel batch du repas).
        # Produit: si *moi* j'ai déjà publié ce repas, je ne dois plus le voir dans la liste.
        meal_plan_ids = list({mprb.meal_plan_id for mprb in mprbs})
        published_meal_plan_ids = set()
        if meal_plan_ids:
            published_meal_plan_ids = set(
                MealPlan.objects.filter(id__in=meal_plan_ids)
                .filter(meal_plan_recipe_batches__recipe_batch__posts__is_published=True)
                .filter(meal_plan_recipe_batches__recipe_batch__posts__user=request.user)
                .values_list('id', flat=True)
                .distinct()
            )

        items = []
        for mprb in mprbs:
            mp = mprb.meal_plan
            if mp.id in published_meal_plan_ids:
                continue
            try:
                hour = slot_hours.get(getattr(mp, 'meal_time', None), 12)
                mp_dt = timezone.make_aware(
                    datetime.combine(mp.date, time(hour=hour, minute=0, second=0)),
                    timezone.get_current_timezone(),
                )
                mp_dt = timezone.localtime(mp_dt)
            except Exception:
                mp_dt = None

            # Filtre produit: meal plan "récent" (< 24h) + jamais dans le futur.
            if mp_dt is None:
                continue
            if mp_dt > now:
                continue
            if mp_dt < recent_cutoff:
                continue

            batch = mprb.recipe_batch
            recipe = getattr(batch, 'recipe', None)
            items.append(
                {
                    'meal_plan_id': mp.id,
                    'date': mp.date,
                    'meal_time': mp.meal_time,
                    **meal_plan_slot_api_fields(mp),
                    'recipe_batch_id': batch.id,
                    'recipe': {
                        'id': getattr(recipe, 'id', None),
                        'title': getattr(recipe, 'title', None),
                        'image_url': getattr(recipe, 'image_url', None),
                    }
                    if recipe
                    else None,
                }
            )

        return Response({'results': items}, status=status.HTTP_200_OK)

    @action(detail=True, methods=['get'])
    def photos(self, request, pk=None):
        """Galerie de photos associées au batch (via meal_plan -> recipe_batches)"""
        meal_plan = self.get_object()
        batch_ids = list(meal_plan.meal_plan_recipe_batches.values_list('recipe_batch_id', flat=True))
        photos = PostPhoto.objects.filter(recipe_batch_id__in=batch_ids).select_related('step')
        from .serializers import PostPhotoLightSerializer
        serializer = PostPhotoLightSerializer(photos, many=True, context={'request': request})
        return Response(serializer.data)
    
    @action(detail=True, methods=['get'], url_path='published-post')
    def published_post(self, request, pk=None):
        """Récupérer le post publié associé à ce meal_plan"""
        meal_plan = self.get_object()
        try:
            batch_ids = list(meal_plan.meal_plan_recipe_batches.values_list('recipe_batch_id', flat=True))
            post = Post.objects.filter(recipe_batch_id__in=batch_ids, is_published=True).first()
            if post:
                from .serializers import PostSerializer
                serializer = PostSerializer(post, context={'request': request})
                return Response(serializer.data)
            else:
                return Response({'exists': False}, status=status.HTTP_200_OK)
        except Exception as e:
            return Response({'error': str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    @action(detail=False, methods=['post'], url_path='create-batch-and-associate')
    def create_batch_and_associate(self, request):
        """
        Créer un batch unique pour une recette et l'associer à plusieurs meal plans.
        
        Payload attendu:
        {
            "recipe_id": 123,
            "dates": [
                {"date": "2025-12-15", "meal_time": "lunch"},
                {"date": "2025-12-16", "meal_time": "dinner"},
                ...
            ],
            "ratio": 1.0  # optionnel, défaut 1.0
        }
        """
        from django.db import transaction
        from decimal import Decimal
        
        recipe_id = request.data.get('recipe_id')
        dates = request.data.get('dates', [])
        ratio = request.data.get('ratio', 1.0)
        
        if not recipe_id:
            return Response(
                {'error': 'recipe_id is required'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        if not dates or not isinstance(dates, list):
            return Response(
                {'error': 'dates must be a non-empty list'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        try:
            recipe = Recipe.objects.get(id=recipe_id)
        except Recipe.DoesNotExist:
            return Response(
                {'error': 'Recipe not found'},
                status=status.HTTP_404_NOT_FOUND
            )
        
        portions = request.data.get('portions')
        if portions is not None:
            try:
                portions = int(portions)
                portions = max(0, portions)
            except (ValueError, TypeError):
                portions = None
        
        created_meal_plans = []
        valid_meal_times = {c[0] for c in MealPlan.MEAL_TIME_CHOICES}

        def _parse_scheduled_time(val):
            if not val:
                return None
            if isinstance(val, str):
                for fmt in ('%H:%M:%S', '%H:%M'):
                    try:
                        return datetime.strptime(val.strip(), fmt).time()
                    except ValueError:
                        continue
            return None

        with transaction.atomic():
            batch = RecipeBatch.objects.create(
                recipe=recipe,
                created_by=request.user
            )

            for date_item in dates:
                date_key = date_item.get('date')
                meal_time = date_item.get('meal_time')
                if not date_key or not meal_time or meal_time not in valid_meal_times:
                    continue
                try:
                    target_date = datetime.strptime(date_key, '%Y-%m-%d').date()
                except ValueError:
                    continue

                slot_key = (date_item.get('slot_key') or '').strip()
                custom_label = (date_item.get('custom_label') or '').strip()
                scheduled_time = _parse_scheduled_time(date_item.get('scheduled_time'))

                if meal_time == 'other':
                    if not slot_key or not custom_label:
                        continue
                else:
                    slot_key = meal_time

                existing_meal_plan = MealPlan.objects.filter(
                    user=request.user,
                    date=target_date,
                    slot_key=slot_key,
                ).first()

                if existing_meal_plan:
                    if not existing_meal_plan.meal_plan_recipe_batches.filter(recipe_batch=batch).exists():
                        MealPlanRecipeBatch.objects.create(
                            meal_plan=existing_meal_plan,
                            recipe_batch=batch,
                            portions=portions,
                            is_portions_overridden=portions is not None,
                            order=existing_meal_plan.meal_plan_recipe_batches.count()
                        )
                    meal_plan = existing_meal_plan
                else:
                    meal_plan = MealPlan.objects.create(
                        user=request.user,
                        date=target_date,
                        meal_time=meal_time,
                        slot_key=slot_key,
                        custom_label=custom_label if meal_time == 'other' else '',
                        scheduled_time=scheduled_time,
                        meal_type='recipe',
                        confirmed=False,
                    )
                    MealPlanRecipeBatch.objects.create(
                        meal_plan=meal_plan,
                        recipe_batch=batch,
                        portions=portions,
                        is_portions_overridden=portions is not None,
                        order=0
                    )

                created_meal_plans.append(meal_plan)
        
        # Précharger les relations nécessaires pour le serializer
        from django.db.models import Prefetch
        created_meal_plans = MealPlan.objects.filter(
            id__in=[mp.id for mp in created_meal_plans]
        ).prefetch_related(
            Prefetch('meal_plan_recipe_batches', queryset=MealPlanRecipeBatch.objects.select_related('recipe_batch__recipe').order_by('order')),
            Prefetch('invitations', queryset=MealInvitation.objects.select_related('invitee')),
        )
        
        # Sérialiser les meal plans créés/mis à jour
        serializer = self.get_serializer(created_meal_plans, many=True)
        return Response({
            'batch_id': batch.id,
            'meal_plans': serializer.data
        }, status=status.HTTP_201_CREATED)
    
    @action(detail=True, methods=['post'], url_path='apply-to-dates')
    def apply_to_dates(self, request, pk=None):
        """Appliquer un meal plan à plusieurs dates (batches)."""
        from django.db import transaction
        from decimal import Decimal
        
        source_meal_plan = self.get_object()
        
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
            source_recipes = source_meal_plan.meal_plan_recipe_batches.all().select_related('recipe_batch__recipe')
            recipe_data = [(mpr.recipe_batch_id, mpr.recipe_batch.recipe_id, mpr.portions, mpr.is_portions_overridden, mpr.order) for mpr in source_recipes]
            
            for date_key in date_keys:
                try:
                    target_date = datetime.strptime(date_key, '%Y-%m-%d').date()
                except ValueError:
                    continue
                
                existing_meal_plan = MealPlan.objects.filter(
                    user=request.user,
                    date=target_date,
                    slot_key=source_meal_plan.slot_key,
                ).first()
                
                if existing_meal_plan:
                    existing_meal_plan.meal_plan_recipe_batches.all().delete()
                    meal_plan = existing_meal_plan
                else:
                    meal_plan = MealPlan.objects.create(
                        user=request.user,
                        date=target_date,
                        meal_time=source_meal_plan.meal_time,
                        slot_key=source_meal_plan.slot_key,
                        custom_label=source_meal_plan.custom_label,
                        scheduled_time=source_meal_plan.scheduled_time,
                        meal_type=source_meal_plan.meal_type,
                        confirmed=source_meal_plan.confirmed,
                    )
                
                for batch_id, recipe_id, portions, is_overridden, order in recipe_data:
                    batch = RecipeBatch.objects.get(id=batch_id) if batch_id else RecipeBatch.objects.create(recipe_id=recipe_id, created_by=request.user)
                    MealPlanRecipeBatch.objects.create(
                        meal_plan=meal_plan,
                        recipe_batch=batch,
                        portions=portions,
                        is_portions_overridden=is_overridden,
                        order=order
                    )
                
                created_meal_plans.append(meal_plan)
        
        # Sérialiser les meal plans créés
        serializer = self.get_serializer(created_meal_plans, many=True)
        return Response(serializer.data, status=status.HTTP_201_CREATED)
    
    @action(detail=True, methods=['post'], url_path='remove-from-group')
    def remove_from_group(self, request, pk=None):
        """Legacy group removal not supported after batch refactor."""
        return Response({'detail': 'Grouping moved to recipe batches'}, status=status.HTTP_410_GONE)
    
    @action(detail=True, methods=['post'])
    def invite(self, request, pk=None):
        """Inviter des utilisateurs à un repas"""
        from recipes.services.invitation_service import execute_meal_invitation

        meal_plan = self.get_object()
        invitee_ids = request.data.get('invitee_ids', [])

        if not invitee_ids:
            return Response({'error': 'invitee_ids is required'}, status=status.HTTP_400_BAD_REQUEST)

        try:
            result = execute_meal_invitation(
                request.user,
                {'meal_plan_id': meal_plan.id, 'invitee_ids': invitee_ids},
            )
        except ValueError as exc:
            return Response({'error': str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        meal_plan.refresh_from_db()
        from .serializers import MealPlanSerializer
        meal_plan_serializer = MealPlanSerializer(meal_plan, context={'request': request})

        new_invitations = MealInvitation.objects.filter(
            meal_plan=meal_plan,
            invitee_id__in=invitee_ids,
            status='pending',
        ).order_by('-created_at')
        serializer = MealInvitationSerializer(new_invitations, many=True, context={'request': request})
        return Response({
            'invitations': serializer.data,
            'meal_plan': meal_plan_serializer.data,
            'result': result,
        }, status=status.HTTP_201_CREATED)


class MealInvitationViewSet(viewsets.ModelViewSet):
    """ViewSet pour les invitations à des repas"""
    serializer_class = MealInvitationSerializer
    permission_classes = [IsAuthenticated]

    def get_serializer_class(self):
        if self.action == 'list':
            view_mode = (self.request.query_params.get('view') or '').strip().lower()
            if view_mode == 'timeline':
                from .serializers import MealInvitationTimelineListSerializer
                return MealInvitationTimelineListSerializer
            return MealInvitationListSerializer
        return MealInvitationSerializer

    def get_queryset(self):
        """
        Par défaut, on retourne uniquement les invitations **reçues** (où l'utilisateur est l'invité).
        Objectif: l'écran timeline/notifications ne doit pas mélanger les invitations des repas
        dont l'utilisateur est l'hôte.

        Pour les usages hôte (gestion des invitations envoyées), on peut demander explicitement
        `scope=host` (ou `scope=all`).

        On expose quelques filtres pour alléger les réponses côté frontend :
        - status : filtrer par statut (ex: pending)
        - date__gte / date__lte : filtrer par plage de dates sur meal_plan.date
        - meal_plan : filtrer sur un meal_plan précis
        """
        params = self.request.query_params
        scope = (params.get('scope') or 'invitee').strip().lower()
        user = self.request.user

        # Cas "gestion participants" : quand on cible un meal plan précis, on doit pouvoir
        # récupérer TOUTES les invitations de ce meal plan, à condition que l'utilisateur
        # ait accès à ce meal plan (hôte ou invité accepté).
        meal_plan_id = params.get('meal_plan')
        if meal_plan_id:
            try:
                mp_id = int(meal_plan_id)
            except (TypeError, ValueError):
                mp_id = None

            if mp_id is None:
                return MealInvitation.objects.none()

            accessible_filter = get_accessible_meal_plan_filter(user)
            if not MealPlan.objects.filter(id=mp_id).filter(accessible_filter).exists():
                return MealInvitation.objects.none()

            qs = MealInvitation.objects.filter(meal_plan_id=mp_id)
            qs = qs.select_related('inviter', 'invitee', 'meal_plan', 'meal_plan__user')

            status_param = params.get('status')
            if status_param:
                qs = qs.filter(status=status_param)
            return qs

        if scope == 'host':
            qs = MealInvitation.objects.filter(inviter=user)
        elif scope == 'all':
            qs = MealInvitation.objects.filter(Q(inviter=user) | Q(invitee=user))
        else:
            # default: invitations reçues
            qs = MealInvitation.objects.filter(invitee=user)

        # Pour les actions "objet" (retrieve/destroy/...), on élargit aux invitations des meal plans accessibles,
        # sinon un utilisateur invité accepté ne peut pas supprimer une invitation liée au même repas.
        if self.action in ('retrieve', 'destroy', 'update', 'partial_update'):
            accessible_filter = get_accessible_meal_plan_filter(user)
            qs = MealInvitation.objects.filter(meal_plan__in=MealPlan.objects.filter(accessible_filter))

        qs = qs.select_related('inviter', 'invitee', 'meal_plan', 'meal_plan__user')

        # Timeline: on veut 0-2 thumbs de recettes sans charger tout le meal plan
        view_mode = (params.get('view') or '').strip().lower()
        if view_mode == 'timeline':
            from django.db.models import Prefetch
            from .models import MealPlanRecipeBatch
            qs = qs.prefetch_related(
                Prefetch(
                    'meal_plan__meal_plan_recipe_batches',
                    queryset=MealPlanRecipeBatch.objects.select_related('recipe_batch__recipe').order_by('order'),
                )
            )
        
        status_param = params.get('status')
        if status_param:
          qs = qs.filter(status=status_param)
        
        date_gte = params.get('date__gte')
        if date_gte:
            qs = qs.filter(meal_plan__date__gte=date_gte)
        
        date_lte = params.get('date__lte')
        if date_lte:
            qs = qs.filter(meal_plan__date__lte=date_lte)
        
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
        
        # Ne plus créer de meal plan pour l'invité - l'invitation acceptée est la source de vérité
        # L'invité verra le meal plan dans son calendrier via le champ is_guest du serializer
        
        meal_plan = invitation.meal_plan
        
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
    
    @action(detail=True, methods=['post'])
    def cancel(self, request, pk=None):
        """
        Annuler une invitation déjà acceptée par l'utilisateur invité.
        
        Concrètement, on passe le statut de 'accepted' à 'declined', ce qui
        retire l'accès au meal plan partagé via get_accessible_meal_plan_filter.
        """
        invitation = self.get_object()
        
        if invitation.invitee != request.user:
            return Response({'error': 'You can only cancel invitations sent to you'}, status=status.HTTP_403_FORBIDDEN)
        
        if invitation.status != 'accepted':
            return Response({'error': 'Only accepted invitations can be cancelled'}, status=status.HTTP_400_BAD_REQUEST)
        
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
        ).select_related('inviter', 'meal_plan', 'meal_plan__user')

        # Optionnellement filtrer par plage de dates (sur meal_plan.date)
        date_gte = request.query_params.get('date__gte')
        date_lte = request.query_params.get('date__lte')
        if date_gte:
            invitations = invitations.filter(meal_plan__date__gte=date_gte)
        if date_lte:
            invitations = invitations.filter(meal_plan__date__lte=date_lte)

        serializer = self.get_serializer(invitations, many=True)
        return Response(serializer.data)


class CookingProgressViewSet(viewsets.ModelViewSet):
    """ViewSet pour la progression de cuisson"""
    permission_classes = [IsAuthenticated]
    
    def get_queryset(self):
        queryset = CookingProgress.objects.filter(user=self.request.user)
        
        status_filter = self.request.query_params.get('status', None)
        if status_filter:
            queryset = queryset.filter(status=status_filter)
        
        batch_id = self.request.query_params.get('recipe_batch', None)
        if batch_id:
            queryset = queryset.filter(recipe_batch_id=batch_id)
        
        return queryset.select_related('recipe_batch').order_by('-updated_at')
    
    def get_serializer_class(self):
        if self.action in ['create', 'update', 'partial_update']:
            return CookingProgressCreateUpdateSerializer
        return CookingProgressSerializer
    
    def create(self, request, *args, **kwargs):
        """Override create pour gérer le get_or_create"""
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        
        validated_data = serializer.validated_data
        recipe_batch = validated_data.get('recipe_batch')
        
        # Chercher une progression existante en cours
        existing_progress = CookingProgress.objects.filter(
            user=request.user,
            recipe_batch=recipe_batch,
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
        """Récupérer la progression en cours pour un batch"""
        batch_id = request.query_params.get('recipe_batch')
        
        if not batch_id:
            return Response({'error': 'recipe_batch parameter is required'}, status=status.HTTP_400_BAD_REQUEST)
        
        progress = CookingProgress.objects.filter(
            user=request.user,
            recipe_batch_id=batch_id,
            status='in_progress'
        ).first()
        
        if progress:
            serializer = self.get_serializer(progress)
            return Response(serializer.data)
        else:
            # Retourner un objet vide plutôt que None pour éviter les problèmes de parsing JSON
            return Response({}, status=status.HTTP_200_OK)
    
    @action(detail=True, methods=['post'])
    def complete(self, request, pk=None):
        """Marquer une progression comme terminée"""
        progress = self.get_object()
        progress.complete()
        serializer = self.get_serializer(progress)
        return Response(serializer.data)
    
    @action(detail=True, methods=['post'])
    def abandon(self, request, pk=None):
        """Marquer une progression comme abandonnée"""
        progress = self.get_object()
        progress.status = 'abandoned'
        progress.save()
        serializer = self.get_serializer(progress)
        return Response(serializer.data)


class TimerViewSet(viewsets.ModelViewSet):
    """ViewSet pour les minuteurs actifs"""
    permission_classes = [IsAuthenticated]
    
    def get_queryset(self):
        from django.utils import timezone
        from datetime import timedelta
        # Inclure les timers actifs ou expirés depuis moins d'1 heure
        now = timezone.now()
        one_hour_ago = now - timedelta(hours=1)
        queryset = Timer.objects.filter(
            user=self.request.user,
            is_completed=False,
            expires_at__gte=one_hour_ago
        )
        return queryset.select_related('recipe_batch', 'step', 'cooking_progress').order_by('expires_at')
    
    def get_serializer_class(self):
        if self.action in ['create']:
            return TimerCreateSerializer
        return TimerSerializer
    
    def perform_create(self, serializer):
        from django.utils import timezone
        from datetime import timedelta

        timer = serializer.save(user=self.request.user)

        # Planifier l'envoi de la notification push "presque terminé"
        expires_at = timer.expires_at
        now = timezone.now()
        if expires_at and expires_at > now:
            almost_eta = expires_at - timedelta(seconds=3)
            if almost_eta > now:
                send_timer_almost_finished_push.apply_async(args=[timer.id], eta=almost_eta)
    
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
        if photo.uploaded_by_id and photo.uploaded_by_id != user.id:
            return False
        if photo.recipe_batch_id and photo.recipe_batch:
            accessible_meal_plan_filter = get_accessible_meal_plan_filter(user)
            return RecipeBatch.objects.filter(
                id=photo.recipe_batch_id,
                meal_plan_recipe_batches__meal_plan__in=MealPlan.objects.filter(
                    accessible_meal_plan_filter
                )
            ).exists()
        if photo.post_id and photo.post:
            return photo.post.user_id == user.id
        return False
    
    def get_queryset(self):
        # Pour retrieve (GET /posts/{id}/), autoriser tout post publié (ex: depuis une notification)
        if self.action == 'retrieve':
            return Post.objects.filter(is_published=True).select_related(
                'user', 'recipe_batch', 'recipe_batch__recipe', 'meal_plan'
            ).prefetch_related(
                Prefetch(
                    'photos',
                    queryset=PostPhoto.objects.select_related('recipe_batch__recipe').order_by('order'),
                ),
                Prefetch(
                    'meal_plan__meal_plan_recipe_batches',
                    queryset=MealPlanRecipeBatch.objects.select_related('recipe_batch__recipe').order_by('order'),
                ),
                'cookies',
                'comments',
            ).order_by('-created_at')

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
        
        # Filtrer par batch (nouveau modèle) ou par recette via le batch
        recipe_batch_id = self.request.query_params.get('recipe_batch')
        if recipe_batch_id:
            queryset = queryset.filter(recipe_batch_id=recipe_batch_id)

        recipe_id = self.request.query_params.get('recipe')
        if recipe_id:
            queryset = queryset.filter(recipe_batch__recipe_id=recipe_id)
        
        # Filtrer par utilisateur si fourni
        user_id = self.request.query_params.get('user')
        if user_id:
            try:
                queryset = queryset.filter(user_id=int(user_id))
            except (ValueError, TypeError):
                pass
        
        # Optimisation : pour les listes, limiter les champs chargés
        if self.action == 'list':
            from django.db.models import Exists, OuterRef, Prefetch
            queryset = queryset.select_related(
                'user',
                'recipe_batch',
                'recipe_batch__recipe',
                'meal_plan',
            ).prefetch_related(
                'photos', 'cookies', 'comments',
                Prefetch(
                    'recipe_batch__meal_plan_recipe_batches',
                    queryset=MealPlanRecipeBatch.objects.select_related('meal_plan').only('meal_plan_id', 'meal_plan__date', 'meal_plan__meal_time')
                )
            )
            if self.request.user.is_authenticated:
                follow_exists = Follow.objects.filter(
                    follower_id=self.request.user.id,
                    following_id=OuterRef('user_id'),
                )
                queryset = queryset.annotate(_viewer_follows_post_author=Exists(follow_exists))
            queryset = queryset.order_by('-created_at')
        else:
            queryset = queryset.select_related(
                'user',
                'recipe_batch',
                'recipe_batch__recipe',
                'meal_plan',
            ).prefetch_related('photos', 'cookies', 'comments').order_by('-created_at')
        
        return queryset
    
    def list(self, request, *args, **kwargs):
        """Liste optimisée des posts avec pagination"""
        logger = logging.getLogger(__name__)
        t0 = perf_counter()
        queryset = self.filter_queryset(self.get_queryset())
        t_qs = perf_counter()
        print(f"[PostViewSet.list] start count={queryset.count()}")
        
        # Utiliser la pagination DRF
        page = self.paginate_queryset(queryset)
        t_paginate = perf_counter()
        if page is not None:
            serializer = self.get_serializer(page, many=True)
            t_ser = perf_counter()
            print(f"[PostViewSet.list] page count={len(page)} qs_ms={(t_qs - t0)*1000:.1f} paginate_ms={(t_paginate - t_qs)*1000:.1f} serialize_ms={(t_ser - t_paginate)*1000:.1f} total_ms={(t_ser - t0)*1000:.1f}")
            logger.info(
                "[PostViewSet.list] page count=%s qs_time_ms=%.1f paginate_ms=%.1f serialize_ms=%.1f total_ms=%.1f",
                len(page),
                (t_qs - t0) * 1000,
                (t_paginate - t_qs) * 1000,
                (t_ser - t_paginate) * 1000,
                (t_ser - t0) * 1000,
            )
            return self.get_paginated_response(serializer.data)
        
        # Fallback si pas de pagination (ne devrait pas arriver)
        serializer = self.get_serializer(queryset, many=True)
        t_ser = perf_counter()
        print(f"[PostViewSet.list] no_page count={queryset.count()} qs_ms={(t_qs - t0)*1000:.1f} paginate_ms={(t_paginate - t_qs)*1000:.1f} serialize_ms={(t_ser - t_paginate)*1000:.1f} total_ms={(t_ser - t0)*1000:.1f}")
        logger.info(
            "[PostViewSet.list] no_page count=%s qs_time_ms=%.1f paginate_ms=%.1f serialize_ms=%.1f total_ms=%.1f",
            queryset.count(),
            (t_qs - t0) * 1000,
            (t_paginate - t_qs) * 1000,
            (t_ser - t_paginate) * 1000,
            (t_ser - t0) * 1000,
        )
        return Response(serializer.data)
    
    def get_serializer_class(self):
        if self.action == 'list':
            from .serializers import PostListSerializer
            return PostListSerializer
        if self.action in ['create', 'update', 'partial_update']:
            return PostCreateUpdateSerializer
        return PostSerializer
    
    def perform_create(self, serializer):
        serializer.save(user=self.request.user)

    @action(detail=True, methods=['post'], url_path='report')
    def report(self, request, pk=None):
        """Signaler un post publié (un signalement par utilisateur et par post)."""
        post = get_object_or_404(Post, pk=pk, is_published=True)
        if post.user_id == request.user.id:
            return Response(
                {'detail': 'Vous ne pouvez pas signaler votre propre publication.'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        reason = (request.data.get('reason') or '')[:2000]
        PostReport.objects.update_or_create(
            post=post,
            reporter=request.user,
            defaults={'reason': reason},
        )
        return Response({'ok': True}, status=status.HTTP_200_OK)

    @action(detail=False, methods=['post'])
    def get_upload_presigned_url(self, request):
        """Générer une URL pré-signée pour uploader une photo directement vers S3"""
        recipe_batch_id = request.data.get('recipe_batch_id')
        meal_plan_id = request.data.get('meal_plan_id')
        photo_type = request.data.get('photo_type', 'spontaneous')
        
        if not recipe_batch_id and not meal_plan_id:
            return Response({'error': 'recipe_batch_id or meal_plan_id is required'}, status=status.HTTP_400_BAD_REQUEST)
        
        try:
            accessible_meal_plan_filter = get_accessible_meal_plan_filter(request.user)
            recipe_batch = None
            meal_plan = None
            if recipe_batch_id:
                # Vérifier batch accessible via meal plans
                recipe_batch = RecipeBatch.objects.filter(
                    id=recipe_batch_id,
                    meal_plan_recipe_batches__meal_plan__in=MealPlan.objects.filter(
                        accessible_meal_plan_filter
                    )
                ).distinct().first()
                if not recipe_batch:
                    return Response({'error': 'Recipe batch not found or access denied'}, status=status.HTTP_404_NOT_FOUND)
            else:
                meal_plan = MealPlan.objects.filter(id=meal_plan_id).filter(accessible_meal_plan_filter).first()
                if not meal_plan:
                    return Response({'error': 'Meal plan not found or access denied'}, status=status.HTTP_404_NOT_FOUND)
        except Exception as e:
            return Response({'error': f'Error accessing target: {str(e)}'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
        
        # Vérifier que le type de photo est valide
        if photo_type not in PHOTO_TYPES:
            return Response({'error': f'Invalid photo_type. Must be one of: {", ".join(PHOTO_TYPES)}'}, status=status.HTTP_400_BAD_REQUEST)
        
        # Vérifier l'unicité (legacy) uniquement si on connaît le batch.
        # En upload temporaire meal_plan, on ne bloque pas: l'association batch se fera ensuite.
        if recipe_batch and photo_type in RESTRICTED_PHOTO_TYPES:
            existing_photo = PostPhoto.objects.filter(
                recipe_batch=recipe_batch,
                photo_type=photo_type,
                is_draft=False
            ).first()
            if existing_photo:
                return Response({'error': f'A {photo_type} photo already exists for this recipe batch'}, status=status.HTTP_400_BAD_REQUEST)
        
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
            if recipe_batch:
                file_name = f"recipe_batches/{recipe_batch.id}/{unique_id}.jpg"
            else:
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
                'recipe_batch_id': recipe_batch_id,
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
        """Confirmer qu'une photo a été uploadée et créer ou finaliser l'objet PostPhoto"""
        recipe_batch_id = request.data.get('recipe_batch_id')
        meal_plan_id = request.data.get('meal_plan_id')
        image_path = request.data.get('image_path') or request.data.get('file_name')  # Support des deux pour compatibilité
        photo_type = request.data.get('photo_type', 'spontaneous')
        step_id = request.data.get('step_id', None)
        draft_id = request.data.get('draft_id', None)  # ID du draft à finaliser
        
        if (not recipe_batch_id and not meal_plan_id) or not image_path:
            return Response({'error': 'recipe_batch_id or meal_plan_id and image_path are required'}, status=status.HTTP_400_BAD_REQUEST)
        
        try:
            accessible_meal_plan_filter = get_accessible_meal_plan_filter(request.user)
            recipe_batch = None
            meal_plan = None
            if recipe_batch_id:
                recipe_batch = RecipeBatch.objects.filter(
                    id=recipe_batch_id,
                    meal_plan_recipe_batches__meal_plan__in=MealPlan.objects.filter(
                        accessible_meal_plan_filter
                    )
                ).distinct().first()
                if not recipe_batch:
                    return Response({'error': 'Recipe batch not found or access denied'}, status=status.HTTP_404_NOT_FOUND)
            else:
                meal_plan = MealPlan.objects.filter(id=meal_plan_id).filter(accessible_meal_plan_filter).first()
                if not meal_plan:
                    return Response({'error': 'Meal plan not found or access denied'}, status=status.HTTP_404_NOT_FOUND)
        except Exception as e:
            return Response({'error': f'Error accessing target: {str(e)}'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
        
        # Si un draft_id est fourni, finaliser le draft
        if draft_id:
            try:
                draft = PostPhoto.objects.get(
                    id=draft_id,
                    recipe_batch=recipe_batch,
                    is_draft=True
                )
                draft.image_path = image_path
                draft.is_draft = False
                if not draft.uploaded_by_id:
                    draft.uploaded_by = request.user
                draft.save(update_fields=['image_path', 'is_draft', 'uploaded_by'])
                post_photo = draft
            except PostPhoto.DoesNotExist:
                return Response({'error': 'Draft not found or already finalized'}, status=status.HTTP_404_NOT_FOUND)
        else:
            # Vérifier l'unicité (legacy) seulement si batch connu.
            if recipe_batch and photo_type in RESTRICTED_PHOTO_TYPES:
                existing_photo = PostPhoto.objects.filter(
                    recipe_batch=recipe_batch, 
                    photo_type=photo_type,
                    is_draft=False
                ).first()
                if existing_photo:
                    return Response({'error': f'A {photo_type} photo already exists for this recipe batch'}, status=status.HTTP_400_BAD_REQUEST)
            
            # Créer l'objet PostPhoto avec image_path
            photo_data = {
                'recipe_batch': recipe_batch,
                'meal_plan': meal_plan,
                'photo_type': photo_type,
                'image_path': image_path,
                'is_draft': False,
                'uploaded_by': request.user,
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
            photo = PostPhoto.objects.select_related('recipe_batch', 'post').get(id=photo_id)
        except PostPhoto.DoesNotExist:
            return Response({'error': 'Photo not found'}, status=status.HTTP_404_NOT_FOUND)
        
        if not self._user_can_manage_photo(photo, request.user):
            return Response({'error': 'Permission denied'}, status=status.HTTP_403_FORBIDDEN)
        
        base_path = 'photos'
        if photo.recipe_batch_id:
            base_path = f"recipe_batches/{photo.recipe_batch_id}"
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
            recipe_batch=original_photo.recipe_batch,
            photo_type=original_photo.photo_type,
            image_path=new_path,
            step=original_photo.step,
            created_at=original_photo.created_at,  # Conserver la même date de création
            uploaded_by=request.user,
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
                'image_path': file_name,  # Stocker le chemin relatif
                'uploaded_by': request.user,
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
        
        # Récupérer le batch principal
        main_batch = meal_plan.meal_plan_recipe_batches.select_related('recipe_batch').first()
        if not main_batch or not main_batch.recipe_batch:
            return Response({'error': 'No recipe batch found for this meal plan'}, status=status.HTTP_400_BAD_REQUEST)

        complete_meal_plan_batches_for_publish(request.user, meal_plan)
        
        # Créer le post
        post = Post.objects.create(
            user=request.user,
            meal_plan=meal_plan,
            recipe_batch=main_batch.recipe_batch,
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
                'image_path': file_name,  # Stocker le chemin relatif
                'uploaded_by': request.user,
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

        if post.meal_plan_id:
            complete_meal_plan_batches_for_publish(request.user, post.meal_plan)
        elif post.recipe_batch_id:
            complete_recipe_batch_workflow(request.user, post.recipe_batch)
        
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
        """
        Envoyer un cookie (like) à un post.
        
        Contrairement au queryset par défaut de PostViewSet (qui limite parfois
        aux posts de l'utilisateur connecté), ici on veut permettre d'envoyer
        un cookie à (presque) n'importe quel post publié.
        """
        user = request.user

        # On autorise les cookies sur tous les posts publiés, sans restriction
        # de propriétaire ou d'amitié (la "visibilité" d'un post est déjà
        # gérée par is_published).
        post_qs = Post.objects.filter(is_published=True)
        post = get_object_or_404(post_qs, pk=pk)
        
        # Vérifier si l'utilisateur a déjà donné un cookie
        cookie, created = PostCookie.objects.get_or_create(
            user=user,
            post=post
        )
        
        serializer = PostSerializer(post, context={'request': request})
        if created:
            # Notification pour le propriétaire du post (sauf si c'est soi-même)
            if post.user_id != user.id:
                logger.info("post_miam: creating notification for user=%s post=%s from user=%s", post.user_id, post.id, user.id)

                miam_titles = [
                    "On parle de ton repas",
                    "Ton repas fait des envieux",
                    "Un miam de plus au compteur",
                ]
                miam_messages = [
                    f"{user.username} a mis un miam à ton post.",
                    f"{user.username} vient de miamer ton repas.",
                    f"{user.username} a craqué pour ton repas.",
                ]

                notification = Notification.objects.create(
                    user=post.user,
                    notification_type='post_miam',
                    title=random.choice(miam_titles),
                    message=random.choice(miam_messages),
                    related_user=user,
                    related_post_id=post.id,
                )
                devices = PushDevice.objects.filter(
                    user=post.user,
                    is_active=True
                ).exclude(expo_push_token='')
                logger.info("post_miam: found %d push devices for user=%s", devices.count(), post.user_id)
                messages = []
                for device in devices:
                    messages.append(
                        {
                            'to': device.expo_push_token,
                            'title': notification.title,
                            'body': notification.message,
                            'data': {
                                'source': 'social',
                                'kind': 'post_miam',
                                'notification_id': notification.id,
                                'post_id': post.id,
                            },
                            'sound': 'default',
                        }
                    )
                if messages:
                    logger.info("post_miam: sending %d push messages for notification=%s", len(messages), notification.id)
                    send_expo_push_notifications(messages)
                else:
                    logger.info("post_miam: no active push devices for user=%s", post.user_id)
            return Response({
                'message': 'Cookie sent successfully',
                'post': serializer.data
            }, status=status.HTTP_201_CREATED)
        else:
            # Cookie déjà existant
            return Response({
                'message': 'Cookie already sent',
                'post': serializer.data
            }, status=status.HTTP_200_OK)
    
    @action(detail=True, methods=['delete'])
    def remove_cookie(self, request, pk=None):
        """Retirer un cookie (like) d'un post"""
        user = request.user

        # Permettre de retirer un cookie sur n'importe quel post publié
        post_qs = Post.objects.filter(is_published=True)
        post = get_object_or_404(post_qs, pk=pk)
        
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

    @action(detail=True, methods=['get'], url_path='cookie-users')
    def cookie_users(self, request, pk=None):
        """Liste des utilisateurs qui ont donné un Miam (cookie) à ce post."""
        post_qs = Post.objects.filter(is_published=True)
        post = get_object_or_404(post_qs, pk=pk)
        cookies = PostCookie.objects.filter(post=post).select_related('user').order_by('-created_at')
        users = [c.user for c in cookies]
        serializer = UserLightSerializer(users, many=True, context={'request': request})
        return Response(serializer.data, status=status.HTTP_200_OK)

    @action(detail=True, methods=['get', 'post'], url_path='comments')
    def comments(self, request, pk=None):
        """Liste des commentaires d'un post (GET) ou création d'un commentaire (POST)."""
        post_qs = Post.objects.filter(is_published=True)
        post = get_object_or_404(post_qs, pk=pk)

        if request.method == 'GET':
            comments = PostComment.objects.filter(post=post).select_related('user').order_by('created_at')
            serializer = PostCommentSerializer(comments, many=True, context={'request': request})
            return Response(serializer.data, status=status.HTTP_200_OK)

        # POST
        create_serializer = PostCommentCreateSerializer(
            data=request.data,
            context={'request': request, 'post': post}
        )
        if create_serializer.is_valid():
            comment = create_serializer.save()
            commenter = request.user
            text = (comment.text or '').strip()

            # 1. Notification au propriétaire du post (sauf si c'est le commentateur)
            if post.user_id != commenter.id:
                comment_titles = [
                    "Nouveau mot sur ton post",
                    "Quelqu'un réagit à ton repas",
                ]
                comment_messages = [
                    f"{commenter.username} a commenté ton post.",
                    f"{commenter.username} a laissé un petit mot sous ton repas.",
                ]

                notification = Notification.objects.create(
                    user=post.user,
                    notification_type='post_comment',
                    title=random.choice(comment_titles),
                    message=random.choice(comment_messages),
                    related_user=commenter,
                    related_post_id=post.id,
                )
                # Push Expo vers le propriétaire du post
                owner_devices = PushDevice.objects.filter(
                    user=post.user,
                    is_active=True
                ).exclude(expo_push_token='')
                owner_messages = []
                for device in owner_devices:
                    owner_messages.append(
                        {
                            'to': device.expo_push_token,
                            'title': notification.title,
                            'body': notification.message,
                            'data': {
                                'source': 'social',
                                'kind': 'post_comment',
                                'notification_id': notification.id,
                                'post_id': post.id,
                            },
                            'sound': 'default',
                        }
                    )
                if owner_messages:
                    send_expo_push_notifications(owner_messages)

            # 2. Parser les @mentions et notifier chaque utilisateur mentionné
            User = get_user_model()
            mention_pattern = r'@([a-zA-Z0-9_]+)'
            mentioned_usernames = set(re.findall(mention_pattern, text))
            notified_user_ids = {post.user_id, commenter.id}  # Éviter doublons avec propriétaire et commentateur

            for username in mentioned_usernames:
                try:
                    mentioned_user = User.objects.get(username__iexact=username)
                    if mentioned_user.id not in notified_user_ids:
                        mention_titles = [
                            "On te cite à table",
                            "Quelqu'un parle de toi",
                        ]
                        mention_messages = [
                            f"{commenter.username} t'a mentionné dans un commentaire.",
                            f"{commenter.username} t'a glissé dans la conversation.",
                        ]

                        notification = Notification.objects.create(
                            user=mentioned_user,
                            notification_type='post_comment_mention',
                            title=random.choice(mention_titles),
                            message=random.choice(mention_messages),
                            related_user=commenter,
                            related_post_id=post.id,
                        )
                        notified_user_ids.add(mentioned_user.id)
                        # Push Expo vers l'utilisateur mentionné
                        mention_devices = PushDevice.objects.filter(
                            user=mentioned_user,
                            is_active=True
                        ).exclude(expo_push_token='')
                        mention_messages_list = []
                        for device in mention_devices:
                            mention_messages_list.append(
                                {
                                    'to': device.expo_push_token,
                                    'title': notification.title,
                                    'body': notification.message,
                                    'data': {
                                        'source': 'social',
                                        'kind': 'post_comment_mention',
                                        'notification_id': notification.id,
                                        'post_id': post.id,
                                    },
                                    'sound': 'default',
                                }
                            )
                        if mention_messages_list:
                            send_expo_push_notifications(mention_messages_list)
                except User.DoesNotExist:
                    pass  # Username invalide, ignorer

            serializer = PostCommentSerializer(comment, context={'request': request})
            return Response(serializer.data, status=status.HTTP_201_CREATED)
        return Response(create_serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    @action(
        detail=True,
        methods=['post', 'delete'],
        url_path=r'comments/(?P<comment_id>[^/.]+)/like',
    )
    def like_comment(self, request, pk=None, comment_id=None):
        """
        Ajouter ou retirer un like sur un commentaire de post.

        - POST   /posts/{post_id}/comments/{comment_id}/like/   => ajoute un like
        - DELETE /posts/{post_id}/comments/{comment_id}/like/   => retire le like
        """
        post_qs = Post.objects.filter(is_published=True)
        post = get_object_or_404(post_qs, pk=pk)
        comment = get_object_or_404(PostComment, pk=comment_id, post=post)
        user = request.user

        if request.method == 'POST':
            # Créer le like s'il n'existe pas déjà
            PostCommentLike.objects.get_or_create(comment=comment, user=user)
        else:
            # Supprimer le like si présent
            PostCommentLike.objects.filter(comment=comment, user=user).delete()

        # Retourner le commentaire à jour
        serializer = PostCommentSerializer(comment, context={'request': request})
        return Response(serializer.data, status=status.HTTP_200_OK)


class ShoppingListViewSet(viewsets.ModelViewSet):
    """ViewSet pour les listes de courses"""
    serializer_class = ShoppingListSerializer
    permission_classes = [IsAuthenticated]
    
    def list(self, request, *args, **kwargs):
        """
        Liste les shopping lists accessibles par l'utilisateur.
        Cas particulier:
        - si recipe_batch_id est fourni
        - et que l'utilisateur n'a AUCUNE liste liée à ce batch
        - mais qu'une liste existe pour ce batch côté hôte
        
        => retourner une entrée minimale qui signale l'existence d'une liste
           sans exposer ses détails (nom custom, membres, etc.).
        """
        response = super().list(request, *args, **kwargs)

        recipe_batch_id = request.query_params.get('recipe_batch_id')
        # On ne fait le traitement spécial que si on filtre par batch
        try:
            recipe_batch_id_int = int(recipe_batch_id) if recipe_batch_id is not None else None
        except (TypeError, ValueError):
            recipe_batch_id_int = None

        if (
            recipe_batch_id_int is not None
            and isinstance(response.data, dict)
            and response.data.get('count', 0) == 0
        ):
            # Aucun résultat accessible, mais on vérifie s'il existe au moins une liste liée à ce batch
            slb = (
                ShoppingListBatch.objects
                .filter(recipe_batch_id=recipe_batch_id_int)
                .select_related('shopping_list')
                .first()
            )
            if slb and slb.shopping_list:
                # Construire une réponse minimale, sans exposer les membres ni le nom custom
                shopping_list = slb.shopping_list
                # Option: ne pas exposer le vrai nom, utiliser un label générique
                minimal = {
                    'id': shopping_list.id,
                    'name': 'Pas accès',
                    'color': '',
                    'is_archived': shopping_list.is_archived,
                    # Informations minimales mais utiles pour l’UI
                    'items_count': 0,
                    'is_complete': False,
                    'has_access': False,
                }
                response.data = {
                    'count': 1,
                    'next': None,
                    'previous': None,
                    'results': [minimal],
                }

        return response
    
    def get_queryset(self):
        """V2: filtrer par membership (owner/collaborator)"""
        include_archived = self.request.query_params.get('include_archived')
        recipe_batch_id = self.request.query_params.get('recipe_batch_id')

        qs = ShoppingList.objects.filter(members__user=self.request.user).distinct()
        if include_archived != 'true':
            qs = qs.filter(is_archived=False)
        if recipe_batch_id:
            try:
                qs = qs.filter(batches__recipe_batch_id=int(recipe_batch_id))
            except (TypeError, ValueError):
                pass

        return qs.prefetch_related(
            Prefetch('members', queryset=ShoppingListMember.objects.select_related('user')),
            Prefetch('items', queryset=ShoppingListItem.objects.select_related('ingredient__category')),
            Prefetch('batches', queryset=ShoppingListBatch.objects.select_related('recipe_batch__recipe')),
        ).order_by('-updated_at', '-created_at')
    
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
        """
        V2: Rebuild items/quantities from the batches currently associated to the list.
        Reset hard accepted, so we can regenerate deterministically.
        """
        shopping_list = self.get_object()

        with transaction.atomic():
            # Supprimer uniquement les quantités liées à un batch (garder les ajouts manuels)
            ShoppingListItemQuantity.objects.filter(
                shopping_list_item__shopping_list=shopping_list
            ).exclude(recipe_batch__isnull=True).delete()
            # Supprimer les items qui n'ont plus aucune quantité
            empty_item_ids = [
                item.id for item in ShoppingListItem.objects.filter(shopping_list=shopping_list)
                if not item.quantities.exists()
            ]
            ShoppingListItem.objects.filter(id__in=empty_item_ids).delete()
            for slb in ShoppingListBatch.objects.filter(shopping_list=shopping_list).select_related('recipe_batch__recipe'):
                self._add_batch_ingredients_to_list(shopping_list, slb.recipe_batch, request.user)

        items = ShoppingListItem.objects.filter(shopping_list=shopping_list).select_related('ingredient__category', 'checked_by')
        return Response(ShoppingListItemSerializer(items, many=True, context={'request': request}).data, status=status.HTTP_200_OK)

    @action(detail=True, methods=['post'], url_path='conflict-notice')
    def conflict_notice(self, request, pk=None):
        """
        Receive an offline-sync conflict notice from a client and broadcast it
        to the shopping list WebSocket group.

        Payload:
        {
          "items": [{"item_id": 123, "name": "Tomates", "target_user_id": 42}, ...]
        }

        Note: the server remains source of truth (we keep first checker).
        This endpoint only notifies relevant users for UX/coordination.
        """
        shopping_list = self.get_object()  # enforces membership through queryset
        items = request.data.get('items') or []
        if not isinstance(items, list) or not items:
            return Response({"ok": True, "broadcasted": False}, status=status.HTTP_200_OK)

        sanitized_items = []
        target_user_ids = set()
        for it in items:
            if not isinstance(it, dict):
                continue
            item_id = it.get('item_id')
            name = (it.get('name') or '').strip()
            target_user_id = it.get('target_user_id')
            try:
                item_id_int = int(item_id)
            except (TypeError, ValueError):
                continue
            try:
                target_user_id_int = int(target_user_id) if target_user_id is not None else None
            except (TypeError, ValueError):
                target_user_id_int = None

            if not name:
                name = f"Item #{item_id_int}"

            sanitized_items.append(
                {"item_id": item_id_int, "name": name, "target_user_id": target_user_id_int}
            )
            if target_user_id_int is not None:
                target_user_ids.add(target_user_id_int)

        if not sanitized_items:
            return Response({"ok": True, "broadcasted": False}, status=status.HTTP_200_OK)

        member_ids = set(
            ShoppingListMember.objects.filter(shopping_list=shopping_list).values_list('user_id', flat=True)
        )
        target_user_ids = sorted(list(target_user_ids.intersection(member_ids)))

        try:
            channel_layer = get_channel_layer()
            if channel_layer is not None:
                group_name = f"shopping_list_{shopping_list.id}"
                actor = request.user
                async_to_sync(channel_layer.group_send)(
                    group_name,
                    {
                        "type": "shopping_list_conflict_notice",
                        "shopping_list_id": shopping_list.id,
                        "actor_user_id": actor.id if actor else None,
                        "actor_username": getattr(actor, "username", "") or getattr(actor, "email", "") or "",
                        "target_user_ids": target_user_ids,
                        "items": sanitized_items,
                    },
                )
        except Exception:
            pass

        return Response({"ok": True, "broadcasted": True, "targets": target_user_ids}, status=status.HTTP_200_OK)

    @action(detail=True, methods=['get', 'post'], url_path='loyalty-cards')
    def loyalty_cards(self, request, pk=None):
        """
        GET  /shopping-lists/{id}/loyalty-cards/ :
            Retourne les cartes de fidélité associées à cette liste.

        POST /shopping-lists/{id}/loyalty-cards/ :
            - Si `card_id` est fourni, associe une carte existante (appartenant à l'utilisateur).
            - Sinon, crée une nouvelle carte (nom, emoji, barcode_type, number) et l'associe.
        """
        shopping_list = self.get_object()  # membership enforced by queryset

        if request.method == 'GET':
            links = (
                ShoppingListLoyaltyCard.objects
                .filter(shopping_list=shopping_list)
                .select_related('card', 'card__owner')
            )
            cards = [link.card for link in links if link.card and link.card.is_active]
            serializer = LoyaltyCardSerializer(cards, many=True, context={'request': request})
            return Response(serializer.data, status=status.HTTP_200_OK)

        # POST
        data = request.data or {}
        card_id = data.get('card_id')

        if card_id:
            try:
                card = LoyaltyCard.objects.get(id=card_id, owner=request.user, is_active=True)
            except LoyaltyCard.DoesNotExist:
                return Response(
                    {'detail': "Carte introuvable ou vous n'en êtes pas le propriétaire."},
                    status=status.HTTP_404_NOT_FOUND,
                )
        else:
            serializer = LoyaltyCardSerializer(data=data, context={'request': request})
            serializer.is_valid(raise_exception=True)
            card = serializer.save()

        ShoppingListLoyaltyCard.objects.get_or_create(
            shopping_list=shopping_list,
            card=card,
        )
        output = LoyaltyCardSerializer(card, context={'request': request})
        return Response(output.data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=['delete'], url_path='loyalty-cards/(?P<card_id>[^/.]+)')
    def remove_loyalty_card(self, request, pk=None, card_id=None):
        """
        DELETE /shopping-lists/{id}/loyalty-cards/{card_id}/ :
            Retire une carte de fidélité de la liste.
            Réservé au propriétaire de la liste.
        """
        shopping_list = self.get_object()
        is_owner = ShoppingListMember.objects.filter(
            shopping_list=shopping_list,
            user=request.user,
            role='owner',
        ).exists()
        if not is_owner:
            return Response(
                {'detail': "Seul le propriétaire de la liste peut retirer une carte."},
                status=status.HTTP_403_FORBIDDEN,
            )

        try:
            link = ShoppingListLoyaltyCard.objects.get(
                shopping_list=shopping_list,
                card_id=card_id,
            )
        except ShoppingListLoyaltyCard.DoesNotExist:
            # Rien à faire, considérer comme succès idempotent
            return Response(status=status.HTTP_204_NO_CONTENT)

        link.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)

    def _unit_group_for_unit(self, unit: str) -> str:
        unit = (unit or '').lower()
        if unit in ['g', 'kg']:
            return 'weight'
        if unit in ['ml', 'l']:
            return 'volume'
        if unit == 'piece':
            return 'count'
        if unit == 'pinch':
            return 'pinch'
        if unit == 'clove':
            return 'clove'
        return 'other'

    def _canonicalize_quantity(self, quantity: float, unit: str):
        unit = (unit or '').lower()
        if unit == 'kg':
            return float(quantity) * 1000.0, 'g'
        if unit == 'l':
            return float(quantity) * 1000.0, 'ml'
        return float(quantity), unit

    def _compute_total_servings_batch(self, batch: RecipeBatch) -> float:
        """
        Compute total servings for a batch by summing servings across all linked meal plans.
        Falls back to recipe.servings.
        """
        recipe = batch.recipe
        if not recipe:
            return 1.0
        total = 0
        mprbs = MealPlanRecipeBatch.objects.filter(recipe_batch=batch).select_related('meal_plan')
        for mprb in mprbs:
            total += get_batch_portions(mprb.meal_plan, mprb)
        return float(total) if total > 0 else float(recipe.servings or 1)

    def _add_batch_ingredients_to_list(self, shopping_list: ShoppingList, batch: RecipeBatch, actor):
        """
        Adds all recipe ingredients for the given batch into the shopping list as:
        - ShoppingListItem (ingredient + unit_group)
        - ShoppingListItemQuantity per batch (canonical unit for convertible dims)
        """
        recipe = batch.recipe
        if not recipe:
            return

        servings = self._compute_total_servings_batch(batch)
        base_servings = float(recipe.servings or 1)
        ratio = servings / base_servings if base_servings else 1.0

        # Prefetch recipe ingredients if possible
        ris = recipe.recipe_ingredients.all().select_related('ingredient__category')

        for ri in ris:
            unit_group = self._unit_group_for_unit(ri.unit)
            qty = float(ri.quantity) * ratio
            qty_canon, unit_canon = self._canonicalize_quantity(qty, ri.unit)

            # V2 rule: only merge within same unit_group; non-convertibles become separate lines by unit_group mapping
            item, _ = ShoppingListItem.objects.get_or_create(
                shopping_list=shopping_list,
                ingredient=ri.ingredient,
                unit_group=unit_group,
                defaults={
                    'pantry_quantity': 0,
                    'pantry_unit': unit_canon,
                }
            )
            if not item.pantry_unit:
                item.pantry_unit = unit_canon
                item.save(update_fields=['pantry_unit'])

            slq, created = ShoppingListItemQuantity.objects.get_or_create(
                shopping_list_item=item,
                recipe_batch=batch,
                defaults={
                    'quantity': qty_canon,
                    'unit': unit_canon,
                    'checked_quantity': 0,
                }
            )
            if not created:
                # merge quantities (add)
                slq.quantity = Decimal(str(slq.quantity or 0)) + Decimal(str(qty_canon))
                slq.unit = unit_canon or slq.unit
                slq.save(update_fields=['quantity', 'unit', 'updated_at'])

    @action(detail=True, methods=['post'])
    def associate_batch(self, request, pk=None):
        """Associer un RecipeBatch à cette liste (batch unique par liste)."""
        shopping_list = self.get_object()
        recipe_batch_id = request.data.get('recipe_batch_id')
        if not recipe_batch_id:
            return Response({'error': 'recipe_batch_id required'}, status=status.HTTP_400_BAD_REQUEST)

        try:
            batch = RecipeBatch.objects.select_related('recipe').get(id=int(recipe_batch_id))
        except (RecipeBatch.DoesNotExist, ValueError, TypeError):
            return Response({'error': 'Recipe batch not found'}, status=status.HTTP_404_NOT_FOUND)

        with transaction.atomic():
            # If already in another list, remove from old list (and remove all ingredients from that batch)
            existing_link = ShoppingListBatch.objects.filter(recipe_batch=batch).select_related('shopping_list').first()
            if existing_link and existing_link.shopping_list_id != shopping_list.id:
                old_list = existing_link.shopping_list
                # Delete all quantities contributed by this batch in old list
                ShoppingListItemQuantity.objects.filter(
                    recipe_batch=batch,
                    shopping_list_item__shopping_list=old_list
                ).delete()
                # Delete empty items
                ShoppingListItem.objects.filter(shopping_list=old_list).annotate(
                    qcount=Count('quantities')
                ).filter(qcount=0).delete()
                existing_link.delete()

            # Create link to new list (or keep if already linked)
            ShoppingListBatch.objects.update_or_create(
                recipe_batch=batch,
                defaults={'shopping_list': shopping_list},
            )

            # Add ingredients for this batch into the list
            self._add_batch_ingredients_to_list(shopping_list, batch, request.user)

        return Response(ShoppingListSerializer(shopping_list, context={'request': request}).data, status=status.HTTP_200_OK)

    @action(detail=True, methods=['post'])
    def remove_batch(self, request, pk=None):
        """Retirer un batch de la liste et supprimer tous les ingrédients apportés par ce batch (V1)."""
        shopping_list = self.get_object()
        recipe_batch_id = request.data.get('recipe_batch_id')
        if not recipe_batch_id:
            return Response({'error': 'recipe_batch_id required'}, status=status.HTTP_400_BAD_REQUEST)

        try:
            batch_id_int = int(recipe_batch_id)
        except (TypeError, ValueError):
            return Response({'error': 'Invalid recipe_batch_id'}, status=status.HTTP_400_BAD_REQUEST)

        with transaction.atomic():
            ShoppingListBatch.objects.filter(shopping_list=shopping_list, recipe_batch_id=batch_id_int).delete()
            ShoppingListItemQuantity.objects.filter(
                recipe_batch_id=batch_id_int,
                shopping_list_item__shopping_list=shopping_list
            ).delete()
            ShoppingListItem.objects.filter(shopping_list=shopping_list).annotate(
                qcount=Count('quantities')
            ).filter(qcount=0).delete()

        return Response({'ok': True}, status=status.HTTP_200_OK)

    @action(detail=True, methods=['post'])
    def invite(self, request, pk=None):
        """Inviter des utilisateurs à collaborer sur une liste de courses"""
        from django.contrib.auth import get_user_model
        from django.db import transaction
        from accounts.models import Follow, Notification
        User = get_user_model()
        
        shopping_list = self.get_object()
        invitee_ids = request.data.get('invitee_ids', [])
        
        if not invitee_ids:
            return Response({'error': 'invitee_ids is required'}, status=status.HTTP_400_BAD_REQUEST)
        
        # Vérifier que l'utilisateur est owner ou collaborator
        is_owner = shopping_list.members.filter(user=request.user, role='owner').exists()
        is_collaborator = shopping_list.members.filter(user=request.user, role='collaborator').exists()
        if not (is_owner or is_collaborator):
            return Response({'error': 'Permission denied'}, status=status.HTTP_403_FORBIDDEN)
        
        # Vérifier que les utilisateurs sont des complices
        following_ids = Follow.objects.filter(follower=request.user).values_list('following_id', flat=True)
        followers_ids = Follow.objects.filter(following=request.user).values_list('follower_id', flat=True)
        complice_ids = set(list(following_ids) + list(followers_ids))
        
        valid_invitee_ids = [user_id for user_id in invitee_ids if user_id in complice_ids]
        
        if not valid_invitee_ids:
            return Response({'error': 'No valid complices found'}, status=status.HTTP_400_BAD_REQUEST)
        
        # Précharger les utilisateurs
        invitees = {user.id: user for user in User.objects.filter(id__in=valid_invitee_ids)}
        
        # Créer les invitations
        invitations = []
        notification_data = []
        
        with transaction.atomic():
            for invitee_id in valid_invitee_ids:
                invitee = invitees.get(invitee_id)
                if not invitee:
                    continue
                
                # Ne pas inviter si déjà membre
                if shopping_list.members.filter(user=invitee).exists():
                    continue
                
                invitation, created = ShoppingListInvitation.objects.get_or_create(
                    inviter=request.user,
                    invitee=invitee,
                    shopping_list=shopping_list,
                    defaults={'status': 'pending'}
                )
                if created:
                    invitations.append(invitation)
                    notification_data.append({
                        'user': invitee,
                        'notification_type': 'shopping_list_invitation',
                        'title': f"{request.user.username} vous invite à une liste de courses",
                        'message': f"{request.user.username} vous invite à collaborer sur '{shopping_list.name}'",
                        'related_user': request.user
                    })
        
        # Créer les notifications après commit
        if notification_data:
            def create_notifications():
                for notif_data in notification_data:
                    Notification.objects.create(**notif_data)
            transaction.on_commit(create_notifications)
        
        shopping_list.refresh_from_db()
        serializer = self.get_serializer(shopping_list, context={'request': request})
        return Response(serializer.data, status=status.HTTP_200_OK)

    @action(detail=True, methods=['post'], url_path='add_members')
    def add_members(self, request, pk=None):
        """Ajouter des membres directement à la liste (sans validation). Ils voient la liste dans leurs listes."""
        from django.contrib.auth import get_user_model
        from django.db import transaction
        from accounts.models import Follow, Notification
        User = get_user_model()

        shopping_list = self.get_object()
        invitee_ids = request.data.get('invitee_ids', [])

        if not invitee_ids:
            return Response({'error': 'invitee_ids is required'}, status=status.HTTP_400_BAD_REQUEST)

        is_owner = shopping_list.members.filter(user=request.user, role='owner').exists()
        is_collaborator = shopping_list.members.filter(user=request.user, role='collaborator').exists()
        if not (is_owner or is_collaborator):
            return Response({'error': 'Permission denied'}, status=status.HTTP_403_FORBIDDEN)

        following_ids = Follow.objects.filter(follower=request.user).values_list('following_id', flat=True)
        followers_ids = Follow.objects.filter(following=request.user).values_list('follower_id', flat=True)
        complice_ids = set(list(following_ids) + list(followers_ids))
        valid_invitee_ids = [uid for uid in invitee_ids if uid in complice_ids]

        if not valid_invitee_ids:
            return Response({'error': 'No valid complices found'}, status=status.HTTP_400_BAD_REQUEST)

        invitees = {u.id: u for u in User.objects.filter(id__in=valid_invitee_ids)}
        notification_data = []

        with transaction.atomic():
            for invitee_id in valid_invitee_ids:
                invitee = invitees.get(invitee_id)
                if not invitee or shopping_list.members.filter(user=invitee).exists():
                    continue
                ShoppingListMember.objects.get_or_create(
                    shopping_list=shopping_list,
                    user=invitee,
                    defaults={'role': 'collaborator'}
                )
                notification_data.append({
                    'user': invitee,
                    'notification_type': 'shopping_list_invitation',
                    'title': f"{request.user.username} vous a ajouté à une liste de courses",
                    'message': f"{request.user.username} vous a ajouté à la liste '{shopping_list.name}'",
                    'related_user': request.user
                })

        if notification_data:
            def create_notifications():
                for nd in notification_data:
                    Notification.objects.create(**nd)
            transaction.on_commit(create_notifications)

        shopping_list.refresh_from_db()
        serializer = self.get_serializer(shopping_list, context={'request': request})
        return Response(serializer.data, status=status.HTTP_200_OK)

    @action(detail=True, methods=['post'])
    def remove_member(self, request, pk=None):
        """Retirer un membre d'une liste de courses (seulement owner)"""
        shopping_list = self.get_object()
        user_id = request.data.get('user_id')
        
        if not user_id:
            return Response({'error': 'user_id is required'}, status=status.HTTP_400_BAD_REQUEST)
        
        # Vérifier que l'utilisateur est owner
        is_owner = shopping_list.members.filter(user=request.user, role='owner').exists()
        if not is_owner:
            return Response({'error': 'Only owner can remove members'}, status=status.HTTP_403_FORBIDDEN)
        
        # Ne pas permettre de retirer le owner
        member_to_remove = shopping_list.members.filter(user_id=user_id).first()
        if not member_to_remove:
            return Response({'error': 'Member not found'}, status=status.HTTP_404_NOT_FOUND)
        
        if member_to_remove.role == 'owner':
            return Response({'error': 'Cannot remove owner'}, status=status.HTTP_400_BAD_REQUEST)
        
        member_to_remove.delete()
        shopping_list.refresh_from_db()
        serializer = self.get_serializer(shopping_list, context={'request': request})
        return Response(serializer.data, status=status.HTTP_200_OK)

    @action(detail=True, methods=['get'])
    def batch_done(self, request, pk=None):
        """
        Indique si les ingrédients du batch sont "couverts" (V1) dans cette liste.
        V1 simplifié: done si pour chaque quantity du batch, checked_quantity >= quantity.
        (Pantry non pris en compte ici pour rester simple.)
        """
        shopping_list = self.get_object()
        recipe_batch_id = request.query_params.get('recipe_batch_id')
        if not recipe_batch_id:
            return Response({'error': 'recipe_batch_id required'}, status=status.HTTP_400_BAD_REQUEST)
        try:
            batch_id_int = int(recipe_batch_id)
        except (TypeError, ValueError):
            return Response({'error': 'Invalid recipe_batch_id'}, status=status.HTTP_400_BAD_REQUEST)

        qs = ShoppingListItemQuantity.objects.filter(
            recipe_batch_id=batch_id_int,
            shopping_list_item__shopping_list=shopping_list,
        )
        if not qs.exists():
            return Response({'done': False}, status=status.HTTP_200_OK)

        for q in qs:
            if Decimal(str(q.checked_quantity or 0)) < Decimal(str(q.quantity or 0)):
                return Response({'done': False}, status=status.HTTP_200_OK)
        return Response({'done': True}, status=status.HTTP_200_OK)


class ShoppingListInvitationViewSet(viewsets.ModelViewSet):
    """ViewSet pour les invitations aux listes de courses"""
    serializer_class = None  # À définir dans serializers.py
    permission_classes = [IsAuthenticated]
    
    def get_queryset(self):
        # L'utilisateur peut voir les invitations qu'il a envoyées ou reçues
        qs = ShoppingListInvitation.objects.filter(
            Q(inviter=self.request.user) | Q(invitee=self.request.user)
        ).select_related('inviter', 'invitee', 'shopping_list')
        
        # Filtrer par shopping_list si fourni
        shopping_list_id = self.request.query_params.get('shopping_list')
        if shopping_list_id:
            try:
                qs = qs.filter(shopping_list_id=shopping_list_id)
            except ValueError:
                pass
        
        return qs
    
    def get_serializer_class(self):
        # Utiliser un serializer simple pour les invitations
        from .serializers import ShoppingListInvitationSerializer
        return ShoppingListInvitationSerializer
    
    @action(detail=True, methods=['post'])
    def accept(self, request, pk=None):
        """Accepter une invitation"""
        invitation = self.get_object()
        if invitation.invitee != request.user:
            return Response({'error': 'Permission denied'}, status=status.HTTP_403_FORBIDDEN)
        
        if invitation.status != 'pending':
            return Response({'error': 'Invitation already processed'}, status=status.HTTP_400_BAD_REQUEST)
        
        with transaction.atomic():
            invitation.status = 'accepted'
            invitation.save()
            
            # Créer le membre
            ShoppingListMember.objects.get_or_create(
                shopping_list=invitation.shopping_list,
                user=invitation.invitee,
                defaults={'role': 'collaborator'}
            )
        
        serializer = self.get_serializer(invitation)
        return Response(serializer.data, status=status.HTTP_200_OK)
    
    @action(detail=True, methods=['post'])
    def decline(self, request, pk=None):
        """Refuser une invitation"""
        invitation = self.get_object()
        if invitation.invitee != request.user:
            return Response({'error': 'Permission denied'}, status=status.HTTP_403_FORBIDDEN)
        
        if invitation.status != 'pending':
            return Response({'error': 'Invitation already processed'}, status=status.HTTP_400_BAD_REQUEST)
        
        invitation.status = 'declined'
        invitation.save()
        
        serializer = self.get_serializer(invitation)
        return Response(serializer.data, status=status.HTTP_200_OK)


def _mark_shopping_done_if_list_complete(shopping_list):
    """
    Si la liste est complète (tous les ingrédients achetés), marque shopping_done=True
    sur tous les RecipeBatch associés à cette liste.
    """
    from decimal import Decimal
    items = shopping_list.items.prefetch_related('quantities').all()
    if not items.exists():
        return
    for item in items:
        pantry_qty = Decimal(str(item.pantry_quantity or 0))
        total_qty = sum(Decimal(str(q.quantity or 0)) for q in item.quantities.all())
        total_checked = sum(Decimal(str(q.checked_quantity or 0)) for q in item.quantities.all())
        remaining = total_qty - total_checked - pantry_qty
        if remaining > 0:
            return  # liste pas complète
    batch_ids = list(
        ShoppingListBatch.objects.filter(shopping_list=shopping_list).values_list('recipe_batch_id', flat=True)
    )
    if batch_ids:
        RecipeBatch.objects.filter(id__in=batch_ids).update(shopping_done=True, updated_at=timezone.now())


def _broadcast_shopping_list_item_update(item, updated_by_user):
    """
    Broadcast a minimal, aggregated view of a ShoppingListItem to the WebSocket room
    corresponding to its shopping list.
    """
    try:
        from decimal import Decimal as _D

        # Aggregate quantities
        qs = item.quantities.all()
        total_qty = float(sum(_D(str(q.quantity or 0)) for q in qs))
        total_checked = float(sum(_D(str(q.checked_quantity or 0)) for q in qs))

        # Determine unit (same logic as with_quantities)
        unit = ""
        first_q = qs.first()
        if first_q and first_q.unit:
            unit = first_q.unit
        elif item.pantry_unit:
            unit = item.pantry_unit

        pantry_qty = float(item.pantry_quantity or 0)

        category = item.ingredient.category

        payload = {
            "item_id": item.id,
            "shopping_list_id": item.shopping_list_id,
            "ingredient_id": item.ingredient_id,
            "name": item.ingredient.name,
            "unit_group": item.unit_group,
            "quantity": total_qty,
            "checked_quantity": total_checked,
            "unit": unit,
            "category": {
                "id": category.id,
                "name": category.name,
                "display_order": category.display_order,
            }
            if category
            else None,
            "pantry_quantity": pantry_qty,
            "pantry_unit": item.pantry_unit or unit or "",
            "checked_at": item.checked_at.isoformat() if item.checked_at else None,
            "checked_by": {
                "id": updated_by_user.id,
                "username": getattr(updated_by_user, "username", "") or updated_by_user.email,
                "name": getattr(updated_by_user, "username", "") or updated_by_user.email,
            }
            if updated_by_user
            else None,
        }

        channel_layer = get_channel_layer()
        if channel_layer is None:
            return

        group_name = f"shopping_list_{item.shopping_list_id}"
        async_to_sync(channel_layer.group_send)(
            group_name,
            {
                "type": "shopping_list_item_updated",
                "item": payload,
                "updated_by_user_id": updated_by_user.id if updated_by_user else None,
            },
        )
    except Exception:
        # En cas de problème de temps réel, ne jamais casser la requête principale.
        return


class ShoppingListItemViewSet(viewsets.ModelViewSet):
    """ViewSet pour les items de liste de courses"""
    serializer_class = ShoppingListItemSerializer
    permission_classes = [IsAuthenticated]
    
    def get_queryset(self):
        """V2: Filtrer par shopping list accessible via membership"""
        shopping_list_id = self.request.query_params.get('shopping_list_id')
        
        if shopping_list_id:
            try:
                shopping_list = ShoppingList.objects.get(id=shopping_list_id, members__user=self.request.user)
                queryset = ShoppingListItem.objects.filter(shopping_list=shopping_list)
            except ShoppingList.DoesNotExist:
                return ShoppingListItem.objects.none()
        else:
            queryset = ShoppingListItem.objects.filter(shopping_list__members__user=self.request.user).distinct()
        
        # Filtres optionnels
        ingredient_id = self.request.query_params.get('ingredient_id')
        
        if ingredient_id:
            queryset = queryset.filter(ingredient_id=ingredient_id)
        
        # Optimisation : précharger toutes les relations nécessaires
        return queryset.select_related(
            'ingredient__category',
            'shopping_list',
            'checked_by',
        ).order_by('-updated_at')
    
    @action(detail=False, methods=['get'])
    def with_quantities(self, request):
        """V2: Retourne les lignes (ingredient + unit_group) avec quantités totalisées depuis ShoppingListItemQuantity."""
        
        shopping_list_id = request.query_params.get('shopping_list_id')
        
        if not shopping_list_id:
            return Response({'error': 'shopping_list_id required'}, status=status.HTTP_400_BAD_REQUEST)
        
        from .models import MealPlan, MealPlanRecipeBatch, PostPhoto
        from .serializers import RecipeLightSerializer
        
        try:
            shopping_list = ShoppingList.objects.prefetch_related(
                Prefetch('items', queryset=ShoppingListItem.objects.select_related('ingredient__category', 'checked_by').prefetch_related(
                    Prefetch('quantities', queryset=ShoppingListItemQuantity.objects.select_related(
                        'recipe_batch__recipe', 'checked_by'
                    ).prefetch_related(
                        Prefetch('recipe_batch__meal_plan_recipe_batches', queryset=MealPlanRecipeBatch.objects.select_related('meal_plan'))
                    ))
                ))
            ).get(id=shopping_list_id, members__user=request.user)
        except ShoppingList.DoesNotExist:
            return Response({'error': 'Shopping list not found'}, status=status.HTTP_404_NOT_FOUND)
        
        result = []
        now = timezone.now()
        hide_after = timedelta(days=1)
        for item in shopping_list.items.all():
            quantities_list = list(item.quantities.all())
            active_quantities = [
                q for q in quantities_list
                if not shopping_list_item_quantity_is_stale(q, now, hide_after)
            ]
            if not active_quantities:
                continue

            total_qty = float(sum(Decimal(str(q.quantity or 0)) for q in active_quantities))
            total_checked = float(sum(Decimal(str(q.checked_quantity or 0)) for q in active_quantities))
            unit = ''
            first_q = active_quantities[0]
            if first_q and first_q.unit:
                unit = first_q.unit
            elif item.pantry_unit:
                unit = item.pantry_unit

            category = item.ingredient.category

            # Filtrer côté backend les lignes "tout acheté" depuis plus de 24h (totaux = quantités actives)
            pantry_qty = float(item.pantry_quantity or 0)
            remaining = total_qty - total_checked - pantry_qty
            if remaining <= 0 and item.checked_at:
                try:
                    if now - item.checked_at > hide_after:
                        # Ignorer complètement cette ligne dans la réponse
                        continue
                except Exception:
                    # En cas de problème de timezone/valeur, ne pas filtrer agressivement
                    pass

            line_checked_at = None if remaining > 0 else item.checked_at
            line_checked_by = None if remaining > 0 else item.checked_by
            
            # Collecter les batches avec leurs détails (recipe_batch=None = ajout manuel)
            batches_data = []
            batch_ids = []
            has_manual = False
            manual_qty = 0.0
            manual_checked = 0.0
            for q in active_quantities:
                batch = q.recipe_batch
                if batch is None:
                    # Quantité manuelle (pas de recette liée)
                    has_manual = True
                    manual_qty += float(q.quantity or 0)
                    manual_checked += float(q.checked_quantity or 0)
                    continue
                if batch.id in batch_ids:
                    continue
                batch_ids.append(batch.id)
                
                dates = []
                for mprb in batch.meal_plan_recipe_batches.all():
                    if mprb.meal_plan:
                        dates.append(mprb.meal_plan.date.isoformat())
                dates = sorted(list(set(dates)))
                
                photo_url = None
                if batch.recipe:
                    first_photo = PostPhoto.objects.filter(recipe_batch=batch).order_by('-created_at').first()
                    if first_photo:
                        try:
                            from savr_back.settings import build_presigned_get_url
                            photo_url = build_presigned_get_url(first_photo.image_path, expires_in=3600)
                        except Exception:
                            pass
                
                recipe_data = None
                if batch.recipe:
                    try:
                        recipe_data = RecipeLightSerializer(batch.recipe, context={'request': request}).data
                    except Exception:
                        recipe_data = {
                            'id': batch.recipe.id,
                            'title': batch.recipe.title or '',
                            'image_url': None,
                        }
                
                batches_data.append({
                    'batch_id': batch.id,
                    'recipe': recipe_data,
                    'quantity': float(q.quantity or 0),
                    'checked_quantity': float(q.checked_quantity or 0),
                    'unit': q.unit or unit,
                    'dates': dates,
                    'photo_url': photo_url,
                    'is_manual': False,
                })
            
            if has_manual:
                batches_data.append({
                    'batch_id': None,
                    'recipe': None,
                    'quantity': manual_qty,
                    'checked_quantity': manual_checked,
                    'unit': unit,
                    'dates': [],
                    'photo_url': None,
                    'is_manual': True,
                })
            
            result.append({
                'item_id': item.id,
                'ingredient_id': item.ingredient.id,
                'unit_group': item.unit_group,
                'name': item.ingredient.name,
                'quantity': total_qty,
                'checked_quantity': total_checked,
                'unit': unit,
                'category': {
                    'id': category.id if category else None,
                    'name': category.name if category else 'Autres',
                    'display_order': category.display_order if category else None,
                },
                'pantry_quantity': float(item.pantry_quantity or 0),
                'pantry_unit': item.pantry_unit or unit or '',
                'checked_at': line_checked_at,
                'checked_by': UserLightSerializer(line_checked_by).data if line_checked_by else None,
                'batches': batches_data,
                'recipes_count': len(batches_data),
            })
        return Response(result, status=status.HTTP_200_OK)

    @action(detail=True, methods=['post'])
    def check(self, request, pk=None):
        """Cocher la totalité restante (V1) pour cette ligne."""
        item = self.get_object()
        with transaction.atomic():
            # total remaining = sum(q.quantity - q.checked_quantity) - pantry
            pantry = Decimal(str(item.pantry_quantity or 0))
            qs = item.quantities.select_for_update().all()
            # remaining before pantry
            remaining_total = sum((Decimal(str(q.quantity or 0)) - Decimal(str(q.checked_quantity or 0))) for q in qs)
            remaining_after_pantry = remaining_total - pantry
            if remaining_after_pantry < 0:
                remaining_after_pantry = Decimal('0')

            # Cocher tout le restant, réparti proportionnellement (simple: cocher chaque q à fond tant qu'il reste)
            to_allocate = remaining_after_pantry
            for q in qs:
                if to_allocate <= 0:
                    break
                q_remaining = Decimal(str(q.quantity or 0)) - Decimal(str(q.checked_quantity or 0))
                if q_remaining <= 0:
                    continue
                add = q_remaining if q_remaining <= to_allocate else to_allocate
                q.checked_quantity = Decimal(str(q.checked_quantity or 0)) + add
                q.checked_at = timezone.now()
                q.checked_by = request.user
                q.save(update_fields=['checked_quantity', 'checked_at', 'checked_by', 'updated_at'])
                to_allocate -= add

            item.checked_at = timezone.now()
            item.checked_by = request.user
            item.save(update_fields=['checked_at', 'checked_by', 'updated_at'])

        _mark_shopping_done_if_list_complete(item.shopping_list)
        _broadcast_shopping_list_item_update(item, request.user)
        return Response(ShoppingListItemSerializer(item, context={'request': request}).data, status=status.HTTP_200_OK)

    @action(detail=True, methods=['post'])
    def uncheck(self, request, pk=None):
        """Décocher (restore full) : remet checked_quantity à 0 sur toutes les quantités."""
        item = self.get_object()
        with transaction.atomic():
            item.quantities.update(checked_quantity=0, checked_at=None, checked_by=None)
            item.checked_at = None
            item.checked_by = None
            item.save(update_fields=['checked_at', 'checked_by', 'updated_at'])

        _mark_shopping_done_if_list_complete(item.shopping_list)
        _broadcast_shopping_list_item_update(item, request.user)
        return Response(ShoppingListItemSerializer(item, context={'request': request}).data, status=status.HTTP_200_OK)

    @action(detail=True, methods=['post'], url_path='set_manual_quantity')
    def set_manual_quantity(self, request, pk=None):
        """Définir la quantité manuelle (recipe_batch=None) pour cet item. Crée la ligne si besoin."""
        item = self.get_object()
        quantity = request.data.get('quantity', 0)
        manual_q = item.quantities.filter(recipe_batch__isnull=True).first()
        unit = request.data.get('unit') or (manual_q.unit if manual_q else None) or item.pantry_unit or 'g'
        try:
            qty_val = Decimal(str(quantity))
        except (TypeError, ValueError):
            qty_val = Decimal('0')
        with transaction.atomic():
            qty_obj, _ = ShoppingListItemQuantity.objects.get_or_create(
                shopping_list_item=item,
                recipe_batch=None,
                defaults={'quantity': qty_val, 'unit': unit}
            )
            if not _:
                qty_obj.quantity = qty_val
                qty_obj.unit = unit
                qty_obj.save(update_fields=['quantity', 'unit', 'updated_at'])
        _mark_shopping_done_if_list_complete(item.shopping_list)
        # Temps réel : informer les autres clients du changement de quantité manuelle
        _broadcast_shopping_list_item_update(item, request.user)
        return Response(ShoppingListItemSerializer(item, context={'request': request}).data, status=status.HTTP_200_OK)

    def _unit_group_for_unit(self, unit: str) -> str:
        """Détermine le unit_group à partir d'une unité"""
        unit = (unit or '').lower()
        if unit in ['g', 'kg']:
            return 'weight'
        if unit in ['ml', 'l']:
            return 'volume'
        if unit == 'piece':
            return 'count'
        if unit == 'pinch':
            return 'pinch'
        if unit == 'clove':
            return 'clove'
        return 'other'

    def _canonicalize_quantity(self, quantity: float, unit: str):
        """Canonicalise une quantité (convertit kg->g, l->ml)"""
        unit = (unit or '').lower()
        if unit == 'kg':
            return float(quantity) * 1000.0, 'g'
        if unit == 'l':
            return float(quantity) * 1000.0, 'ml'
        return float(quantity), unit

    def get_serializer_class(self):
        """Utiliser ShoppingListItemCreateSerializer pour la création manuelle"""
        if self.action == 'create':
            from .serializers import ShoppingListItemCreateSerializer
            return ShoppingListItemCreateSerializer
        return ShoppingListItemSerializer

    def create(self, request, *args, **kwargs):
        """
        Créer un item de liste de courses manuellement.
        Crée l'ingrédient s'il n'existe pas et détermine la catégorie automatiquement.
        """
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        
        shopping_list_id = serializer.validated_data['shopping_list_id']
        ingredient_name = serializer.validated_data['ingredient_name'].strip()
        quantity = serializer.validated_data.get('quantity', 1.0)
        unit = serializer.validated_data.get('unit', 'piece')
        category_id = serializer.validated_data.get('category_id')
        
        # Vérifier que l'utilisateur a accès à la liste
        try:
            shopping_list = ShoppingList.objects.get(id=shopping_list_id, members__user=request.user)
        except ShoppingList.DoesNotExist:
            return Response(
                {'error': 'Shopping list not found or access denied'},
                status=status.HTTP_404_NOT_FOUND
            )
        
        with transaction.atomic():
            # Récupérer ou créer l'ingrédient
            from .services.ingredient_matcher import get_or_create_ingredient
            ingredient, created = get_or_create_ingredient(ingredient_name)
            
            # Déterminer la catégorie si non fournie
            if not category_id and not ingredient.category:
                category = self._categorize_ingredient(ingredient_name, ingredient)
                if category:
                    ingredient.category = category
                    ingredient.save(update_fields=['category'])
            
            # Déterminer le unit_group à partir de l'unité
            unit_group = self._unit_group_for_unit(unit)
            
            # Créer ou récupérer le ShoppingListItem
            item, item_created = ShoppingListItem.objects.get_or_create(
                shopping_list=shopping_list,
                ingredient=ingredient,
                unit_group=unit_group,
                defaults={
                    'pantry_unit': unit,
                }
            )
            
            # Quantité manuelle = pas de recipe_batch (recipe_batch=None en DB)
            canonical_qty, canonical_unit = self._canonicalize_quantity(float(quantity), unit)
            quantity_obj, qty_created = ShoppingListItemQuantity.objects.get_or_create(
                shopping_list_item=item,
                recipe_batch=None,
                defaults={
                    'quantity': Decimal(str(canonical_qty)),
                    'unit': canonical_unit,
                }
            )
            if not qty_created:
                quantity_obj.quantity = Decimal(str(quantity_obj.quantity)) + Decimal(str(canonical_qty))
                quantity_obj.save(update_fields=['quantity', 'updated_at'])
            
            # Touch la liste pour que updated_at reflète bien la dernière modification
            shopping_list.updated_at = timezone.now()
            shopping_list.save(update_fields=['updated_at'])

        # Temps réel : informer les autres clients de la création/augmentation d'un item
        _mark_shopping_done_if_list_complete(shopping_list)
        _broadcast_shopping_list_item_update(item, request.user)

        return Response(
            ShoppingListItemSerializer(item, context={'request': request}).data,
            status=status.HTTP_201_CREATED if item_created else status.HTTP_200_OK
        )
    
    def _categorize_ingredient(self, ingredient_name: str, ingredient: Ingredient) -> Optional[Category]:
        from .services.ingredient_categorization import resolve_category_for_ingredient

        return resolve_category_for_ingredient(ingredient_name, ingredient)


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
        
        # Filtrer par owner si fourni dans les query params
        owner_id = self.request.query_params.get('owner')
        is_public_param = self.request.query_params.get('is_public')
        
        if owner_id:
            try:
                owner_id_int = int(owner_id)
                # Si on filtre par owner spécifique, montrer seulement ses collections publiques ou celles où l'utilisateur connecté est membre
                if is_public_param and is_public_param.lower() == 'true':
                    queryset = Collection.objects.filter(
                        owner_id=owner_id_int,
                        is_public=True
                    )
                else:
                    queryset = Collection.objects.filter(
                        Q(owner_id=owner_id_int, is_public=True) | 
                        Q(owner_id=owner_id_int, owner=user) |
                        Q(owner_id=owner_id_int, members__user=user)
                    ).distinct()
            except (ValueError, TypeError):
                queryset = Collection.objects.filter(
                    Q(is_public=True) | Q(owner=user) | Q(members__user=user)
                ).distinct()
        else:
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
        
        # Vérifier les permissions : créateur OU membre collaborateur (si collection collaborative)
        is_owner = collection.owner == user
        is_collaborator = collection.members.filter(user=user, role='collaborator').exists()
        can_add = is_owner or (collection.is_collaborative and is_collaborator)

        if not can_add:
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

    @action(detail=True, methods=['post'])
    def follow(self, request, pk=None):
        """Suivre un livre de recettes (l'ajouter à "Mes livres")"""
        collection = self.get_object()
        user = request.user

        if collection.owner == user:
            return Response(
                {'error': 'Vous possédez déjà ce livre'},
                status=status.HTTP_400_BAD_REQUEST
            )
        if not collection.is_public:
            return Response(
                {'error': 'Ce livre est privé'},
                status=status.HTTP_403_FORBIDDEN
            )

        _, created = CollectionFollower.objects.get_or_create(
            collection=collection,
            user=user
        )
        serializer = self.get_serializer(collection)
        return Response(serializer.data, status=status.HTTP_201_CREATED if created else status.HTTP_200_OK)

    @action(detail=True, methods=['post'])
    def unfollow(self, request, pk=None):
        """Ne plus suivre un livre de recettes"""
        collection = self.get_object()
        user = request.user

        deleted, _ = CollectionFollower.objects.filter(
            collection=collection,
            user=user
        ).delete()
        serializer = self.get_serializer(collection)
        return Response(serializer.data, status=status.HTTP_200_OK)
    
    @action(detail=False, methods=['get'])
    def my_collections(self, request):
        """Récupérer les collections de l'utilisateur : ses livres + livres suivis"""
        try:
            from django.db.models import Prefetch
            # Livres créés par l'utilisateur
            owned = Collection.objects.filter(
                owner=request.user
            ).select_related('owner').prefetch_related(
                Prefetch('followers', CollectionFollower.objects.filter(user=request.user)),
                'collection_recipes__recipe'
            ).annotate(
                total_recipes=Count('collection_recipes', distinct=True),
                last_activity=Max('collection_recipes__added_at')
            )
            # Livres suivis (d'autres utilisateurs)
            followed = Collection.objects.filter(
                followers__user=request.user
            ).select_related('owner').prefetch_related(
                Prefetch('followers', CollectionFollower.objects.filter(user=request.user)),
                'collection_recipes__recipe'
            ).annotate(
                total_recipes=Count('collection_recipes', distinct=True),
                last_activity=Max('collection_recipes__added_at')
            )

            # Fusionner et dédupliquer (owned a priorité)
            owned_ids = set(owned.values_list('id', flat=True))
            followed_excluding_owned = followed.exclude(id__in=owned_ids)

            collections = list(owned) + list(followed_excluding_owned)
            # Trier par last_activity
            collections.sort(key=lambda c: (c.last_activity or c.updated_at or c.created_at) or '', reverse=True)

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
        queryset = apply_dietary_exclusion(queryset, request.user)

        page = self.paginate_queryset(queryset)
        serializer = RecipeLightSerializer(
            page if page is not None else queryset,
            many=True,
            context={'request': request}
        )
        if page is not None:
            return self.get_paginated_response(serializer.data)
        return Response(serializer.data)


