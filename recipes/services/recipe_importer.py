"""
Service pour importer des recettes depuis des URLs externes
Supporte : Bergamot, Marmiton, Elle & Cuisine, etc.
"""
import logging
import re
from typing import Dict, Optional, Tuple
from urllib.parse import urlparse
import requests
from bs4 import BeautifulSoup
from decouple import config

logger = logging.getLogger(__name__)

# Timeout pour les requêtes HTTP
REQUEST_TIMEOUT = 10


def detect_source_type(url: str) -> Optional[str]:
    """Détecte le type de source depuis l'URL"""
    parsed = urlparse(url)
    domain = parsed.netloc.lower()
    
    if 'bergamot.app' in domain or 'dashboard.bergamot.app' in domain:
        return 'bergamot'
    elif 'marmiton.org' in domain:
        return 'marmiton'
    elif 'elle.fr' in domain and 'cuisine' in domain:
        return 'elle_cuisine'
    elif '750g.com' in domain:
        return '750g'
    elif 'cuisineaz.com' in domain:
        return 'cuisineaz'
    elif 'chefclub.tv' in domain:
        return 'chefclub'
    elif 'jow.fr' in domain:
        return 'jow'
    
    return None


def extract_bergamot_recipe(url: str) -> Optional[Dict]:
    """Extrait une recette depuis Bergamot"""
    try:
        logger.info("[BergamotExtractor] Transforme url to api call : %s", url)
        recipe_id = url.split('/')[-1]    # Pour Bergamot, on peut essayer d'accéder à l'API ou parser le HTML
        # Si c'est un lien partagé, il faut peut-être une clé API
        response = requests.get(f"https://api.bergamot.app/recipes/shared?r={recipe_id}", timeout=REQUEST_TIMEOUT, headers={
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        })
        response.raise_for_status()
       
        data = response.json()
        logger.info("[BergamotExtractor] API response received")
        
        # Extraire les ingrédients : conversion simple en texte
        ingredients_list = data.get('ingredients', [])
        ingredients_lines = []
        for ing_group in ingredients_list:
            if isinstance(ing_group, dict):
                for ing in ing_group.get('data', []):
                    ingredients_lines.append(ing)
            elif isinstance(ing_group, list):
                ingredients_lines.extend(ing_group)
        
        ingredients_text = '\n'.join(ingredients_lines)
       
        # Extraire les instructions : conversion simple en texte
        instructions_list = data.get('instructions', [])
        instructions_lines = []
        for step_group in instructions_list:
            if isinstance(step_group, dict):
                for step in step_group.get('data', []):
                    instructions_lines.append(step)
            elif isinstance(step_group, list):
                instructions_lines.extend(step_group)
        
        instructions_text = '\n'.join(instructions_lines)
        
        # Extraire l'image
        photos = data.get('photos', [])
        image_path = photos[0].get('photoUrl', '') if photos else ''
        
        return {
            'title': data.get('title', ''),
            'description': data.get('description', ''),
            'ingredients_text': ingredients_text,
            'instructions_text': instructions_text,
            'prep_time': data.get('time', {}).get('prepTime', 0),
            'cook_time': data.get('time', {}).get('cookTime', 0),
            'servings': data.get('servings', 0),
            'image_path': image_path,
        }
    except Exception as e:
        logger.error(f"Erreur lors de l'extraction Bergamot: {e}")
        return None


