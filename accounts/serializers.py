from rest_framework import serializers
from django.contrib.auth import authenticate
from .models import User, Follow, Notification, LoyaltyCard


class UserRegistrationSerializer(serializers.ModelSerializer):
    password = serializers.CharField(write_only=True, min_length=8)
    
    class Meta:
        model = User
        fields = ('id', 'username', 'email', 'password', 'level', 'experience_points')
        extra_kwargs = {
            'password': {'write_only': True},
        }
    
    def create(self, validated_data):
        password = validated_data.pop('password')
        user = User.objects.create_user(password=password, **validated_data)
        return user


class UserSerializer(serializers.ModelSerializer):
    followers_count = serializers.IntegerField(read_only=True)
    following_count = serializers.IntegerField(read_only=True)
    is_following = serializers.SerializerMethodField()
    is_followed_by = serializers.SerializerMethodField()
    avatar_url = serializers.SerializerMethodField()
    
    class Meta:
        model = User
        fields = (
            'id', 'username', 'email', 'avatar_url', 'level', 
            'experience_points', 'created_at', 'followers_count', 
            'following_count', 'is_following', 'is_followed_by'
        )
        read_only_fields = ('id', 'created_at', 'level', 'experience_points', 'followers_count', 'following_count')
    
    def get_avatar_url(self, obj):
        """Retourner l'URL de l'avatar avec presigned URL si disponible"""
        if not obj.avatar_url:
            return None
        
        # Si l'URL contient un chemin S3 (avatars/...), générer une presigned URL
        # Sinon, retourner l'URL telle quelle (peut être une URL externe)
        try:
            from savr_back.settings import build_presigned_get_url
            import re
            
            # Extraire le chemin depuis l'URL S3
            # Formats possibles:
            # - http://host/bucket/avatars/2/file.jpg
            # - https://bucket.s3.region.amazonaws.com/avatars/2/file.jpg
            # - http://192.168.1.51:9000/savr/avatars/2/file.jpg
            
            if 'avatars/' in obj.avatar_url:
                # Chercher le pattern /bucket/avatars/... ou /avatars/...
                # On cherche après le dernier / qui précède "avatars"
                match = re.search(r'/(?:[^/]+/)?(avatars/.+)$', obj.avatar_url)
                if match:
                    image_path = match.group(1)
                    presigned_url = build_presigned_get_url(image_path)
                    if presigned_url:
                        return presigned_url
                
                # Si la regex ne fonctionne pas, essayer de trouver directement "avatars/"
                idx = obj.avatar_url.find('avatars/')
                if idx != -1:
                    image_path = obj.avatar_url[idx:]
                    presigned_url = build_presigned_get_url(image_path)
                    if presigned_url:
                        return presigned_url
            
            # Si c'est une URL externe (pas S3, pas d'avatars), retourner telle quelle
            if obj.avatar_url.startswith('http') and 'avatars/' not in obj.avatar_url:
                return obj.avatar_url
            
            # Par défaut, essayer de générer une presigned URL avec l'URL complète
            # (peut fonctionner si c'est déjà un chemin relatif)
            return build_presigned_get_url(obj.avatar_url) if obj.avatar_url else None
        except Exception as e:
            # En cas d'erreur, retourner l'URL originale
            import traceback
            print(f"Error generating presigned URL for avatar: {e}")
            print(traceback.format_exc())
            return obj.avatar_url
    
    def get_is_following(self, obj):
        """Vérifier si l'utilisateur connecté suit cet utilisateur"""
        request = self.context.get('request')
        if request and request.user.is_authenticated:
            return Follow.objects.filter(follower=request.user, following=obj).exists()
        return False
    
    def get_is_followed_by(self, obj):
        """Vérifier si cet utilisateur suit l'utilisateur connecté"""
        request = self.context.get('request')
        if request and request.user.is_authenticated:
            return Follow.objects.filter(follower=obj, following=request.user).exists()
        return False


class LoginSerializer(serializers.Serializer):
    email = serializers.EmailField()
    password = serializers.CharField(write_only=True)
    
    def validate(self, attrs):
        email = attrs.get('email')
        password = attrs.get('password')
        
        if email and password:
            user = authenticate(request=self.context.get('request'), username=email, password=password)
            if not user:
                raise serializers.ValidationError('Identifiants invalides.')
            if not user.is_active:
                raise serializers.ValidationError('Ce compte est désactivé.')
            attrs['user'] = user
        else:
            raise serializers.ValidationError('Email et mot de passe requis.')
        
        return attrs


