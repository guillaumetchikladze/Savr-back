from django.core.management.base import BaseCommand
from django.db import transaction

from recipes.models import MealPlan, MealPlanGroup, MealPlanGroupMember


class Command(BaseCommand):
    help = 'Créer un MealPlanGroup pour chaque MealPlan qui n\'en possède pas encore.'

    def handle(self, *args, **options):
        meal_plans_without_group = MealPlan.objects.filter(
            group_memberships__isnull=True
        ).select_related('user')

        if not meal_plans_without_group.exists():
            self.stdout.write(self.style.SUCCESS('Tous les meal plans ont déjà un groupe.'))
            return

        created_groups = 0

        for meal_plan in meal_plans_without_group:
            with transaction.atomic():
                group = MealPlanGroup.objects.create(user=meal_plan.user)
                MealPlanGroupMember.objects.create(
                    group=group,
                    meal_plan=meal_plan,
                    order=0
                )
                created_groups += 1

        self.stdout.write(
            self.style.SUCCESS(f'Création terminée : {created_groups} groupes ajoutés.')
        )

