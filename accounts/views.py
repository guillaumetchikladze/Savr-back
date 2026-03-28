from rest_framework import status, generics
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework_simplejwt.tokens import RefreshToken
from django.contrib.auth import get_user_model
from django.db.models import Q, F
from django.db.models.functions import Greatest
from django.contrib.postgres.search import SearchVector, SearchQuery, SearchRank, TrigramSimilarity
from django.conf import settings
import uuid
from .serializers import (
    UserRegistrationSerializer,
    UserSerializer,
    LoginSerializer,
    NotificationSerializer,
    LoyaltyCardSerializer,
    FeedUserSuggestionSerializer,
)
from .feed_suggestions import build_feed_user_suggestions
from .serializers_push import PushDeviceRegisterSerializer
from .models import Follow, Notification, PushDevice, LoyaltyCard
from .services.expo_push import send_expo_push_notifications
import random
from recipes.models import Recipe, ShoppingListLoyaltyCard, ShoppingListMember
from recipes.serializers import RecipeSerializer
from savr_back.settings import build_s3_client, build_presigned_get_url, build_s3_url

User = get_user_model()


@api_view(['POST'])
@permission_classes([AllowAny])
def register_view(request):
    """Register a new user"""
    serializer = UserRegistrationSerializer(data=request.data)
    if serializer.is_valid():
        user = serializer.save()
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
@permission_classes([IsAuthenticated])
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
@permission_classes([IsAuthenticated])
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
@permission_classes([IsAuthenticated])
def user_detail_view(request, user_id):
    """Récupérer les informations d'un utilisateur spécifique avec les statuts de suivi"""
    try:
        target_user = User.objects.get(id=user_id)
    except User.DoesNotExist:
        return Response({'error': 'Utilisateur non trouvé'}, status=status.HTTP_404_NOT_FOUND)
    
    serializer = UserSerializer(target_user, context={'request': request})
    return Response(serializer.data, status=status.HTTP_200_OK)


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def search_view(request):
    """Recherche intelligente d'utilisateurs et de recettes"""
    query = request.query_params.get('q', '').strip()
    user_id = request.query_params.get('id')
    
    # Si un ID est fourni, retourner directement l'utilisateur
    if user_id:
        try:
            user = User.objects.get(id=int(user_id))
            serializer = UserSerializer(user, context={'request': request})
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
        users = User.objects.exclude(id=request.user.id).order_by('-created_at')[:10]
        recipes = Recipe.objects.select_related('created_by').prefetch_related(
            'steps', 'recipe_ingredients__ingredient'
        ).order_by('-created_at')[:10]
        
        users_serializer = UserSerializer(users, many=True, context={'request': request})
        recipes_serializer = RecipeSerializer(recipes, many=True)
        
        return Response({
            'users': users_serializer.data,
            'recipes': recipes_serializer.data,
        }, status=status.HTTP_200_OK)
    
    # Recherche fuzzy pour les utilisateurs avec trigram similarity
    try:
        users = User.objects.exclude(id=request.user.id).annotate(
            username_similarity=TrigramSimilarity('username', query),
            email_similarity=TrigramSimilarity('email', query),
        ).annotate(
            max_similarity=Greatest('username_similarity', 'email_similarity')
        ).filter(
            Q(username__icontains=query) | 
            Q(email__icontains=query) | 
            Q(max_similarity__gt=0.2)
        ).order_by('-max_similarity')
    except Exception:
        # Fallback sans trigram similarity
        users = User.objects.filter(
            Q(username__icontains=query) | Q(email__icontains=query)
        ).exclude(id=request.user.id)
    
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
    
    users_serializer = UserSerializer(users, many=True, context={'request': request})
    recipes_serializer = RecipeSerializer(recipes, many=True)
    
    return Response({
        'users': users_serializer.data,
        'recipes': recipes_serializer.data,
    }, status=status.HTTP_200_OK)


@api_view(['POST', 'DELETE'])
@permission_classes([IsAuthenticated])
def follow_user_view(request, user_id):
    """Devenir ami (follow) ou ne plus être ami (unfollow) d'un utilisateur"""
    try:
        target_user = User.objects.get(id=user_id)
    except User.DoesNotExist:
        return Response({'error': 'Utilisateur non trouvé'}, status=status.HTTP_404_NOT_FOUND)
    
    if target_user.id == request.user.id:
        return Response({'error': 'Vous ne pouvez pas vous suivre vous-même'}, status=status.HTTP_400_BAD_REQUEST)
    
    if request.method == 'POST':
        # Devenir complice
        follow, created = Follow.objects.get_or_create(
            follower=request.user,
            following=target_user
        )
        if created:
            # Créer une notification pour l'utilisateur suivi
            follow_titles = [
                "Un nouveau complice arrive",
                "Tu as gagné un nouvel allié",
            ]
            follow_messages = [
                f"{request.user.username} t'a ajouté comme complice.",
                f"{request.user.username} a commencé à te suivre.",
            ]

            notification = Notification.objects.create(
                user=target_user,
                notification_type='follow',
                title=random.choice(follow_titles),
                message=random.choice(follow_messages),
                related_user=request.user
            )
            # Envoyer une push Expo aux devices du nouvel ami
            devices = PushDevice.objects.filter(
                user=target_user,
                is_active=True
            ).exclude(expo_push_token='')
            messages = []
            for device in devices:
                messages.append(
                    {
                        'to': device.expo_push_token,
                        'title': notification.title,
                        'body': notification.message,
                        'data': {
                            'source': 'social',
                            'kind': 'follow',
                            'notification_id': notification.id,
                            'user_id': request.user.id,
                        },
                        'sound': 'default',
                    }
                )
            if messages:
                send_expo_push_notifications(messages)
            return Response({'message': 'Vous êtes maintenant ami'}, status=status.HTTP_201_CREATED)
        else:
            return Response({'message': 'Vous êtes déjà ami'}, status=status.HTTP_200_OK)
    
    elif request.method == 'DELETE':
        # Ne plus être ami
        try:
            follow = Follow.objects.get(follower=request.user, following=target_user)
            follow.delete()
            return Response({'message': 'Vous n\'êtes plus ami'}, status=status.HTTP_200_OK)
        except Follow.DoesNotExist:
            return Response({'message': 'Vous n\'êtes pas ami'}, status=status.HTTP_200_OK)


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def notifications_view(request):
    """Récupérer toutes les notifications de l'utilisateur"""
    notifications = Notification.objects.filter(user=request.user).select_related('related_user')
    serializer = NotificationSerializer(notifications, many=True, context={'request': request})
    return Response(serializer.data, status=status.HTTP_200_OK)


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def unread_notifications_count_view(request):
    """Récupérer le nombre de notifications non lues"""
    count = Notification.objects.filter(user=request.user, is_read=False).count()
    return Response({'count': count}, status=status.HTTP_200_OK)


