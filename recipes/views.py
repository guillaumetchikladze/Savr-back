from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from django.db.models import Q
from datetime import datetime, date
from .models import Recipe, Step, Ingredient, RecipeIngredient, MealPlan, MealInvitation
from .serializers import (
    RecipeSerializer, RecipeCreateSerializer,
    StepSerializer, IngredientSerializer,
    MealPlanSerializer, MealInvitationSerializer
)


class RecipeViewSet(viewsets.ModelViewSet):
    """ViewSet pour les recettes"""
    queryset = Recipe.objects.all()
    permission_classes = [IsAuthenticated]
    
    def get_serializer_class(self):
        if self.action == 'create':
            return RecipeCreateSerializer
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
            queryset = queryset.filter(
                Q(title__icontains=search) | Q(description__icontains=search)
            )
        
        return queryset
    
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
    
    def get_queryset(self):
        return MealPlan.objects.filter(user=self.request.user).prefetch_related('shared_with', 'recipe')
    
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
        
        meal_plans = MealPlan.objects.filter(user=request.user, date=target_date)
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
        meal_plans = MealPlan.objects.filter(shared_with=request.user).select_related('user', 'recipe').prefetch_related('shared_with')
        serializer = self.get_serializer(meal_plans, many=True)
        return Response(serializer.data)
    
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
        ).select_related('inviter', 'invitee', 'meal_plan', 'meal_plan__recipe').prefetch_related('meal_plan__shared_with')
    
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
        
        # Si le meal plan existait déjà, on ne le modifie pas (pour ne pas écraser ce que l'utilisateur a configuré)
        # Mais on ajoute l'inviteur dans les shared_with
        if not created:
            meal_plan.shared_with.add(request.user)
        else:
            # Si c'est nouveau, on ajoute l'inviteur dans les shared_with
            user_meal_plan.shared_with.add(invitation.inviter)
            # Et on ajoute l'invité dans les shared_with du meal plan original
            meal_plan.shared_with.add(request.user)
        
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
        ).select_related('inviter', 'meal_plan', 'meal_plan__recipe').prefetch_related('meal_plan__shared_with')
        serializer = self.get_serializer(invitations, many=True)
        return Response(serializer.data)
