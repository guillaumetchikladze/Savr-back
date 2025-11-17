from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from django.db.models import Q
from datetime import datetime, date
from time import perf_counter
from django.conf import settings
from django.db import connection
from urllib.parse import urlparse
import uuid
import boto3
from .models import Recipe, Step, Ingredient, RecipeIngredient, StepIngredient, MealPlan, MealInvitation, CookingProgress, Timer, Post, PostPhoto, PostCookie
def build_s3_client():
    """Créer un client S3 configuré selon l'environnement (AWS ou MinIO)."""
    config = {
        'aws_access_key_id': settings.AWS_ACCESS_KEY_ID,
        'aws_secret_access_key': settings.AWS_SECRET_ACCESS_KEY,
        'region_name': settings.AWS_S3_REGION_NAME,
    }
    if settings.AWS_ENDPOINT:
        config['endpoint_url'] = settings.AWS_ENDPOINT
        if settings.AWS_ENDPOINT.startswith('http://'):
            config['use_ssl'] = False
    return boto3.client('s3', **config)


# Les fonctions build_public_image_url et extract_storage_key ont été supprimées
# On utilise maintenant build_s3_url depuis settings.py pour construire les URLs


PHOTO_TYPES = [choice[0] for choice in PostPhoto.PHOTO_TYPE_CHOICES]
RESTRICTED_PHOTO_TYPES = PostPhoto.UNIQUE_TYPES
from .serializers import (
    RecipeSerializer, RecipeCreateSerializer, RecipeLightSerializer,
    StepSerializer, IngredientSerializer,
    MealPlanSerializer, MealInvitationSerializer,
    MealPlanListSerializer, MealPlanRangeListSerializer, MealPlanByDateSerializer,
    CookingProgressSerializer, CookingProgressCreateUpdateSerializer,
    TimerSerializer, TimerCreateSerializer,
    PostSerializer, PostCreateUpdateSerializer, PostPhotoSerializer
)


