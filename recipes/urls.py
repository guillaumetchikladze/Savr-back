from django.urls import path, include
from rest_framework.routers import DefaultRouter, APIRootView
from rest_framework.permissions import IsAuthenticated
from .views import CategoryViewSet, RecipeViewSet, IngredientViewSet, MealPlanViewSet, MealInvitationViewSet, CookingProgressViewSet, TimerViewSet, PostViewSet, ShoppingListViewSet, ShoppingListItemViewSet, ShoppingListInvitationViewSet, CollectionViewSet, RecipeBatchViewSet
from .dietary_views import dietary_conflicts_view
from .preference_views import (
    preference_mappings_list,
    preference_mapping_resolved,
    preference_mapping_propose,
    preference_mapping_fork,
    preference_mapping_validate,
)


class SecureAPIRootView(APIRootView):
    """API root exigeant une authentification."""
    permission_classes = [IsAuthenticated]


class SecureRouter(DefaultRouter):
    """Router dont la vue API root exige une authentification."""
    APIRootView = SecureAPIRootView

    def get_api_root_view(self, api_urls=None):
        return super().get_api_root_view(api_urls=api_urls)


router = SecureRouter()
router.register(r'categories', CategoryViewSet, basename='category')
router.register(r'recipes', RecipeViewSet, basename='recipe')
router.register(r'recipe-batches', RecipeBatchViewSet, basename='recipebatch')
router.register(r'ingredients', IngredientViewSet, basename='ingredient')
router.register(r'meal-plans', MealPlanViewSet, basename='mealplan')
router.register(r'meal-invitations', MealInvitationViewSet, basename='mealinvitation')
router.register(r'cooking-progress', CookingProgressViewSet, basename='cookingprogress')
router.register(r'timers', TimerViewSet, basename='timer')
router.register(r'posts', PostViewSet, basename='post')
router.register(r'collections', CollectionViewSet, basename='collection')
router.register(r'shopping-lists', ShoppingListViewSet, basename='shoppinglist')
router.register(r'shopping-list-items', ShoppingListItemViewSet, basename='shoppinglistitem')
router.register(r'shopping-list-invitations', ShoppingListInvitationViewSet, basename='shoppinglistinvitation')

urlpatterns = [
    path('dietary-conflicts/', dietary_conflicts_view, name='dietary_conflicts'),
    path('preference-mappings/', preference_mappings_list, name='preference_mappings_list'),
    path('preference-mappings/resolved/', preference_mapping_resolved, name='preference_mapping_resolved'),
    path('preference-mappings/propose/', preference_mapping_propose, name='preference_mapping_propose'),
    path('preference-mappings/fork/', preference_mapping_fork, name='preference_mapping_fork'),
    path('preference-mappings/validate/', preference_mapping_validate, name='preference_mapping_validate'),
    path('', include(router.urls)),
]

