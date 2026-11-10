from django.urls import include, path
from rest_framework.routers import APIRootView, DefaultRouter

from accounts.permissions import IsValidated as IsAuthenticated

from .basket_views import BasketViewSet
from .battle_plan_views import battle_plan_view
from .carnet_views import carnet_me_view, carnet_user_view
from .density_views import feed_suggestions_view, onboarding_social_view
from .dietary_views import (
    dietary_conflict_dismiss_view,
    dietary_conflicts_view,
    dietary_exception_delete_view,
    dietary_exceptions_list_view,
)
from .dish_views import dish_classics_view, dish_detail_view, dish_label_view, dish_recipe_view
from .equipment_views import equipment_catalog_view, equipment_view, recipe_adapt_equipment_view
from .ingredient_info_views import ingredient_info_view, shopping_list_item_info_view
from .pending_views import pending_confirmation_dismiss_view, pending_confirmations_view
from .preference_views import (
    preference_mapping_fork,
    preference_mapping_propose,
    preference_mapping_resolved,
    preference_mapping_validate,
    preference_mappings_list,
)
from .sub_recipe_views import (
    recipe_batch_sub_recipe_modes_view,
    recipe_ingredient_sub_recipe_suggestions_view,
    recipe_ingredient_sub_recipe_view,
)
from .views import (
    CategoryViewSet,
    CollectionViewSet,
    CookingProgressViewSet,
    IngredientViewSet,
    MealInvitationViewSet,
    MealPlanViewSet,
    PostViewSet,
    RecipeBatchViewSet,
    RecipeViewSet,
    ShoppingListInvitationViewSet,
    ShoppingListItemViewSet,
    ShoppingListViewSet,
    TimerViewSet,
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
router.register(r'basket', BasketViewSet, basename='basket')

urlpatterns = [
    path('onboarding/social/', onboarding_social_view, name='onboarding_social'),
    path('feed/suggestions/', feed_suggestions_view, name='feed_suggestions'),
    path('cooked/pending-confirmations/', pending_confirmations_view, name='cooked_pending_confirmations'),
    path(
        'cooked/pending-confirmations/<str:kind>/<int:object_id>/dismiss/',
        pending_confirmation_dismiss_view,
        name='cooked_pending_confirmation_dismiss',
    ),
    path('battle-plan/', battle_plan_view, name='battle_plan'),
    path('equipment/', equipment_view, name='equipment'),
    path('equipment/catalog/', equipment_catalog_view, name='equipment_catalog'),
    path('recipes/<int:recipe_id>/adapt-equipment/', recipe_adapt_equipment_view, name='recipe_adapt_equipment'),
    path('ingredients/<int:ingredient_id>/info/', ingredient_info_view, name='ingredient_info'),
    path('shopping-list-items/<int:item_id>/info/', shopping_list_item_info_view, name='shopping_list_item_info'),
    path(
        'recipe-ingredients/<int:line_id>/sub-recipe/',
        recipe_ingredient_sub_recipe_view,
        name='recipe_ingredient_sub_recipe',
    ),
    path(
        'recipe-ingredients/<int:line_id>/sub-recipe-suggestions/',
        recipe_ingredient_sub_recipe_suggestions_view,
        name='recipe_ingredient_sub_recipe_suggestions',
    ),
    path(
        'recipe-batches/<int:batch_id>/sub-recipe-modes/',
        recipe_batch_sub_recipe_modes_view,
        name='recipe_batch_sub_recipe_modes',
    ),
    path('carnet/me/', carnet_me_view, name='carnet_me'),
    path('carnet/users/<int:user_id>/', carnet_user_view, name='carnet_user'),
    path('dishes/classics/', dish_classics_view, name='dish_classics'),
    path('dishes/recipe/<int:recipe_id>/', dish_recipe_view, name='dish_recipe'),
    path('dishes/label/<str:slug>/', dish_label_view, name='dish_label'),
    path('dishes/<str:dish_key>/', dish_detail_view, name='dish_detail'),
    path('dietary-conflicts/', dietary_conflicts_view, name='dietary_conflicts'),
    path('dietary-conflicts/dismiss/', dietary_conflict_dismiss_view, name='dietary_conflict_dismiss'),
    path('dietary-conflicts/exceptions/', dietary_exceptions_list_view, name='dietary_exceptions_list'),
    path(
        'dietary-conflicts/exceptions/<int:pk>/',
        dietary_exception_delete_view,
        name='dietary_exception_delete',
    ),
    path('preference-mappings/', preference_mappings_list, name='preference_mappings_list'),
    path('preference-mappings/resolved/', preference_mapping_resolved, name='preference_mapping_resolved'),
    path('preference-mappings/propose/', preference_mapping_propose, name='preference_mapping_propose'),
    path('preference-mappings/fork/', preference_mapping_fork, name='preference_mapping_fork'),
    path('preference-mappings/validate/', preference_mapping_validate, name='preference_mapping_validate'),
    path('', include(router.urls)),
]