def extract_marmiton_recipe(url: str) -> Optional[Dict]:
    """Extrait une recette depuis Marmiton"""
    try:
        response = requests.get(url, timeout=REQUEST_TIMEOUT, headers={
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        })
        response.raise_for_status()
        
        soup = BeautifulSoup(response.text, 'html.parser')
        
        # Essayer d'abord d'extraire depuis JSON-LD (données structurées)
        json_ld_scripts = soup.find_all('script', type='application/ld+json')
        for script in json_ld_scripts:
            try:
                import json
                data = json.loads(script.string)
                
                # Gérer les listes de données structurées
                if isinstance(data, list):
                    for item in data:
                        if item.get('@type') == 'Recipe':
                            data = item
                            break
                
                if isinstance(data, dict) and data.get('@type') == 'Recipe':
                    # Extraire les ingrédients
                    ingredients_list = data.get('recipeIngredient', [])
                    ingredients_text = '\n'.join([
                        ing if isinstance(ing, str) else ing.get('name', '')
                        for ing in ingredients_list
                    ])
                    
                    # Extraire les instructions
                    instructions_list = data.get('recipeInstructions', [])
                    instructions_text = '\n'.join([
                        step.get('text', '') if isinstance(step, dict) else str(step)
                        for step in instructions_list
                    ])
                    
                    # Extraire les temps (format ISO 8601)
                    prep_time_str = data.get('prepTime', '')
                    cook_time_str = data.get('cookTime', '')
                    
                    # Extraire l'image
                    image = data.get('image', '')
                    if isinstance(image, dict):
                        image = image.get('url', '')
                    elif isinstance(image, list) and image:
                        image = image[0] if isinstance(image[0], str) else image[0].get('url', '')
                    
                    return {
                        'title': data.get('name', ''),
                        'description': data.get('description', ''),
                        'ingredients_text': ingredients_text,
                        'instructions_text': instructions_text,
                        'prep_time': prep_time_str,
                        'cook_time': cook_time_str,
                        'servings': data.get('recipeYield'),
                        'image_path': image,
                    }
            except (json.JSONDecodeError, KeyError, AttributeError) as e:
                logger.debug(f"Erreur lors du parsing JSON-LD Marmiton: {e}")
                continue
        
        # Fallback : extraction HTML classique
        title_elem = soup.find('h1', class_=re.compile('recipe-title|title'))
        if not title_elem:
            title_elem = soup.find('h1')
        title = title_elem.get_text(strip=True) if title_elem else ''
        
        # Description
        desc_elem = soup.find('div', class_=re.compile('recipe-description|description'))
        description = desc_elem.get_text(strip=True) if desc_elem else ''
        
        # Ingrédients
        ingredients = []
        ingredients_section = soup.find('div', class_=re.compile('recipe-ingredients|ingredients'))
        if ingredients_section:
            for li in ingredients_section.find_all('li'):
                text = li.get_text(strip=True)
                if text:
                    ingredients.append(text)
        
        # Instructions
        instructions = []
        instructions_section = soup.find('div', class_=re.compile('recipe-steps|steps|instructions'))
        if instructions_section:
            for step in instructions_section.find_all(['li', 'p', 'div'], class_=re.compile('step|instruction')):
                text = step.get_text(strip=True)
                if text and len(text) > 10:  # Filtrer les textes trop courts
                    instructions.append(text)
        
        # Temps et portions
        prep_time = None
        cook_time = None
        servings = None
        
        time_elem = soup.find(text=re.compile(r'Préparation|Temps de préparation'))
        if time_elem:
            parent = time_elem.find_parent()
            if parent:
                time_text = parent.get_text()
                match = re.search(r'(\d+)\s*min', time_text)
                if match:
                    prep_time = int(match.group(1))
        
        cook_elem = soup.find(text=re.compile(r'Cuisson|Temps de cuisson'))
        if cook_elem:
            parent = cook_elem.find_parent()
            if parent:
                cook_text = parent.get_text()
                match = re.search(r'(\d+)\s*min', cook_text)
                if match:
                    cook_time = int(match.group(1))
        
        servings_elem = soup.find(text=re.compile(r'Personnes|Portions'))
        if servings_elem:
            parent = servings_elem.find_parent()
            if parent:
                servings_text = parent.get_text()
                match = re.search(r'(\d+)', servings_text)
                if match:
                    servings = int(match.group(1))
        
        if title and (ingredients or instructions):
            return {
                'title': title,
                'description': description,
                'ingredients_text': '\n'.join([f"- {ing}" for ing in ingredients]),
                'instructions_text': '\n'.join([f"{i+1}. {inst}" for i, inst in enumerate(instructions)]),
                'prep_time': prep_time,
                'cook_time': cook_time,
                'servings': servings,
            }
        
        return None
        
    except Exception as e:
        logger.error(f"Erreur lors de l'extraction Marmiton: {e}")
        return None