class RecipeViewSet(viewsets.ModelViewSet):
    """ViewSet pour les recettes"""
    queryset = Recipe.objects.all()
    permission_classes = [IsAuthenticated]
    
    def get_serializer_class(self):
        if self.action == 'create':
            return RecipeCreateSerializer
        # Utiliser RecipeLightSerializer pour les listes (pas besoin de steps/ingredients)
        if self.action in ['list', 'search']:
            return RecipeLightSerializer
        return RecipeSerializer
    
    def get_queryset(self):
        queryset = Recipe.objects.all()
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
        else:
            # Pour retrieve, update, etc. : précharger les steps avec leurs ingrédients
            from django.db.models import Prefetch
            queryset = queryset.prefetch_related(
                Prefetch('steps', queryset=Step.objects.prefetch_related(
                    Prefetch('step_ingredients', queryset=StepIngredient.objects.select_related('ingredient'))
                )),
                'recipe_ingredients__ingredient',
            )
        
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
            
            if settings.DEBUG:
                t_ser_end = perf_counter()
                total_ms = (t_ser_end - t0) * 1000
                qs_ms = (t_qs_end - t_qs_start) * 1000 if 't_qs_end' in locals() else 0
                ser_ms = (t_ser_end - (t_qs_end if 't_qs_end' in locals() else t0)) * 1000
                print(f"[RecipeViewSet.list] count={count} items={len(page)} qs_ms={qs_ms:.1f} ser_ms={ser_ms:.1f} "
                      f"db_queries={db_queries} db_time_ms={db_time_ms:.1f} total_ms={total_ms:.1f}")
            
            return self.get_paginated_response(serializer.data)
        
        serializer = self.get_serializer(queryset, many=True)
        return Response(serializer.data)
    
    def perform_create(self, serializer):
        serializer.save(created_by=self.request.user)
    
    @action(detail=False, methods=['get'])
    def my_recipes(self, request):
        """Récupérer les recettes de l'utilisateur connecté"""
        recipes = Recipe.objects.filter(created_by=request.user)
        serializer = self.get_serializer(recipes, many=True)
        return Response(serializer.data)


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
        qs = MealPlan.objects.filter(user=self.request.user)
        
        # Filtres de date (format YYYY-MM-DD)
        date_gte = self.request.query_params.get('date__gte')
        date_lte = self.request.query_params.get('date__lte')
        if date_gte:
            qs = qs.filter(date__gte=date_gte)
        if date_lte:
            qs = qs.filter(date__lte=date_lte)
        
        # Autres filtres éventuels
        meal_time = self.request.query_params.get('meal_time')
        if meal_time:
            qs = qs.filter(meal_time=meal_time)
        confirmed = self.request.query_params.get('confirmed')
        if confirmed in ('true', 'false'):
            qs = qs.filter(confirmed=(confirmed == 'true'))
        
        # Chargement optimisé des relations utilisées par le serializer
        if self.action in ['list']:
            qs = qs.select_related('recipe').order_by('-date', 'meal_time')
        elif self.action in ['by_date']:
            from django.db.models import Prefetch
            qs = qs.select_related('user', 'recipe').prefetch_related(
                Prefetch('invitations', queryset=MealInvitation.objects.select_related('invitee')),
            ).order_by('-date', 'meal_time')
        elif self.action in ['by_week', 'by_dates', 'bulk']:
            qs = qs.select_related('user', 'recipe').order_by('-date', 'meal_time')
        else:
            # Pour update, retrieve, etc. : préfetch les invitations si le serializer en a besoin
            from django.db.models import Prefetch
            from .models import StepIngredient
            qs = qs.select_related('user', 'recipe').prefetch_related(
                Prefetch('invitations', queryset=MealInvitation.objects.select_related('invitee')),
                Prefetch('recipe__steps', queryset=Step.objects.prefetch_related(
                    Prefetch('step_ingredients', queryset=StepIngredient.objects.select_related('ingredient'))
                )),
                'recipe__recipe_ingredients__ingredient',
            ).order_by('-date', 'meal_time')
        return qs
    
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
            objects = queryset
        
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
        meal_plans = self.get_queryset().filter(date=target_date)
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
    
    @action(detail=True, methods=['post'])
    def invite(self, request, pk=None):
        """Inviter des utilisateurs à un repas"""
        from django.contrib.auth import get_user_model
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
        
        # Créer les invitations
        invitations = []
        for invitee_id in valid_invitee_ids:
            invitee = User.objects.get(id=invitee_id)
            invitation, created = MealInvitation.objects.get_or_create(
                inviter=request.user,
                invitee=invitee,
                meal_plan=meal_plan,
                defaults={'status': 'pending'}
            )
            if created:
                invitations.append(invitation)
                # Créer une notification
                Notification.objects.create(
                    user=invitee,
                    notification_type='meal_invitation',
                    title=f"{request.user.username} vous invite à un repas",
                    message=f"{request.user.username} vous invite à {meal_plan.get_meal_time_display()} le {meal_plan.date.strftime('%d/%m/%Y')}",
                    related_user=request.user
                )
        
        serializer = MealInvitationSerializer(invitations, many=True, context={'request': request})
        return Response(serializer.data, status=status.HTTP_201_CREATED)


class MealInvitationViewSet(viewsets.ModelViewSet):
    """ViewSet pour les invitations à des repas"""
    serializer_class = MealInvitationSerializer
    permission_classes = [IsAuthenticated]
    
    def get_queryset(self):
        # L'utilisateur peut voir les invitations qu'il a envoyées ou reçues
        return MealInvitation.objects.filter(
            Q(inviter=self.request.user) | Q(invitee=self.request.user)
        ).select_related('inviter', 'invitee', 'meal_plan', 'meal_plan__recipe')
    
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
            return Response(None, status=status.HTTP_404_NOT_FOUND)
    
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
        if is_published is not None and is_published.lower() == 'true':
            queryset = Post.objects.filter(is_published=True)
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
        
        return queryset.select_related('user', 'recipe', 'meal_plan', 'cooking_progress').prefetch_related('photos', 'cookies').order_by('-created_at')
    
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
