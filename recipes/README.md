# API Recipes

## Endpoints disponibles

### Recettes (`/api/recipes/`)
- `GET /api/recipes/` - Liste toutes les recettes
- `GET /api/recipes/{id}/` - Détails d'une recette
- `POST /api/recipes/` - Créer une recette
- `PUT /api/recipes/{id}/` - Mettre à jour une recette
- `DELETE /api/recipes/{id}/` - Supprimer une recette
- `GET /api/recipes/my_recipes/` - Mes recettes

**Filtres de recherche:**
- `?meal_type=lunch` - Filtrer par type de repas (breakfast, lunch, dinner, snack)
- `?difficulty=easy` - Filtrer par difficulté (easy, medium, hard)
- `?search=carbonara` - Rechercher dans le titre et la description

### Ingrédients (`/api/ingredients/`)
- `GET /api/ingredients/` - Liste tous les ingrédients
- `GET /api/ingredients/{id}/` - Détails d'un ingrédient
- `GET /api/ingredients/search/?q=tomate` - Rechercher des ingrédients

### Repas planifiés (`/api/meal-plans/`)
- `GET /api/meal-plans/` - Liste tous mes repas planifiés
- `GET /api/meal-plans/{id}/` - Détails d'un repas planifié
- `POST /api/meal-plans/` - Créer un repas planifié
- `PUT /api/meal-plans/{id}/` - Mettre à jour un repas planifié
- `DELETE /api/meal-plans/{id}/` - Supprimer un repas planifié
- `GET /api/meal-plans/by_date/?date=2024-11-15` - Repas pour une date
- `GET /api/meal-plans/by_week/?date=2024-11-15` - Repas pour une semaine
- `POST /api/meal-plans/{id}/confirm/` - Confirmer un repas

## Créer des données d'exemple

```bash
python manage.py create_sample_data
```

Cela créera:
- Un utilisateur de test: `test@example.com` / `testpass123`
- 30+ ingrédients
- 5 recettes complètes avec étapes et ingrédients
- Des repas planifiés pour la semaine en cours

## Exemples de requêtes

### Créer un repas planifié
```json
POST /api/meal-plans/
{
  "date": "2024-11-15",
  "meal_time": "lunch",
  "meal_type": "recipe",
  "recipe_id": 1
}
```

### Créer une recette
```json
POST /api/recipes/
{
  "title": "Pâtes à la carbonara",
  "description": "Un classique italien",
  "meal_type": "dinner",
  "difficulty": "medium",
  "prep_time": 15,
  "cook_time": 20,
  "servings": 4,
  "steps": [
    {"order": 1, "instruction": "Faire cuire les pâtes"},
    {"order": 2, "instruction": "Préparer la sauce"}
  ],
  "ingredients": [
    {"ingredient_id": 1, "quantity": 400, "unit": "g"},
    {"ingredient_id": 2, "quantity": 200, "unit": "g"}
  ]
}
```

