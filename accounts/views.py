from rest_framework import status, generics
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework_simplejwt.tokens import RefreshToken
from django.contrib.auth import get_user_model
from django.db.models import Q, F
from django.contrib.postgres.search import SearchVector, SearchQuery, SearchRank, TrigramSimilarity
from django.conf import settings
import uuid
from .serializers import UserRegistrationSerializer, UserSerializer, LoginSerializer, NotificationSerializer
from .models import Follow, Notification
from recipes.models import Recipe
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
    
    # Recherche stricte pour les utilisateurs (username ou email exact)
    users = User.objects.filter(
        Q(username__iexact=query) | Q(email__iexact=query)
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
            Notification.objects.create(
                user=target_user,
                notification_type='follow',
                title='Nouvel ami',
                message=f'{request.user.username} vous a ajouté comme ami',
                related_user=request.user
            )
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

