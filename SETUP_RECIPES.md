# Setup Recipes API

## Installation

1. **Créer les migrations** :
```bash
cd Savr-back
source venv/bin/activate
python manage.py makemigrations
python manage.py migrate
```

2. **Créer les données d'exemple** :
```bash
python manage.py create_sample_data
```

Cela créera :
- Un utilisateur de test : `test@example.com` / `testpass123`
- 30+ ingrédients
- 5 recettes complètes avec étapes et ingrédients
- Des repas planifiés pour la semaine en cours

## Structure des modèles

### Ingredient
- `name` : Nom de l'ingrédient (unique)

### Recipe
- `title` : Titre de la recette
- `description` : Description
- `meal_type` : Type de repas (breakfast, lunch, dinner, snack)
- `difficulty` : Difficulté (easy, medium, hard)
- `prep_time` : Temps de préparation en minutes
- `cook_time` : Temps de cuisson en minutes
- `servings` : Nombre de portions
- `image_url` : URL de l'image (optionnel)
- `created_by` : Utilisateur qui a créé la recette

### RecipeIngredient (Many-to-Many)
- `recipe` : Recette
- `ingredient` : Ingrédient
- `quantity` : Quantité
- `unit` : Unité (g, kg, ml, l, tsp, tbsp, cup, piece, pinch, clove)

### Step
- `recipe` : Recette
- `order` : Ordre de l'étape
- `instruction` : Instruction de l'étape

### MealPlan
- `user` : Utilisateur
- `date` : Date du repas
- `meal_time` : Moment du repas (lunch, dinner)
- `meal_type` : Type de repas (cantine, takeaway, recipe)
- `recipe` : Recette associée (optionnel, si meal_type = 'recipe')
- `confirmed` : Repas confirmé ou non

## API Endpoints

### Recettes
- `GET /api/recipes/` - Liste toutes les recettes
- `GET /api/recipes/{id}/` - Détails d'une recette
- `POST /api/recipes/` - Créer une recette
- `PUT /api/recipes/{id}/` - Mettre à jour une recette
- `DELETE /api/recipes/{id}/` - Supprimer une recette
- `GET /api/recipes/my_recipes/` - Mes recettes

**Filtres** :
- `?meal_type=lunch` - Filtrer par type de repas
- `?difficulty=easy` - Filtrer par difficulté
- `?search=carbonara` - Rechercher dans le titre et la description

### Ingrédients
- `GET /api/ingredients/` - Liste tous les ingrédients
- `GET /api/ingredients/{id}/` - Détails d'un ingrédient
- `GET /api/ingredients/search/?q=tomate` - Rechercher des ingrédients

### Repas planifiés
- `GET /api/meal-plans/` - Liste tous mes repas planifiés
- `GET /api/meal-plans/{id}/` - Détails d'un repas planifié
- `POST /api/meal-plans/` - Créer un repas planifié
- `PUT /api/meal-plans/{id}/` - Mettre à jour un repas planifié
- `DELETE /api/meal-plans/{id}/` - Supprimer un repas planifié
- `GET /api/meal-plans/by_date/?date=2024-11-15` - Repas pour une date
- `GET /api/meal-plans/by_week/?date=2024-11-15` - Repas pour une semaine
- `POST /api/meal-plans/{id}/confirm/` - Confirmer un repas

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

## Frontend

Le frontend a été mis à jour pour :
- Charger automatiquement les repas planifiés au chargement de l'écran
- Sauvegarder automatiquement les repas planifiés quand on les modifie
- Gérer les erreurs d'authentification

Les repas sont maintenant persistés dans la base de données PostgreSQL.

