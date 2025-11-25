"""
Service de prétraitement des recettes importées depuis des URLs.
Nettoie, normalise et structure les données avant l'envoi à l'IA.
"""
import logging
import re
from typing import Dict, Optional, List, Tuple

logger = logging.getLogger(__name__)


def clean_text(text: str) -> str:
    """Nettoie un texte en enlevant les caractères indésirables et en normalisant les espaces"""
    if not text:
        return ""
    
    # Enlever les caractères de contrôle et normaliser les espaces
    text = re.sub(r'\s+', ' ', text)
    text = re.sub(r'\n\s*\n', '\n', text)  # Enlever les lignes vides multiples
    text = text.strip()
    
    return text


def normalize_ingredient_line(line: str) -> str:
    """
    Nettoie et normalise une ligne d'ingrédient.
    Enlève les numéros, les puces, et normalise le formatage.
    """
    if not line:
        return ""
    
    # Enlever les numéros au début (1., 2., -1, etc.)
    line = re.sub(r'^[\d\-\•\*]\s*', '', line)
    
    # Enlever les puces communes
    line = re.sub(r'^[•\-\*]\s*', '', line)
    
    # Enlever les espaces multiples
    line = re.sub(r'\s+', ' ', line)
    
    # Capitaliser la première lettre si nécessaire
    line = line.strip()
    if line and not line[0].isupper():
        line = line[0].upper() + line[1:] if len(line) > 1 else line.upper()
    
    return line


def normalize_instruction_line(line: str) -> str:
    """
    Nettoie et normalise une ligne d'instruction.
    Enlève les numéros d'étapes et normalise le formatage.
    """
    if not line:
        return ""
    
    # Enlever les numéros d'étapes au début (1., 2., Étape 1, etc.)
    line = re.sub(r'^(étape\s*)?\d+[\.\)]\s*', '', line, flags=re.IGNORECASE)
    line = re.sub(r'^\d+[\.\)]\s*', '', line)
    
    # Enlever les puces
    line = re.sub(r'^[•\-\*]\s*', '', line)
    
    # Enlever les espaces multiples
    line = re.sub(r'\s+', ' ', line)
    
    # Capitaliser la première lettre
    line = line.strip()
    if line:
        line = line[0].upper() + line[1:] if len(line) > 1 else line.upper()
    
    return line


def parse_time_to_minutes(time_str: Optional[str]) -> Optional[int]:
    """
    Convertit une chaîne de temps en minutes.
    Supporte: "30 min", "1h30", "PT30M", "1h", etc.
    """
    if not time_str:
        return None
    
    if isinstance(time_str, int):
        return time_str
    
    time_str = str(time_str).strip().upper()
    
    # Format ISO 8601 (PT30M, PT1H30M)
    if time_str.startswith('PT'):
        time_str = time_str[2:]
        total_minutes = 0
        
        # Heures
        hour_match = re.search(r'(\d+)H', time_str)
        if hour_match:
            total_minutes += int(hour_match.group(1)) * 60
        
        # Minutes
        min_match = re.search(r'(\d+)M', time_str)
        if min_match:
            total_minutes += int(min_match.group(1))
        
        return total_minutes if total_minutes > 0 else None
    
    # Format français (1h30, 30 min, etc.)
    total_minutes = 0
    
    # Heures
    hour_match = re.search(r'(\d+)\s*h', time_str)
    if hour_match:
        total_minutes += int(hour_match.group(1)) * 60
    
    # Minutes
    min_match = re.search(r'(\d+)\s*min', time_str)
    if min_match:
        total_minutes += int(min_match.group(1))
    
    # Si pas de format trouvé, essayer de trouver juste un nombre
    if total_minutes == 0:
        num_match = re.search(r'(\d+)', time_str)
        if num_match:
            # Supposer que c'est en minutes si < 10, sinon en heures
            num = int(num_match.group(1))
            total_minutes = num if num < 10 else num * 60
    
    return total_minutes if total_minutes > 0 else None


def parse_servings(servings_str: Optional[str]) -> Optional[int]:
    """Extrait le nombre de portions depuis une chaîne"""
    if not servings_str:
        return None
    
    if isinstance(servings_str, int):
        return servings_str
    
    servings_str = str(servings_str).strip()
    
    # Chercher un nombre
    match = re.search(r'(\d+)', servings_str)
    if match:
        return int(match.group(1))
    
    return None


def detect_meal_type(title: str, description: str = "") -> Optional[str]:
    """
    Détecte le type de repas depuis le titre et la description.
    Retourne: 'breakfast', 'lunch', 'dinner', 'snack', ou None
    """
    text = (title + " " + description).lower()
    
    # Petit-déjeuner
    breakfast_keywords = ['petit-déjeuner', 'petit dejeuner', 'breakfast', 'café', 'cafe', 'croissant', 'pain', 'confiture']
    if any(kw in text for kw in breakfast_keywords):
        return 'breakfast'
    
    # En-cas
    snack_keywords = ['en-cas', 'encas', 'snack', 'goûter', 'gouter', 'collation', 'apéritif', 'aperitif']
    if any(kw in text for kw in snack_keywords):
        return 'snack'
    
    # Dîner (souvent plus élaboré)
    dinner_keywords = ['dîner', 'diner', 'dinner', 'soir', 'souper']
    if any(kw in text for kw in dinner_keywords):
        return 'dinner'
    
    # Déjeuner par défaut
    return 'lunch'


