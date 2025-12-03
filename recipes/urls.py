from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import CategoryViewSet, RecipeViewSet, IngredientViewSet, MealPlanViewSet, MealPlanGroupViewSet, MealInvitationViewSet, CookingProgressViewSet, TimerViewSet, PostViewSet, ShoppingListViewSet, ShoppingListItemViewSet, CollectionViewSet

router = DefaultRouter()
router.register(r'categories', CategoryViewSet, basename='category')
router.register(r'recipes', RecipeViewSet, basename='recipe')
router.register(r'ingredients', IngredientViewSet, basename='ingredient')
router.register(r'meal-plans', MealPlanViewSet, basename='mealplan')
router.register(r'meal-plan-groups', MealPlanGroupViewSet, basename='mealplangroup')
router.register(r'meal-invitations', MealInvitationViewSet, basename='mealinvitation')
router.register(r'cooking-progress', CookingProgressViewSet, basename='cookingprogress')
router.register(r'timers', TimerViewSet, basename='timer')
router.register(r'posts', PostViewSet, basename='post')
router.register(r'collections', CollectionViewSet, basename='collection')
router.register(r'shopping-lists', ShoppingListViewSet, basename='shoppinglist')
router.register(r'shopping-list-items', ShoppingListItemViewSet, basename='shoppinglistitem')

urlpatterns = [
    path('', include(router.urls)),
]

