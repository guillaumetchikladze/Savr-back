from rest_framework import status, generics
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework_simplejwt.tokens import RefreshToken
from django.contrib.auth import get_user_model
from django.db.models import Q, Exists, OuterRef
from django.contrib.postgres.search import SearchVector, SearchQuery, SearchRank, TrigramSimilarity
from django.conf import settings
import uuid
from .permissions import IsValidated
from .serializers import (
    UserRegistrationSerializer,
    UserSerializer,
    UserSearchResultSerializer,
    LoginSerializer,
    NotificationSerializer,
    LoyaltyCardSerializer,
    FeedUserSuggestionSerializer,
)
from .feed_suggestions import build_feed_user_suggestions
from .serializers_push import PushDeviceRegisterSerializer
from .models import Follow, FollowRequest, Notification, PushDevice, LoyaltyCard, UserBlock
from .blocks import are_blocked_either_way, blocked_user_ids_for, exclude_blocked_from_user_qs
from .services.follow_service import (
    accept_follow_request,
    decline_follow_request,
    request_follow,
    unfollow_or_cancel,
)
from .services.expo_push import send_expo_push_notifications
import random
from recipes.models import Recipe, ShoppingListLoyaltyCard, ShoppingListMember
from recipes.serializers import RecipeSerializer
from savr_back.settings import build_s3_client, build_presigned_get_url, build_s3_url
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.core.cache import cache
from django.contrib.auth.tokens import default_token_generator
from django.utils.http import urlsafe_base64_encode, urlsafe_base64_decode
from django.utils.encoding import force_bytes, force_str
from django.shortcuts import render, redirect
from django.views.decorators.http import require_http_methods

User = get_user_model()


def _get_client_ip(request) -> str:
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return (request.META.get("REMOTE_ADDR") or "").strip() or "unknown"


def _rate_limit_key(prefix: str, value: str) -> str:
    return f"rl:{prefix}:{value}"


def _get_public_base_url() -> str:
    base = (getattr(settings, "PUBLIC_BASE_URL", "") or "").strip()
    return base.rstrip("/")


@api_view(['POST'])
@permission_classes([AllowAny])
def register_view(request):
    """Register a new user"""
    serializer = UserRegistrationSerializer(data=request.data)
    if serializer.is_valid():
        user = serializer.save()
        # Enqueue welcome email (non bloquant)
        try:
            from emails.services import enqueue_email

            from_email = (getattr(settings, "EMAIL_FROM_ADDRESS", "") or "").strip() or "noreply@tchikook.fr"
            login_url = (request.data.get("login_url") or "").strip()

            enqueue_email(
                from_email=from_email,
                to_email=user.email,
                subject="Bienvenue sur Tchikook",
                content={
                    "template_name": "emails/welcome",
                    "context": {
                        "username": user.username or user.email,
                        "login_url": login_url,
                    },
                },
                action_name="welcome",
                priority="NORMAL",
                user_id=user.id,
            )
        except Exception:
            # Ne pas bloquer la création de compte si l'email échoue.
            pass
        refresh = RefreshToken.for_user(user)
        return Response({
            'message': 'Utilisateur créé avec succès',
            'user': UserSerializer(user).data,
            'tokens': {
                'refresh': str(refresh),
                'access': str(refresh.access_token),
            }
        }, status=status.HTTP_201_CREATED)
    return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


@api_view(['POST'])
@permission_classes([AllowAny])
def login_view(request):
    """Login user and return JWT tokens"""
    serializer = LoginSerializer(data=request.data, context={'request': request})
    if serializer.is_valid():
        user = serializer.validated_data['user']
        refresh = RefreshToken.for_user(user)
        return Response({
            'message': 'Connexion réussie',
            'user': UserSerializer(user).data,
            'tokens': {
                'refresh': str(refresh),
                'access': str(refresh.access_token),
            }
        }, status=status.HTTP_200_OK)
    return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


@api_view(['GET', 'PATCH'])
@permission_classes([IsAuthenticated])
def profile_view(request):
    """Get or update current user profile"""
    if request.method == 'GET':
        serializer = UserSerializer(request.user, context={'request': request})
        return Response(serializer.data, status=status.HTTP_200_OK)
    elif request.method == 'PATCH':
        # Mise à jour du profil (notamment avatar_url)
        serializer = UserSerializer(request.user, data=request.data, partial=True, context={'request': request})
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data, status=status.HTTP_200_OK)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