def extract_generic_recipe(url: str) -> Optional[Dict]:
    """Extraction générique pour les sites non supportés spécifiquement"""
    try:
        response = requests.get(url, timeout=REQUEST_TIMEOUT, headers={
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        })
        response.raise_for_status()
        
        soup = BeautifulSoup(response.text, 'html.parser')
        
        # Titre
        title_elem = soup.find('h1') or soup.find('title')
        title = title_elem.get_text(strip=True) if title_elem else ''
        
        # Chercher les données structurées (JSON-LD, microdata)
        json_ld = soup.find('script', type='application/ld+json')
        if json_ld:
            try:
                import json
                data = json.loads(json_ld.string)
                if isinstance(data, dict) and data.get('@type') == 'Recipe':
                    return {
                        'title': data.get('name', title),
                        'description': data.get('description', ''),
                        'ingredients_text': '\n'.join([
                            f"- {ing}" if isinstance(ing, str) else f"- {ing.get('name', '')}"
                            for ing in data.get('recipeIngredient', [])
                        ]),
                        'instructions_text': '\n'.join([
                            f"{i+1}. {step.get('text', step) if isinstance(step, dict) else step}"
                            for i, step in enumerate(data.get('recipeInstructions', []))
                        ]),
                        'prep_time': data.get('prepTime', '').replace('PT', '').replace('M', '') if data.get('prepTime') else None,
                        'cook_time': data.get('cookTime', '').replace('PT', '').replace('M', '') if data.get('cookTime') else None,
                        'servings': data.get('recipeYield'),
                    }
            except (json.JSONDecodeError, KeyError):
                pass
        
        # Fallback : extraction basique
        ingredients = []
        instructions = []
        
        for elem in soup.find_all(['li', 'p']):
            text = elem.get_text(strip=True)
            if not text or len(text) < 5:
                continue
            
            # Détecter les ingrédients
            if re.search(r'\d+\s*(g|kg|ml|l|cl|tasse|cuillère|pincée|gousse)', text, re.IGNORECASE):
                ingredients.append(text)
            # Détecter les instructions
            elif re.search(r'^(faire|mélanger|ajouter|cuire|chauffer|découper|verser|mettre)', text, re.IGNORECASE):
                instructions.append(text)
        
        if title and (ingredients or instructions):
            return {
                'title': title,
                'ingredients_text': '\n'.join([f"- {ing}" for ing in ingredients[:20]]),  # Limiter à 20
                'instructions_text': '\n'.join([f"{i+1}. {inst}" for i, inst in enumerate(instructions[:15])]),  # Limiter à 15
            }
        
        return None
        
    except Exception as e:
        logger.error(f"Erreur lors de l'extraction générique: {e}")
        return None


def import_recipe_from_url(url: str) -> Tuple[Optional[Dict], Optional[str]]:
    """
    Importe une recette depuis une URL
    
    Returns:
        Tuple[Optional[Dict], Optional[str]]: (données de la recette, type de source)
    """
    if not url or not url.startswith(('http://', 'https://')):
        return None, None
    
    source_type = detect_source_type(url)
    logger.info(f"Import de recette depuis {url} (source: {source_type})")
    
    recipe_data = None
    
    if source_type == 'bergamot':
        recipe_data = extract_bergamot_recipe(url)
    elif source_type == 'marmiton':
        recipe_data = extract_marmiton_recipe(url)
    else:
        # Essayer l'extraction générique
        recipe_data = extract_generic_recipe(url)
    
    if recipe_data:
        recipe_data['import_source_url'] = url
        return recipe_data, source_type or 'generic'
    
    return None, source_type

