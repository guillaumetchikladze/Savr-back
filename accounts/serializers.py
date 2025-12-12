from rest_framework import serializers
from django.contrib.auth import authenticate
from .models import User, Follow, Notification


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


class NotificationSerializer(serializers.ModelSerializer):
    related_user = UserSerializer(read_only=True)
    notification_type_display = serializers.CharField(source='get_notification_type_display', read_only=True)
    
    class Meta:
        model = Notification
        fields = [
            'id', 'notification_type', 'notification_type_display',
            'title', 'message', 'related_user', 'is_read', 'created_at'
        ]
        read_only_fields = ['id', 'created_at']