@api_view(['POST'])
@permission_classes([IsValidated])
def upload_avatar_view(request):
    """Générer une URL pré-signée pour uploader un avatar et mettre à jour le profil"""
    try:
        s3_client = build_s3_client()
        bucket_name = settings.AWS_BUCKET
        
        if not bucket_name:
            return Response(
                {'error': 'S3 bucket non configuré'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )
        
        # Générer un nom de fichier unique pour l'avatar
        unique_id = str(uuid.uuid4()).replace('-', '')
        file_name = f"avatars/{request.user.id}/{unique_id}.jpg"
        
        # Générer l'URL pré-signée pour l'upload (valide 5 minutes)
        presigned_url = s3_client.generate_presigned_url(
            'put_object',
            Params={
                'Bucket': bucket_name,
                'Key': file_name,
                'ContentType': 'image/jpeg',
            },
            ExpiresIn=300  # 5 minutes
        )
        
        # Construire l'URL permanente (sera convertie en presigned URL par le serializer)
        avatar_url = build_s3_url(file_name)
        
        return Response({
            'presigned_url': presigned_url,
            'avatar_url': avatar_url,
            'image_path': file_name,
        }, status=status.HTTP_200_OK)
    except Exception as e:
        return Response(
            {'error': f'Erreur lors de la génération de l\'URL: {str(e)}'},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR
        )


@api_view(['POST'])
@permission_classes([IsValidated])
def confirm_avatar_upload_view(request):
    """Confirmer l'upload de l'avatar et mettre à jour le profil utilisateur"""
    image_path = request.data.get('image_path')
    if not image_path:
        return Response(
            {'error': 'image_path requis'},
            status=status.HTTP_400_BAD_REQUEST
        )
    
    try:
        # Construire l'URL permanente de l'avatar (pas pré-signée car elle expire)
        avatar_url = build_s3_url(image_path)
        
        # Mettre à jour l'avatar_url de l'utilisateur
        request.user.avatar_url = avatar_url
        request.user.save()
        
        serializer = UserSerializer(request.user, context={'request': request})
        return Response({
            'message': 'Avatar mis à jour avec succès',
            'user': serializer.data,
        }, status=status.HTTP_200_OK)
    except Exception as e:
        import traceback
        error_details = traceback.format_exc()
        print(f"Error in confirm_avatar_upload_view: {error_details}")
        return Response(
            {'error': f'Erreur lors de la mise à jour de l\'avatar: {str(e)}'},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR
        )


@api_view(['GET'])
@permission_classes([IsValidated])
def user_detail_view(request, user_id):
    """Récupérer les informations d'un utilisateur spécifique avec les statuts de suivi"""
    try:
        target_user = User.objects.get(id=user_id)
    except User.DoesNotExist:
        return Response({'error': 'Utilisateur non trouvé'}, status=status.HTTP_404_NOT_FOUND)

    if are_blocked_either_way(request.user, target_user):
        return Response(
            {'error': 'Ce profil n’est pas disponible.', 'blocked': True},
            status=status.HTTP_403_FORBIDDEN,
        )

    serializer = UserSerializer(target_user, context={'request': request})
    return Response(serializer.data, status=status.HTTP_200_OK)


@api_view(['GET'])
@permission_classes([IsValidated])
def search_view(request):
    """Recherche intelligente d'utilisateurs et de recettes"""
    query = request.query_params.get('q', '').strip()
    user_id = request.query_params.get('id')
    
    # Si un ID est fourni, retourner directement l'utilisateur
    if user_id:
        try:
            user = User.objects.get(id=int(user_id))
            if are_blocked_either_way(request.user, user):
                return Response({
                    'users': [],
                    'recipes': [],
                }, status=status.HTTP_200_OK)
            serializer = UserSearchResultSerializer(user, context={'request': request})
            return Response({
                'users': [serializer.data],
                'recipes': [],
            }, status=status.HTTP_200_OK)
        except (User.DoesNotExist, ValueError, TypeError):
            return Response({
                'users': [],
                'recipes': [],
            }, status=status.HTTP_200_OK)
    
    if not query:
        # Retourner des suggestions (utilisateurs et recettes populaires)
        users = exclude_blocked_from_user_qs(
            User.objects.exclude(id=request.user.id).order_by('-created_at'),
            request.user,
        )[:10]
        recipes = Recipe.objects.select_related('created_by').prefetch_related(
            'steps', 'recipe_ingredients__ingredient'
        ).order_by('-created_at')[:10]
        
        users_serializer = UserSearchResultSerializer(users, many=True, context={'request': request})
        recipes_serializer = RecipeSerializer(recipes, many=True)
        
        return Response({
            'users': users_serializer.data,
            'recipes': recipes_serializer.data,
        }, status=status.HTTP_200_OK)
    
    # Recherche fuzzy sur le username uniquement (pas d'email)
    try:
        users = User.objects.exclude(id=request.user.id).annotate(
            username_similarity=TrigramSimilarity('username', query),
        ).filter(
            Q(username__icontains=query) | Q(username_similarity__gt=0.2)
        ).order_by('-username_similarity')
    except Exception:
        users = User.objects.filter(
            Q(username__icontains=query)
        ).exclude(id=request.user.id)

    users = exclude_blocked_from_user_qs(users, request.user)
    
    # Recherche fuzzy pour les recettes avec PostgreSQL Full-Text Search
    try:
        # Utilise SearchVector pour indexer titre et description
        search_vector = SearchVector('title', weight='A', config='french') + \
                        SearchVector('description', weight='B', config='french')
        search_query = SearchQuery(query, config='french')
        
        # Recherche avec trigram similarity pour une recherche plus flexible
        # Fallback si l'extension pg_trgm n'est pas disponible
        try:
            recipes = Recipe.objects.select_related('created_by').prefetch_related(
                'steps', 'recipe_ingredients__ingredient'
            ).annotate(
                similarity=TrigramSimilarity('title', query) + 
                           TrigramSimilarity('description', query),
                search_rank=SearchRank(search_vector, search_query)
            ).filter(
                Q(search_rank__gt=0) | Q(similarity__gt=0.1)
            ).order_by('-search_rank', '-similarity')[:20]  # Limiter à 20 résultats
        except Exception:
            # Fallback sans trigram similarity
            recipes = Recipe.objects.select_related('created_by').prefetch_related(
                'steps', 'recipe_ingredients__ingredient'
            ).annotate(
                search_rank=SearchRank(search_vector, search_query)
            ).filter(
                search_rank__gt=0
            ).order_by('-search_rank')[:20]  # Limiter à 20 résultats
    except Exception:
        # Fallback avec recherche simple si Full-Text Search n'est pas disponible
        # Recherche intelligente avec plusieurs critères
        query_words = query.split()
        recipes_query = Q()
        for word in query_words:
            recipes_query |= Q(title__icontains=word) | Q(description__icontains=word)
        recipes = Recipe.objects.select_related('created_by').prefetch_related(
            'steps', 'recipe_ingredients__ingredient'
        ).filter(recipes_query).distinct()[:20]  # Limiter à 20 résultats
    
    # Limiter les utilisateurs à 10 résultats
    users = users[:10]
    
    users_serializer = UserSearchResultSerializer(users, many=True, context={'request': request})
    recipes_serializer = RecipeSerializer(recipes, many=True)
    
    return Response({
        'users': users_serializer.data,
        'recipes': recipes_serializer.data,
    }, status=status.HTTP_200_OK)


@api_view(['POST', 'DELETE'])
@permission_classes([IsValidated])
def follow_user_view(request, user_id):
    """Demander à suivre, s'abonner en retour (auto) ou se désabonner / annuler une demande."""
    try:
        target_user = User.objects.get(id=user_id)
    except User.DoesNotExist:
        return Response({'error': 'Utilisateur non trouvé'}, status=status.HTTP_404_NOT_FOUND)

    if target_user.id == request.user.id:
        return Response({'error': 'Vous ne pouvez pas vous suivre vous-même'}, status=status.HTTP_400_BAD_REQUEST)

    if are_blocked_either_way(request.user, target_user):
        return Response(
            {'error': 'Action impossible : un blocage est en place entre ces comptes.'},
            status=status.HTTP_403_FORBIDDEN,
        )

    if request.method == 'POST':
        result = request_follow(request.user, target_user)
        action = result.pop('action')
        message = result.pop('message')
        status_code = status.HTTP_201_CREATED if action in {
            'request_sent', 'followed_auto', 'mutual_established',
        } else status.HTTP_200_OK
        return Response({'action': action, 'message': message, **result}, status=status_code)

    result = unfollow_or_cancel(request.user, target_user)
    action = result.pop('action')
    message = result.pop('message')
    return Response({'action': action, 'message': message, **result}, status=status.HTTP_200_OK)


@api_view(['POST'])
@permission_classes([IsValidated])
def accept_follow_request_view(request, user_id):
    """Accepter une demande de suivi reçue."""
    try:
        requester = User.objects.get(id=user_id)
    except User.DoesNotExist:
        return Response({'error': 'Utilisateur non trouvé'}, status=status.HTTP_404_NOT_FOUND)

    if requester.id == request.user.id:
        return Response({'error': 'Action invalide'}, status=status.HTTP_400_BAD_REQUEST)

    if are_blocked_either_way(request.user, requester):
        return Response(
            {'error': 'Action impossible : un blocage est en place entre ces comptes.'},
            status=status.HTTP_403_FORBIDDEN,
        )

    result = accept_follow_request(request.user, requester)
    action = result.pop('action')
    message = result.pop('message')
    if action == 'not_found':
        return Response({'error': message, **result}, status=status.HTTP_404_NOT_FOUND)
    return Response({'action': action, 'message': message, **result}, status=status.HTTP_200_OK)


@api_view(['POST'])
@permission_classes([IsValidated])
def decline_follow_request_view(request, user_id):
    """Refuser une demande de suivi reçue."""
    try:
        requester = User.objects.get(id=user_id)
    except User.DoesNotExist:
        return Response({'error': 'Utilisateur non trouvé'}, status=status.HTTP_404_NOT_FOUND)

    if requester.id == request.user.id:
        return Response({'error': 'Action invalide'}, status=status.HTTP_400_BAD_REQUEST)

    result = decline_follow_request(request.user, requester)
    action = result.pop('action')
    message = result.pop('message')
    if action == 'not_found':
        return Response({'error': message, **result}, status=status.HTTP_404_NOT_FOUND)
    return Response({'action': action, 'message': message, **result}, status=status.HTTP_200_OK)


@api_view(['GET'])
@permission_classes([IsValidated])
def notifications_view(request):
    """Récupérer toutes les notifications de l'utilisateur"""
    # Pagination + queryset optimisé:
    # - Evite de renvoyer "tout" (payload énorme + sérialisation lente)
    # - Evite le N+1 sur `is_following` via annotation Exists
    from rest_framework.pagination import PageNumberPagination

    paginator = PageNumberPagination()
    paginator.page_size = 30
    paginator.page_size_query_param = 'page_size'
    paginator.max_page_size = 100

    follow_exists = Follow.objects.filter(
        follower=request.user,
        following_id=OuterRef('related_user_id'),
    )

    qs = (
        Notification.objects
        .filter(user=request.user)
        .select_related('related_user')
        .annotate(related_user_is_following=Exists(follow_exists))
        .only(
            'id',
            'notification_type',
            'title',
            'message',
            'related_post_id',
            'is_read',
            'created_at',
            'related_user_id',
            'related_user__id',
            'related_user__username',
            'related_user__avatar_url',
        )
        .order_by('-created_at')
    )

    page = paginator.paginate_queryset(qs, request)
    serializer = NotificationSerializer(page, many=True, context={'request': request})
    return paginator.get_paginated_response(serializer.data)


@api_view(['GET'])
@permission_classes([IsValidated])
def unread_notifications_count_view(request):
    """Récupérer le nombre de notifications non lues"""
    count = Notification.objects.filter(user=request.user, is_read=False).count()
    return Response({'count': count}, status=status.HTTP_200_OK)


@api_view(['POST'])
@permission_classes([IsValidated])
def mark_notification_read_view(request, notification_id):
    """Marquer une notification comme lue"""
    try:
        notification = Notification.objects.get(id=notification_id, user=request.user)
        notification.is_read = True
        notification.save()
        return Response({'message': 'Notification marquée comme lue'}, status=status.HTTP_200_OK)
    except Notification.DoesNotExist:
        return Response({'error': 'Notification non trouvée'}, status=status.HTTP_404_NOT_FOUND)


@api_view(['POST'])
@permission_classes([IsValidated])
def mark_all_notifications_read_view(request):
    """Marquer toutes les notifications comme lues"""
    Notification.objects.filter(user=request.user, is_read=False).update(is_read=True)
    return Response({'message': 'Toutes les notifications ont été marquées comme lues'}, status=status.HTTP_200_OK)


@api_view(['GET'])
@permission_classes([IsValidated])
def complices_view(request):
    """Récupérer tous les complices de l'utilisateur (following + followers)"""
    # Récupérer les utilisateurs suivis (following)
    following = Follow.objects.filter(follower=request.user).select_related('following')
    following_users = [follow.following for follow in following]
    
    # Récupérer les followers
    followers = Follow.objects.filter(following=request.user).select_related('follower')
    follower_users = [follow.follower for follow in followers]
    
    # Combiner et dédupliquer
    all_complices = {}
    for user in following_users + follower_users:
        if user.id not in all_complices:
            all_complices[user.id] = user
    
    complices = list(all_complices.values())
    
    # Trier par nom d'utilisateur
    complices.sort(key=lambda u: u.username)
    
    serializer = UserSerializer(complices, many=True, context={'request': request})
    return Response(serializer.data, status=status.HTTP_200_OK)


@api_view(['GET'])
@permission_classes([IsValidated])
def followers_list_view(request):
    """Récupérer uniquement les abonnés de l'utilisateur (followers)"""
    followers_qs = Follow.objects.filter(following=request.user).select_related('follower')
    followers = [follow.follower for follow in followers_qs]
    serializer = UserSerializer(followers, many=True, context={'request': request})
    return Response(serializer.data, status=status.HTTP_200_OK)


@api_view(['GET'])
@permission_classes([IsValidated])
def following_list_view(request):
    """Récupérer uniquement les abonnements de l'utilisateur (following)"""
    following_qs = Follow.objects.filter(follower=request.user).select_related('following')
    following = [follow.following for follow in following_qs]
    serializer = UserSerializer(following, many=True, context={'request': request})
    return Response(serializer.data, status=status.HTTP_200_OK)


@api_view(['GET'])
@permission_classes([IsValidated])
def user_followers_view(request, user_id):
    """Récupérer les abonnés d'un utilisateur spécifique"""
    try:
        target_user = User.objects.get(id=user_id)
    except User.DoesNotExist:
        return Response({'error': 'Utilisateur non trouvé'}, status=status.HTTP_404_NOT_FOUND)

    if are_blocked_either_way(request.user, target_user):
        return Response([], status=status.HTTP_200_OK)

    blocked_ids = blocked_user_ids_for(request.user)
    followers_qs = Follow.objects.filter(following=target_user).select_related('follower')
    followers = [
        follow.follower
        for follow in followers_qs
        if follow.follower_id not in blocked_ids
    ]
    serializer = UserSerializer(followers, many=True, context={'request': request})
    return Response(serializer.data, status=status.HTTP_200_OK)


@api_view(['GET'])
@permission_classes([IsValidated])
def user_following_view(request, user_id):
    """Récupérer les abonnements d'un utilisateur spécifique"""
    try:
        target_user = User.objects.get(id=user_id)
    except User.DoesNotExist:
        return Response({'error': 'Utilisateur non trouvé'}, status=status.HTTP_404_NOT_FOUND)

    if are_blocked_either_way(request.user, target_user):
        return Response([], status=status.HTTP_200_OK)

    blocked_ids = blocked_user_ids_for(request.user)
    following_qs = Follow.objects.filter(follower=target_user).select_related('following')
    following = [
        follow.following
        for follow in following_qs
        if follow.following_id not in blocked_ids
    ]
    serializer = UserSerializer(following, many=True, context={'request': request})
    return Response(serializer.data, status=status.HTTP_200_OK)

@api_view(['GET'])
@permission_classes([IsValidated])
def users_search_view(request):
    """Recherche d'utilisateurs pour autocomplete @mention (retourne id, username, avatar_url)"""
    from recipes.serializers import UserLightSerializer
    query = (request.query_params.get('q') or '').strip()
    if len(query) < 2:
        return Response([], status=status.HTTP_200_OK)
    users = User.objects.filter(
        Q(username__icontains=query) | Q(username__istartswith=query)
    ).exclude(id=request.user.id).order_by('username')
    users = exclude_blocked_from_user_qs(users, request.user)[:15]
    serializer = UserLightSerializer(users, many=True, context={'request': request})
    return Response(serializer.data, status=status.HTTP_200_OK)


@api_view(['GET'])
@permission_classes([IsValidated])
def user_by_username_view(request):
    """Récupérer un utilisateur par son username exact (pour les liens @mention)"""
    username = (request.query_params.get('username') or '').strip()
    if not username:
        return Response({'error': 'username requis'}, status=status.HTTP_400_BAD_REQUEST)
    try:
        user = User.objects.get(username__iexact=username)
        return Response({'id': user.id, 'username': user.username}, status=status.HTTP_200_OK)
    except User.DoesNotExist:
        return Response({'error': 'Utilisateur non trouvé'}, status=status.HTTP_404_NOT_FOUND)


@api_view(['POST'])
@permission_classes([IsValidated])
def register_push_device_view(request):
    """
    Enregistrer ou mettre à jour un appareil capable de recevoir des notifications push Expo.
    """
    serializer = PushDeviceRegisterSerializer(data=request.data, context={'request': request})
    if not serializer.is_valid():
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    device = serializer.save()
    return Response(
        {
            'id': device.id,
            'expo_push_token': device.expo_push_token,
            'platform': device.platform,
            'is_active': device.is_active,
        },
        status=status.HTTP_200_OK,
    )


@api_view(['POST'])
@permission_classes([IsValidated])
def test_push_device_view(request):
    """
    Endpoint de test pour envoyer une notification push immédiate
    au(x) device(s) enregistrés de l'utilisateur courant.
    """
    devices = PushDevice.objects.filter(user=request.user, is_active=True).exclude(expo_push_token='').all()
    if not devices:
        return Response({'error': 'Aucun device push actif pour cet utilisateur'}, status=status.HTTP_400_BAD_REQUEST)

    messages = []
    for device in devices:
        messages.append(
            {
                'to': device.expo_push_token,
                'title': 'Test notifications Tchikook',
                'body': 'Si tu vois ceci, la push backend fonctionne.',
                'data': {
                    'source': 'debug',
                    'kind': 'test_push',
                },
                'sound': 'default',
            }
        )

    send_expo_push_notifications(messages)
    return Response({'message': f'Push de test envoyée à {len(messages)} device(s).'}, status=status.HTTP_200_OK)


def _password_reset_from_email():
    return (getattr(settings, "EMAIL_FROM_ADDRESS", "") or "").strip() or "noreply@tchikook.fr"


def _password_reset_response_payload(**extra):
    from_email = _password_reset_from_email()
    return {
        'message': 'Si le compte existe, un email a été envoyé.',
        'from_email': from_email,
        **extra,
    }


@api_view(['POST'])
@permission_classes([AllowAny])
def password_reset_request_view(request):
    """
    Envoie un lien de reset password (token Django) par email.
    Réponse non discriminante (anti-énumération).
    """
    ip = _get_client_ip(request)
    email = (request.data.get('email') or '').strip().lower()
    ttl_seconds = 5
    key_ip = _rate_limit_key("pwreset:ip", ip)
    key_email = _rate_limit_key("pwreset:email", email or "empty")
    if not cache.add(key_ip, "1", timeout=ttl_seconds) or not cache.add(key_email, "1", timeout=ttl_seconds):
        return Response(
            _password_reset_response_payload(retry_after_seconds=ttl_seconds),
            status=429,
        )

    # Réponse OK même si vide/invalide
    if not email:
        return Response(_password_reset_response_payload(), status=status.HTTP_200_OK)

    user = User.objects.filter(email__iexact=email, is_active=True).first()
    if user:
        uidb64 = urlsafe_base64_encode(force_bytes(user.pk))
        token = default_token_generator.make_token(user)
        base = _get_public_base_url()
        # Route web (sans /api) demandée.
        reset_url = f"{base}/auth/password-reset/{uidb64}/{token}/" if base else ""

        try:
            from emails.services import enqueue_email

            from_email = _password_reset_from_email()
            enqueue_email(
                from_email=from_email,
                to_email=user.email,
                subject="Réinitialiser ton mot de passe Tchikook",
                content={
                    "template_name": "emails/password_reset",
                    "context": {
                        "reset_url": reset_url,
                    },
                },
                action_name="password_reset",
                priority="HIGH",
                user_id=user.id,
            )
        except Exception:
            pass

    return Response(_password_reset_response_payload(), status=status.HTTP_200_OK)


@require_http_methods(["GET", "POST"])
def password_reset_confirm_view(request, uidb64: str, token: str):
    """
    Page web Django (sans auth) pour choisir un nouveau mot de passe.
    """
    user = None
    try:
        uid = force_str(urlsafe_base64_decode(uidb64))
        user = User.objects.filter(pk=uid, is_active=True).first()
    except Exception:
        user = None

    valid = bool(user and default_token_generator.check_token(user, token))
    if not valid:
        return render(
            request,
            "auth/password_reset_confirm.html",
            {"valid": False},
            status=400,
        )

    if request.method == "POST":
        new_password = (request.POST.get("new_password") or "").strip()
        confirm_password = (request.POST.get("confirm_password") or "").strip()
        if not new_password or new_password != confirm_password:
            return render(
                request,
                "auth/password_reset_confirm.html",
                {"valid": True, "error": "Les mots de passe ne correspondent pas."},
                status=400,
            )
        try:
            validate_password(new_password, user=user)
        except ValidationError as e:
            return render(
                request,
                "auth/password_reset_confirm.html",
                {"valid": True, "error": "Mot de passe invalide.", "details": list(e.messages)},
                status=400,
            )
        user.set_password(new_password)
        user.save(update_fields=["password"])
        return render(request, "auth/password_reset_confirm.html", {"valid": True, "success": True})

    return render(request, "auth/password_reset_confirm.html", {"valid": True})


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def change_password_view(request):
    old_password = (request.data.get('old_password') or '').strip()
    new_password = (request.data.get('new_password') or '').strip()
    if not old_password or not new_password:
        return Response({'error': 'old_password et new_password requis.'}, status=status.HTTP_400_BAD_REQUEST)

    user = request.user
    if not user.check_password(old_password):
        return Response({'error': 'Ancien mot de passe incorrect.'}, status=status.HTTP_400_BAD_REQUEST)

    try:
        validate_password(new_password, user=user)
    except ValidationError as e:
        return Response({'error': 'Mot de passe invalide.', 'details': list(e.messages)}, status=status.HTTP_400_BAD_REQUEST)

    user.set_password(new_password)
    user.save(update_fields=['password'])
    return Response({'message': 'Mot de passe mis à jour.'}, status=status.HTTP_200_OK)


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def delete_account_view(request):
    """
    Suppression définitive du compte (Guideline Apple 5.1.1).
    Body: { "password": "..." }
    """
    password = (request.data.get('password') or '').strip()
    if not password:
        return Response({'error': 'Mot de passe requis.'}, status=status.HTTP_400_BAD_REQUEST)

    user = request.user
    if not user.check_password(password):
        return Response({'error': 'Mot de passe incorrect.'}, status=status.HTTP_400_BAD_REQUEST)

    user_id = user.id
    # Hard-delete : cascades Django + SET_NULL sur contenus liés (recettes, etc.)
    user.delete()
    return Response(
        {'message': 'Compte supprimé.', 'id': user_id},
        status=status.HTTP_200_OK,
    )


def _sever_follow_relations(user_a, user_b):
    """Coupe follows et demandes dans les deux sens."""
    Follow.objects.filter(
        Q(follower=user_a, following=user_b) | Q(follower=user_b, following=user_a)
    ).delete()
    FollowRequest.objects.filter(
        Q(requester=user_a, target=user_b) | Q(requester=user_b, target=user_a)
    ).delete()


@api_view(['POST', 'DELETE'])
@permission_classes([IsValidated])
def block_user_view(request, user_id):
    """POST = bloquer ; DELETE = débloquer."""
    try:
        target = User.objects.get(id=user_id)
    except User.DoesNotExist:
        return Response({'error': 'Utilisateur non trouvé'}, status=status.HTTP_404_NOT_FOUND)

    if target.id == request.user.id:
        return Response({'error': 'Vous ne pouvez pas vous bloquer vous-même.'}, status=status.HTTP_400_BAD_REQUEST)

    if request.method == 'DELETE':
        deleted, _ = UserBlock.objects.filter(blocker=request.user, blocked_id=user_id).delete()
        if not deleted:
            return Response(
                {'error': 'Cet utilisateur n’est pas dans votre liste de blocage.'},
                status=status.HTTP_404_NOT_FOUND,
            )
        return Response(
            {'message': 'Utilisateur débloqué.', 'unblocked_user_id': user_id},
            status=status.HTTP_200_OK,
        )

    block, created = UserBlock.objects.get_or_create(blocker=request.user, blocked=target)
    _sever_follow_relations(request.user, target)

    return Response(
        {
            'message': f'@{target.username} a été bloqué.' if target.username else 'Utilisateur bloqué.',
            'blocked_user_id': target.id,
            'created': created,
            'created_at': block.created_at,
        },
        status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
    )


@api_view(['GET'])
@permission_classes([IsValidated])
def blocked_users_list_view(request):
    """Liste paginée des utilisateurs bloqués par l’utilisateur courant."""
    from rest_framework.pagination import PageNumberPagination

    paginator = PageNumberPagination()
    paginator.page_size = 30
    paginator.page_size_query_param = 'page_size'
    paginator.max_page_size = 100

    qs = (
        UserBlock.objects.filter(blocker=request.user)
        .select_related('blocked')
        .order_by('-created_at')
    )
    page = paginator.paginate_queryset(qs, request)
    data = [
        {
            'id': row.blocked_id,
            'username': row.blocked.username,
            'avatar_url': row.blocked.avatar_url,
            'blocked_at': row.created_at,
        }
        for row in page
    ]
    return paginator.get_paginated_response(data)


class LoyaltyCardListCreateView(generics.ListCreateAPIView):
    """
    Lister et créer les cartes de fidélité de l'utilisateur courant.
    """

    serializer_class = LoyaltyCardSerializer
    permission_classes = [IsValidated]

    def get_queryset(self):
        return (
            LoyaltyCard.objects.filter(owner=self.request.user, is_active=True)
            .order_by('-created_at')
        )


class LoyaltyCardDetailView(generics.RetrieveUpdateDestroyAPIView):
    """
    Détail / suppression logique d'une carte de fidélité.
    """

    serializer_class = LoyaltyCardSerializer
    permission_classes = [IsValidated]

    def get_queryset(self):
        return LoyaltyCard.objects.filter(owner=self.request.user, is_active=True)

    def perform_destroy(self, instance):
        instance.is_active = False
        instance.save(update_fields=['is_active', 'updated_at'])


@api_view(['GET'])
@permission_classes([IsValidated])
def loyalty_card_barcode_view(request, card_id):
    """
    Retourne les informations nécessaires pour afficher le code barre / QR d'une carte.

    Autorisé si:
    - l'utilisateur est propriétaire de la carte, OU
    - l'utilisateur est membre d'au moins une liste à laquelle la carte est associée.
    """
    try:
        card = LoyaltyCard.objects.get(pk=card_id, is_active=True)
    except LoyaltyCard.DoesNotExist:
        return Response({'detail': 'Carte introuvable.'}, status=status.HTTP_404_NOT_FOUND)

    user = request.user
    is_owner = card.owner_id == user.id

    has_list_access = False
    if not is_owner:
        list_ids = list(
            ShoppingListLoyaltyCard.objects.filter(card=card).values_list('shopping_list_id', flat=True)
        )
        if list_ids:
            has_list_access = ShoppingListMember.objects.filter(
                shopping_list_id__in=list_ids,
                user=user,
            ).exists()

    if not (is_owner or has_list_access):
        return Response(
            {'detail': 'Vous ne pouvez pas accéder à cette carte.'},
            status=status.HTTP_403_FORBIDDEN,
        )

    from .services.loyalty_cards_crypto import decrypt_card_number

    try:
        number = decrypt_card_number(card.encrypted_number)
    except Exception:
        # Ne pas exposer de détails internes, ni le numéro de carte.
        return Response(
            {'detail': 'Impossible de lire cette carte pour le moment.'},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )

    return Response(
        {
            'id': card.id,
            'name': card.name,
            'emoji': card.emoji,
            'barcode_type': card.barcode_type or 'code128',
            'barcode_value': number,
            'number_last4': card.number_last4,
            'is_owner': is_owner,
        },
        status=status.HTTP_200_OK,
    )


@api_view(['GET'])
@permission_classes([IsValidated])
def feed_user_suggestions_view(request):
    """Suggestions de profils pour le feed (complices en commun + récence + aléatoire)."""
    rows = build_feed_user_suggestions(request.user)
    serializer = FeedUserSuggestionSerializer(rows, many=True, context={'request': request})
    return Response(serializer.data, status=status.HTTP_200_OK)