@api_view(['POST'])
@permission_classes([IsAuthenticated])
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
@permission_classes([IsAuthenticated])
def mark_all_notifications_read_view(request):
    """Marquer toutes les notifications comme lues"""
    Notification.objects.filter(user=request.user, is_read=False).update(is_read=True)
    return Response({'message': 'Toutes les notifications ont été marquées comme lues'}, status=status.HTTP_200_OK)


@api_view(['GET'])
@permission_classes([IsAuthenticated])
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
@permission_classes([IsAuthenticated])
def followers_list_view(request):
    """Récupérer uniquement les abonnés de l'utilisateur (followers)"""
    followers_qs = Follow.objects.filter(following=request.user).select_related('follower')
    followers = [follow.follower for follow in followers_qs]
    serializer = UserSerializer(followers, many=True, context={'request': request})
    return Response(serializer.data, status=status.HTTP_200_OK)


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def following_list_view(request):
    """Récupérer uniquement les abonnements de l'utilisateur (following)"""
    following_qs = Follow.objects.filter(follower=request.user).select_related('following')
    following = [follow.following for follow in following_qs]
    serializer = UserSerializer(following, many=True, context={'request': request})
    return Response(serializer.data, status=status.HTTP_200_OK)


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def user_followers_view(request, user_id):
    """Récupérer les abonnés d'un utilisateur spécifique"""
    try:
        target_user = User.objects.get(id=user_id)
    except User.DoesNotExist:
        return Response({'error': 'Utilisateur non trouvé'}, status=status.HTTP_404_NOT_FOUND)
    
    followers_qs = Follow.objects.filter(following=target_user).select_related('follower')
    followers = [follow.follower for follow in followers_qs]
    serializer = UserSerializer(followers, many=True, context={'request': request})
    return Response(serializer.data, status=status.HTTP_200_OK)


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def user_following_view(request, user_id):
    """Récupérer les abonnements d'un utilisateur spécifique"""
    try:
        target_user = User.objects.get(id=user_id)
    except User.DoesNotExist:
        return Response({'error': 'Utilisateur non trouvé'}, status=status.HTTP_404_NOT_FOUND)
    
    following_qs = Follow.objects.filter(follower=target_user).select_related('following')
    following = [follow.following for follow in following_qs]
    serializer = UserSerializer(following, many=True, context={'request': request})
    return Response(serializer.data, status=status.HTTP_200_OK)

@api_view(['GET'])
@permission_classes([IsAuthenticated])
def users_search_view(request):
    """Recherche d'utilisateurs pour autocomplete @mention (retourne id, username, avatar_url)"""
    from recipes.serializers import UserLightSerializer
    query = (request.query_params.get('q') or '').strip()
    if len(query) < 2:
        return Response([], status=status.HTTP_200_OK)
    users = User.objects.filter(
        Q(username__icontains=query) | Q(username__istartswith=query)
    ).exclude(id=request.user.id).order_by('username')[:15]
    serializer = UserLightSerializer(users, many=True, context={'request': request})
    return Response(serializer.data, status=status.HTTP_200_OK)


@api_view(['GET'])
@permission_classes([IsAuthenticated])
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
@permission_classes([IsAuthenticated])
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
@permission_classes([IsAuthenticated])
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


class LoyaltyCardListCreateView(generics.ListCreateAPIView):
    """
    Lister et créer les cartes de fidélité de l'utilisateur courant.
    """

    serializer_class = LoyaltyCardSerializer
    permission_classes = [IsAuthenticated]

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
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        return LoyaltyCard.objects.filter(owner=self.request.user, is_active=True)

    def perform_destroy(self, instance):
        instance.is_active = False
        instance.save(update_fields=['is_active', 'updated_at'])


@api_view(['GET'])
@permission_classes([IsAuthenticated])
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
@permission_classes([IsAuthenticated])
def feed_user_suggestions_view(request):
    """Suggestions de profils pour le feed (complices en commun + récence + aléatoire)."""
    rows = build_feed_user_suggestions(request.user)
    serializer = FeedUserSuggestionSerializer(rows, many=True, context={'request': request})
    return Response(serializer.data, status=status.HTTP_200_OK)