def split_long_instructions(instructions_text: str, max_length: int = 200) -> List[str]:
    """
    Découpe les instructions trop longues en plusieurs étapes.
    """
    lines = instructions_text.split('\n')
    result = []
    
    for line in lines:
        line = normalize_instruction_line(line)
        if not line:
            continue
        
        # Si la ligne est trop longue, essayer de la découper
        if len(line) > max_length:
            # Découper par phrases (point, point-virgule, etc.)
            sentences = re.split(r'[.;]\s+', line)
            current = ""
            
            for sentence in sentences:
                sentence = sentence.strip()
                if not sentence:
                    continue
                
                if len(current) + len(sentence) + 2 > max_length and current:
                    result.append(current.strip())
                    current = sentence
                else:
                    current += (". " if current else "") + sentence
            
            if current:
                result.append(current.strip())
        else:
            result.append(line)
    
    return result


def preprocess_recipe_data(raw_data: Dict) -> Dict:
    """
    Prétraite les données brutes d'une recette importée.
    
    Args:
        raw_data: Données brutes extraites depuis l'URL
    
    Returns:
        Dict: Données nettoyées et structurées prêtes pour l'IA
    """
    logger.info("[Preprocessor] Prétraitement des données pour: %s", raw_data.get('title', 'Unknown'))
    
    processed = {
        'title': clean_text(raw_data.get('title', '')),
        'description': clean_text(raw_data.get('description', '')),
        'import_source_url': raw_data.get('import_source_url', ''),
    }
    
    # Nettoyer et normaliser les ingrédients
    ingredients_text = raw_data.get('ingredients_text', '')
    if ingredients_text:
        # Gérer le cas où ingredients_text est déjà une liste (import structuré)
        if isinstance(ingredients_text, list):
            # Convertir la liste en texte
            ingredient_lines = [
                normalize_ingredient_line(str(ing))
                for ing in ingredients_text
                if str(ing).strip()
            ]
        else:
            # Cas normal : texte libre à nettoyer
            ingredient_lines = [
                normalize_ingredient_line(line)
                for line in ingredients_text.split('\n')
                if line.strip()
            ]
        # Filtrer les lignes vides et limiter à 50 ingrédients
        processed['ingredients_text'] = '\n'.join(ingredient_lines[:50])
    else:
        processed['ingredients_text'] = ''
    
    # Nettoyer et normaliser les instructions
    instructions_text = raw_data.get('instructions_text', '')
    if instructions_text:
        # Gérer le cas où instructions_text est déjà une liste (import structuré)
        if isinstance(instructions_text, list):
            # Convertir la liste en texte
            instruction_lines = [
                normalize_instruction_line(str(inst))
                for inst in instructions_text
                if str(inst).strip()
            ]
        else:
            # Cas normal : texte libre à nettoyer et découper
            instruction_lines = split_long_instructions(instructions_text)
        # Limiter à 30 étapes
        processed['instructions_text'] = '\n'.join(instruction_lines[:30])
    else:
        processed['instructions_text'] = ''
    
    # Normaliser les temps
    prep_time = raw_data.get('prep_time')
    if prep_time:
        processed['prep_time'] = parse_time_to_minutes(prep_time)
    else:
        processed['prep_time'] = None
    
    cook_time = raw_data.get('cook_time')
    if cook_time:
        processed['cook_time'] = parse_time_to_minutes(cook_time)
    else:
        processed['cook_time'] = None
    
    # Normaliser les portions
    servings = raw_data.get('servings')
    if servings:
        processed['servings'] = parse_servings(servings)
    else:
        processed['servings'] = None
    
    # Détecter le type de repas si non fourni
    # (on laisse l'IA le faire, mais on peut pré-remplir)
    
    # Image
    processed['image_path'] = raw_data.get('image_path', '')
    
    # Source type
    processed['source_type'] = 'imported'
    
    # Validation : s'assurer qu'on a au moins un titre et des ingrédients/instructions
    if not processed['title']:
        logger.warning("[Preprocessor] Titre manquant après prétraitement")
    
    if not processed['ingredients_text'] and not processed['instructions_text']:
        logger.warning("[Preprocessor] Aucun ingrédient ni instruction après prétraitement")
    
    logger.info(
        "[Preprocessor] Prétraitement terminé: %d ingrédients, %d instructions",
        len(processed['ingredients_text'].split('\n')) if processed['ingredients_text'] else 0,
        len(processed['instructions_text'].split('\n')) if processed['instructions_text'] else 0
    )
    
    return processed

