from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import RecipeViewSet, IngredientViewSet, MealPlanViewSet, MealInvitationViewSet

router = DefaultRouter()
router.register(r'recipes', RecipeViewSet, basename='recipe')
router.register(r'ingredients', IngredientViewSet, basename='ingredient')
router.register(r'meal-plans', MealPlanViewSet, basename='mealplan')
router.register(r'meal-invitations', MealInvitationViewSet, basename='mealinvitation')

urlpatterns = [
    path('', include(router.urls)),
]