class FeedUserSuggestionSerializer(serializers.Serializer):
    """Profils suggérés pour le feed (hors réseau complice actuel)."""

    id = serializers.IntegerField(source='user.id')
    username = serializers.CharField(source='user.username')
    avatar_url = serializers.SerializerMethodField()
    mutual_complices_count = serializers.IntegerField()
    mutual_preview = serializers.SerializerMethodField()

    def _avatar_url(self, user):
        return UserSerializer(user, context=self.context).data.get('avatar_url')

    def get_avatar_url(self, obj):
        return self._avatar_url(obj['user'])

    def get_mutual_preview(self, obj):
        return [
            {
                'id': u.id,
                'username': u.username,
                'avatar_url': self._avatar_url(u),
            }
            for u in (obj.get('mutual_preview') or [])
        ]


class NotificationSerializer(serializers.ModelSerializer):
    related_user = UserSerializer(read_only=True)
    notification_type_display = serializers.CharField(source='get_notification_type_display', read_only=True)
    
    class Meta:
        model = Notification
        fields = [
            'id', 'notification_type', 'notification_type_display',
            'title', 'message', 'related_user', 'related_post_id', 'is_read', 'created_at'
        ]
        read_only_fields = ['id', 'created_at']


class LoyaltyCardSerializer(serializers.ModelSerializer):
    """
    Serializer pour les cartes de fidélité.

    - Le numéro complet est fourni via le champ write-only `number`
      et n'est jamais renvoyé en clair.
    - On expose uniquement les métadonnées, les 4 derniers chiffres
      et les listes où la carte est partagée.
    """

    number = serializers.CharField(write_only=True, required=False, allow_blank=True)
    number_last4 = serializers.CharField(read_only=True)
    is_owner = serializers.SerializerMethodField()
    owner_name = serializers.SerializerMethodField()
    lists = serializers.SerializerMethodField()

    class Meta:
        model = LoyaltyCard
        fields = [
            'id',
            'name',
            'emoji',
            'barcode_type',
            'number',
            'number_last4',
            'is_active',
            'created_at',
            'updated_at',
            'is_owner',
            'owner_name',
            'lists',
        ]
        read_only_fields = [
            'id',
            'created_at',
            'updated_at',
            'number_last4',
            'is_owner',
            'owner_name',
            'lists',
            'is_active',
        ]

    def get_is_owner(self, obj):
        request = self.context.get('request')
        user = getattr(request, 'user', None)
        return bool(user and user.is_authenticated and obj.owner_id == user.id)

    def get_owner_name(self, obj):
        owner = getattr(obj, 'owner', None)
        if not owner:
            return None
        return owner.username or owner.email or None

    def get_lists(self, obj):
        """
        Retourne les listes où la carte est partagée, sous forme légère:
        [{id, name, is_owner}] pour l'utilisateur courant.
        """
        from recipes.models import ShoppingListLoyaltyCard, ShoppingListMember

        request = self.context.get('request')
        user = getattr(request, 'user', None)
        current_user_id = getattr(user, 'id', None)

        links = (
            ShoppingListLoyaltyCard.objects
            .filter(card=obj)
            .select_related('shopping_list')
        )

        results = []
        for link in links:
            sl = link.shopping_list
            if not sl:
                continue
            is_owner = False
            if current_user_id:
                is_owner = ShoppingListMember.objects.filter(
                    shopping_list=sl,
                    user_id=current_user_id,
                    role='owner',
                ).exists()
            results.append(
                {
                    'id': sl.id,
                    'name': sl.name,
                    'is_owner': is_owner,
                }
            )
        return results

    def create(self, validated_data):
        from .services.loyalty_cards_crypto import encrypt_card_number

        request = self.context.get('request')
        user = getattr(request, 'user', None)
        if not user or not user.is_authenticated:
            raise serializers.ValidationError("Authentication required")

        number = validated_data.pop('number', None)
        if not number:
            raise serializers.ValidationError({'number': 'Ce champ est obligatoire.'})

        ciphertext, last4 = encrypt_card_number(number)
        card = LoyaltyCard.objects.create(
            owner=user,
            encrypted_number=ciphertext,
            number_last4=last4,
            **validated_data,
        )
        return card

    def update(self, instance, validated_data):
        """
        Permet de renommer la carte, changer l'emoji, le type de code barre
        et, facultativement, de mettre à jour le numéro (avec rechiffrement).
        """
        from .services.loyalty_cards_crypto import encrypt_card_number

        number = validated_data.pop('number', None)

        update_fields = []

        if number is not None and number != '':
          ciphertext, last4 = encrypt_card_number(number)
          instance.encrypted_number = ciphertext
          instance.number_last4 = last4
          update_fields.extend(['encrypted_number', 'number_last4'])

        for field in ['name', 'emoji', 'barcode_type']:
            if field in validated_data:
                setattr(instance, field, validated_data[field])
                update_fields.append(field)

        if update_fields:
            update_fields.append('updated_at')
            instance.save(update_fields=update_fields)
        else:
            instance.save(update_fields=['updated_at'])

        return instance

