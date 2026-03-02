"""
Service pour importer des recettes depuis des URLs externes
Supporte : Bergamot, Marmiton, Elle & Cuisine, etc.
"""
import logging
import re
import json
from typing import Dict, Optional, Tuple
from urllib.parse import urlparse
import requests
from bs4 import BeautifulSoup
from decouple import config
from recipe_scrapers import SCRAPERS, scrape_html
from apify_client import ApifyClient

logger = logging.getLogger(__name__)

# Timeout pour les requêtes HTTP
REQUEST_TIMEOUT = 10


RECIPE_SCRAPERS_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/121.0.0.0 Safari/537.36"
)


def _netloc_host(url: str) -> str:
    try:
        host = urlparse(url).netloc.lower()
    except Exception:
        return ''
    if host.startswith('www.'):
        host = host[4:]
    return host


def _is_recipe_scrapers_supported(url: str) -> bool:
    """
    `recipe-scrapers` expose SCRAPERS (mapping domain->scraper class).
    On accepte une correspondance exacte ou suffixe (sous-domaines).
    """
    host = _netloc_host(url)
    if not host:
        return False
    for domain in SCRAPERS.keys():
        d = (domain or '').lower()
        if d.startswith('www.'):
            d = d[4:]
        if host == d or host.endswith('.' + d):
            return True
    return False


def _parse_first_int(val: object) -> Optional[int]:
    if val is None:
        return None
    if isinstance(val, int):
        return val
    s = str(val).strip()
    m = re.search(r'(\d+)', s)
    return int(m.group(1)) if m else None


def _normalize_multiline_steps(text: str) -> str:
    """
    Transforme une string d'instructions potentiellement multi-lignes en format
    1. ..., 2. ... pour aider le parsing IA.
    """
    raw = (text or '').strip()
    if not raw:
        return ''
    lines = [ln.strip() for ln in re.split(r'[\r\n]+', raw) if ln.strip()]
    if len(lines) <= 1:
        return raw
    return '\n'.join([f"{i+1}. {ln}" for i, ln in enumerate(lines)])


def extract_with_recipe_scrapers(url: str) -> Optional[Dict]:
    """
    Extraction via `recipe-scrapers` (HTML parsing). On fetch nous-mêmes le HTML
    (headers, timeout) puis on appelle `scrape_html(..., wild_mode=True)` pour
    maximiser les chances de support.
    """
    if not url or not url.startswith(('http://', 'https://')):
        return None

    # Si la lib ne supporte pas le domaine, `wild_mode=True` peut encore marcher
    # (schema.org), mais c’est plus coûteux; on log uniquement.
    supported = _is_recipe_scrapers_supported(url)
    logger.info(
        "[RecipeScrapers] Starting extraction (supported=%s) for url=%s",
        supported,
        url,
    )

    try:
        response = requests.get(
            url,
            timeout=REQUEST_TIMEOUT,
            headers={
                'User-Agent': RECIPE_SCRAPERS_UA,
                'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
            },
        )
        response.raise_for_status()
        html = response.text or ''
        if not html.strip():
            return None

        scraper = scrape_html(html, url, wild_mode=True)

        # Champs essentiels
        title = (scraper.title() or '').strip()
        image = ''
        try:
            image = (scraper.image() or '').strip()
        except Exception:
            image = ''

        ingredients_list = []
        try:
            ingredients_list = scraper.ingredients() or []
        except Exception:
            ingredients_list = []
        ingredients_lines = []
        for ing in ingredients_list:
            s = str(ing).strip()
            if s:
                ingredients_lines.append(f"- {s}" if not s.startswith('-') else s)
        ingredients_text = '\n'.join(ingredients_lines).strip()

        instructions_raw = ''
        try:
            instructions_raw = scraper.instructions() or ''
        except Exception:
            instructions_raw = ''
        instructions_text = _normalize_multiline_steps(instructions_raw)

        # Champs optionnels
        description = ''
        if hasattr(scraper, 'description'):
            try:
                description = (scraper.description() or '').strip()  # type: ignore[operator]
            except Exception:
                description = ''

        servings = None
        if hasattr(scraper, 'yields'):
            try:
                servings = _parse_first_int(scraper.yields())  # type: ignore[operator]
            except Exception:
                servings = None

        # Temps (en minutes si possible)
        prep_time = None
        cook_time = None
        total_time = None
        if hasattr(scraper, 'prep_time'):
            try:
                prep_time = _parse_first_int(scraper.prep_time())  # type: ignore[operator]
            except Exception:
                prep_time = None
        if hasattr(scraper, 'cook_time'):
            try:
                cook_time = _parse_first_int(scraper.cook_time())  # type: ignore[operator]
            except Exception:
                cook_time = None
        if hasattr(scraper, 'total_time'):
            try:
                total_time = _parse_first_int(scraper.total_time())  # type: ignore[operator]
            except Exception:
                total_time = None

        # Si on n'a que total_time, on le met en cook_time (fallback) pour rester compatible
        if cook_time is None and total_time is not None:
            cook_time = total_time

        extracted = {
            'title': title,
            'description': description,
            'ingredients_text': ingredients_text,
            'instructions_text': instructions_text,
            'prep_time': prep_time,
            'cook_time': cook_time,
            'servings': servings,
            'image_path': image,
        }

        # On considère l'extraction valable si on a au moins un titre et de la matière
        if extracted.get('title') and (ingredients_text or instructions_text):
            return extracted
        if ingredients_text or instructions_text:
            # Titre manquant parfois: on laisse passer, l'IA peut en déduire un titre minimal
            return extracted

        return None
    except Exception as e:
        logger.warning("[RecipeScrapers] Extraction failed for url=%s: %s", url, e)
        return None


def count_ingredients_lines(ingredients_text: str) -> int:
    lines = []
    for ln in (ingredients_text or '').splitlines():
        cleaned = ln.strip()
        cleaned = re.sub(r'^[-*•\s]+', '', cleaned).strip()
        if cleaned:
            lines.append(cleaned)
    return len(lines)


def is_ingredients_suspicious(ingredients_text: str) -> tuple[bool, str]:
    """
    Heuristiques minimales et conservatrices.
    Objectif: bloquer les imports quand on n'a pas une liste d'ingrédients exploitable.
    """
    min_lines = int(config('RECIPE_IMPORT_MIN_INGREDIENT_LINES', default=2))
    min_chars = int(config('RECIPE_IMPORT_MIN_INGREDIENT_CHARS', default=10))

    txt = (ingredients_text or '').strip()
    if not txt:
        return True, "Aucun ingrédient extrait."

    if len(txt) < min_chars:
        return True, f"Texte d'ingrédients trop court ({len(txt)} caractères)."

    n = count_ingredients_lines(txt)
    if n < min_lines:
        return True, f"Nombre d'ingrédients trop faible ({n} < {min_lines})."

    return False, ""


def detect_source_type(url: str) -> Optional[str]:
    """Détecte le type de source depuis l'URL"""
    parsed = urlparse(url)
    domain = parsed.netloc.lower()
    
    if 'instagram.com' in domain or 'instagr.am' in domain:
        return 'instagram'
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


class InstagramImportError(Exception):
    def __init__(self, code: str, message: str = ''):
        self.code = code
        self.message = message or ''
        super().__init__(self.message or code)


def extract_instagram_recipe(url: str) -> Optional[Dict]:
    """
    Extraction d'une recette depuis un post Instagram public en utilisant Apify.
    - Récupère la légende + premier commentaire
    - Laisse l'IA détecter s'il s'agit bien d'une recette et structurer ingrédients/étapes
    """
    token = config('APIFY_API_TOKEN', default='')
    if not token:
        logger.error("[InstagramExtractor] APIFY_API_TOKEN n'est pas configuré")
        raise InstagramImportError(
            code='apify_not_configured',
            message="APIFY_API_TOKEN n'est pas configuré côté serveur.",
        )

    try:
        client = ApifyClient(token)
        run_input = {
            "directUrls": [url],
            "resultsType": "posts",
            "resultsLimit": 1,
        }
        logger.info("[InstagramExtractor] Lancement actor apify/instagram-scraper pour url=%s", url)
        run = client.actor("apify/instagram-scraper").call(run_input=run_input)
        dataset_id = run.get("defaultDatasetId")
        if not dataset_id:
            logger.warning("[InstagramExtractor] Aucun datasetId retourné par Apify pour url=%s", url)
            raise InstagramImportError(
                code='apify_failed',
                message="Aucun résultat retourné par l'API Apify.",
            )

        items_iter = client.dataset(dataset_id).iterate_items()
        first_item = next(items_iter, None)
    except InstagramImportError:
        raise
    except Exception as e:
        logger.error("[InstagramExtractor] Erreur lors de l'appel Apify pour url=%s: %s", url, e, exc_info=True)
        raise InstagramImportError(
            code='apify_failed',
            message=str(e),
        )

    if not first_item:
        logger.info("[InstagramExtractor] Dataset Apify vide pour url=%s", url)
        raise InstagramImportError(
            code='post_not_found',
            message="Aucun post trouvé pour cette URL Instagram.",
        )

    caption = (first_item.get("caption") or "").strip()
    first_comment = (first_item.get("firstComment") or "").strip()
    full_text_parts = []
    if caption:
        full_text_parts.append(caption)
    if first_comment:
        full_text_parts.append(first_comment)
    full_text = "\n\n".join(full_text_parts).strip()

    if not full_text:
        logger.info("[InstagramExtractor] Aucun texte exploitable (caption/firstComment) pour url=%s", url)
        raise InstagramImportError(
            code='no_recipe_text',
            message="Aucune description de recette trouvée dans ce post Instagram.",
        )

    # Appel IA pour parser la légende et structurer ingrédients/instructions
    from .ai_service import parse_instagram_caption  # import local pour éviter les cycles

    parsed = parse_instagram_caption(full_text)
    if not parsed.get("is_recipe"):
        logger.info(
            "[InstagramExtractor] Post identifié comme non-recette (code=no_recipe) pour url=%s, reason=%s",
            url,
            parsed.get("reason", ""),
        )
        raise InstagramImportError(
            code='no_recipe',
            message=parsed.get("reason", "Ce post ne semble pas contenir de recette exploitable."),
        )

    ingredients_text = (parsed.get("ingredients_text") or "").strip()
    instructions_text = (parsed.get("instructions_text") or "").strip()

    if not ingredients_text:
        logger.info(
            "[InstagramExtractor] Aucune liste d'ingrédients structurée après parsing IA pour url=%s",
            url,
        )
        raise InstagramImportError(
            code='no_ingredients',
            message="Impossible d'identifier une liste d'ingrédients exploitable dans ce post Instagram.",
        )

    image_url = first_item.get("displayUrl") or first_item.get("display_url") or ""
    image_url = (image_url or "").strip()

    title = (parsed.get("title") or "").strip()
    if not title:
        owner_username = (first_item.get("ownerUsername") or first_item.get("owner_username") or "").strip()
        short_code = (first_item.get("shortCode") or first_item.get("short_code") or "").strip()
        title_parts = []
        if owner_username:
            title_parts.append(owner_username)
        title_parts.append("Recette Instagram")
        if short_code:
            title_parts.append(short_code)
        title = " - ".join(title_parts)

    description = (caption or full_text)[:600]

    recipe_data: Dict[str, object] = {
        "title": title,
        "description": description,
        "ingredients_text": ingredients_text,
        "instructions_text": instructions_text,
        "prep_time": parsed.get("prep_time"),
        "cook_time": parsed.get("cook_time"),
        "servings": parsed.get("servings"),
        "image_path": image_url,
        "instagram_meta": {
            "id": first_item.get("id"),
            "shortCode": first_item.get("shortCode") or first_item.get("short_code"),
            "ownerUsername": first_item.get("ownerUsername") or first_item.get("owner_username"),
            "type": first_item.get("type"),
            "timestamp": first_item.get("timestamp"),
        },
    }

    return recipe_data


def _extract_recipe_from_json_ld(data, title_fallback: str = '') -> Optional[Dict]:
    """Extrait un dict recette depuis une structure JSON-LD si @type=Recipe"""
    if not data:
        return None

    # Si liste, chercher le premier Recipe (et parcourir @graph)
    if isinstance(data, list):
        for item in data:
            recipe = _extract_recipe_from_json_ld(item, title_fallback)
            if recipe:
                return recipe
        return None

    if not isinstance(data, dict):
        return None

    # Si @graph est présent, balayer les nœuds
    if '@graph' in data and isinstance(data['@graph'], list):
        for node in data['@graph']:
            recipe = _extract_recipe_from_json_ld(node, title_fallback)
            if recipe:
                return recipe

    types = data.get('@type') or data.get('type')
    if isinstance(types, list):
        is_recipe = any(t == 'Recipe' for t in types)
    else:
        is_recipe = types == 'Recipe'

    if not is_recipe:
        return None

    # Ingrédients
    ingredients_list = data.get('recipeIngredient', [])
    ingredients_text = '\n'.join([
        f"- {ing}" if isinstance(ing, str) else f"- {ing.get('name', '')}"
        for ing in ingredients_list
    ])

    # Instructions (schema.org varie beaucoup: string, list[str], list[HowToStep], HowToSection, ItemList)
    raw_instructions = data.get('recipeInstructions', [])
    steps: list[str] = []

    def _push_step(val):
        if not val:
            return
        if isinstance(val, str):
            txt = val.strip()
            if txt:
                steps.append(txt)
            return
        if isinstance(val, dict):
            # HowToStep / HowToSection
            txt = (val.get('text') or val.get('name') or '').strip()
            if txt:
                steps.append(txt)
                return
            # ItemList / nested
            nested = val.get('itemListElement') or val.get('steps') or val.get('elements')
            if isinstance(nested, list):
                for x in nested:
                    _push_step(x)
            return
        if isinstance(val, list):
            for x in val:
                _push_step(x)

    _push_step(raw_instructions)

    instructions_text = '\n'.join([f"{i+1}. {s}" for i, s in enumerate(steps)])

    # Image
    image = data.get('image', '')
    if isinstance(image, dict):
        image = image.get('url', '')
    elif isinstance(image, list) and image:
        image = image[0] if isinstance(image[0], str) else image[0].get('url', '')

    return {
        'title': data.get('name', title_fallback),
        'description': data.get('description', ''),
        'ingredients_text': ingredients_text,
        'instructions_text': instructions_text,
        'prep_time': data.get('prepTime'),
        'cook_time': data.get('cookTime'),
        'servings': data.get('recipeYield'),
        'image_path': image,
    }


def _fallback_extract_from_soup(soup: BeautifulSoup, title_hint: str = '') -> Optional[Dict]:
    """Extraction heuristique sur HTML (ingrédients/instructions)"""
    title_elem = soup.find('h1') or soup.find('title')
    title = title_elem.get_text(strip=True) if title_elem else title_hint

    # Chercher métadonnées d'image
    og_image = soup.find('meta', property='og:image') or soup.find('meta', attrs={'name': 'twitter:image'})
    image_url = og_image['content'] if og_image and og_image.has_attr('content') else ''
    if not image_url:
        first_img = soup.find('img')
        if first_img and first_img.has_attr('src'):
            image_url = first_img['src']

    def _collect_after_heading(patterns):
        items = []
        heading = None
        for tag in soup.find_all(['h2', 'h3', 'h4']):
            txt = tag.get_text(strip=True).lower()
            if any(p in txt for p in patterns):
                heading = tag
                break
        if not heading:
            return items

        for sibling in heading.find_next_siblings():
            if sibling.name in ['h1', 'h2', 'h3', 'h4']:
                break
            for li in sibling.find_all('li'):
                t = li.get_text(strip=True)
                if t:
                    items.append(t)
            if sibling.name in ['p', 'div']:
                t = sibling.get_text(strip=True)
                if t:
                    items.append(t)
        return items

    # 1) Essayer de collecter via headings explicites
    ingredients = _collect_after_heading(['ingrédient', 'ingredient'])
    instructions = _collect_after_heading(['préparation', 'preparation', 'instruction', 'étape', 'etape'])

    # 2) Si vide, fallback regex sur li/p
    if not ingredients or not instructions:
        for elem in soup.find_all(['li', 'p']):
            text = elem.get_text(strip=True)
            if not text or len(text) < 3:
                continue
            if re.search(r'\d+\s*(g|kg|ml|l|cl|tasse|cuill|pincée|gousse|pers\.|pers|personnes?)', text, re.IGNORECASE):
                ingredients.append(text)
            elif re.search(r'^(faire|mélanger|melanger|ajouter|cuire|chauffer|découper|decouper|verser|mettre|préchauffer|prechauffer)', text, re.IGNORECASE):
                instructions.append(text)

    if title and (ingredients or instructions):
        return {
            'title': title,
            'ingredients_text': '\n'.join([f"- {ing}" for ing in ingredients[:40]]),
            'instructions_text': '\n'.join([f"{i+1}. {inst}" for i, inst in enumerate(instructions[:40])]),
            'image_path': image_url,
        }

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


def extract_cuisineaz_recipe(url: str) -> Optional[Dict]:
    """Extrait une recette depuis CuisineAZ (JSON-LD prioritaire)"""
    try:
        response = requests.get(url, timeout=REQUEST_TIMEOUT, headers={
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        })
        response.raise_for_status()

        soup = BeautifulSoup(response.text, 'html.parser')

        # Titre (fallback)
        title_elem = soup.find('h1') or soup.find('title')
        title = title_elem.get_text(strip=True) if title_elem else ''

        # JSON-LD (si présent, on le traite en priorité)
        for script in soup.find_all('script', type='application/ld+json'):
            try:
                data = json.loads(script.string)
                recipe = _extract_recipe_from_json_ld(data, title)
                if recipe and (recipe.get('ingredients_text') or recipe.get('instructions_text')):
                    # CuisineAZ met souvent l'image via og:image même si JSON-LD est incomplet
                    if not recipe.get('image_path'):
                        og_image = soup.find('meta', property='og:image') or soup.find('meta', attrs={'name': 'twitter:image'})
                        if og_image and og_image.has_attr('content'):
                            recipe['image_path'] = og_image['content']
                    return recipe
            except (json.JSONDecodeError, TypeError):
                continue

        # Fallback HTML spécifique CuisineAZ (sélecteurs dédiés)
        ingredients_items = []
        ingredients_ul = soup.select_one('section.ingredients ul.ingredient_list')
        if ingredients_ul:
            for li in ingredients_ul.select('li.ingredient_item'):
                label = li.select_one('.ingredient_label')
                qty = li.select_one('.ingredient_qte')
                label_text = label.get_text(strip=True) if label else ''
                qty_text = qty.get_text(strip=True) if qty else ''
                combined = f"{qty_text} {label_text}".strip()
                if combined:
                    ingredients_items.append(combined)

        steps_items = []
        steps_ul = soup.select_one('section.instructions ul.preparation_steps')
        if steps_ul:
            for li in steps_ul.select('li.preparation_step'):
                # le texte est dans un <p> juste après le titre
                p = li.find('p')
                if p:
                    txt = p.get_text(strip=True)
                    if txt:
                        steps_items.append(txt)

        ingredients_text = '\n'.join([f"- {ing}" for ing in ingredients_items]) if ingredients_items else ''
        instructions_text = '\n'.join([f"{i+1}. {step}" for i, step in enumerate(steps_items)]) if steps_items else ''

        # Image via og:image
        og_image = soup.find('meta', property='og:image') or soup.find('meta', attrs={'name': 'twitter:image'})
        image_url = og_image['content'] if og_image and og_image.has_attr('content') else ''

        if title and (ingredients_text or instructions_text):
            return {
                'title': title,
                'description': '',
                'ingredients_text': ingredients_text,
                'instructions_text': instructions_text,
                'prep_time': None,
                'cook_time': None,
                'servings': None,
                'image_path': image_url,
            }

        # Ultime fallback générique
        return _fallback_extract_from_soup(soup, title)

    except Exception as e:
        logger.error(f"Erreur lors de l'extraction CuisineAZ: {e}")
        return None


def extract_jow_recipe(url: str) -> Optional[Dict]:
    """Extrait une recette depuis Jow.fr (JSON-LD prioritaire, fallback HTML ciblé)"""
    try:
        response = requests.get(url, timeout=REQUEST_TIMEOUT, headers={
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        })
        response.raise_for_status()

        soup = BeautifulSoup(response.text, 'html.parser')

        title_elem = soup.find('h1') or soup.find('title')
        title = title_elem.get_text(strip=True) if title_elem else ''

        # 1) JSON-LD (souvent présent sur Jow)
        for script in soup.find_all('script', type='application/ld+json'):
            try:
                data = json.loads(script.string)
                recipe = _extract_recipe_from_json_ld(data, title)
                if recipe and (recipe.get('ingredients_text') or recipe.get('instructions_text')):
                    if not recipe.get('image_path'):
                        og_image = soup.find('meta', property='og:image') or soup.find('meta', attrs={'name': 'twitter:image'})
                        if og_image and og_image.has_attr('content'):
                            recipe['image_path'] = og_image['content']
                    return recipe
            except (json.JSONDecodeError, TypeError):
                continue

        # 2) Fallback HTML ciblé Jow (sections "ingredients" / "instructions")
        def _find_section(name_fragment: str):
            return soup.find(
                lambda tag: tag.name in ['section', 'div'] and any(
                    name_fragment in (attr_val.lower() if isinstance(attr_val, str) else ' '.join(attr_val).lower())
                    for attr_val in [
                        tag.get('id') or '',
                        tag.get('aria-label') or '',
                        ' '.join(tag.get('class', []))
                    ]
                )
            )

        ingredients_section = _find_section('ingredient') or soup.find(id='ingredients')
        instructions_section = _find_section('instruction') or soup.find(id='instructions')

        ingredients_items = []
        if ingredients_section:
            for node in ingredients_section.find_all(['li', 'p', 'span']):
                txt = node.get_text(strip=True)
                if not txt:
                    continue
                # Garder des entrées avec quantité ou intitulé plausible
                if re.search(r'\d', txt) or len(txt.split()) >= 2:
                    ingredients_items.append(txt)

        steps_items = []
        if instructions_section:
            for node in instructions_section.find_all(['li', 'p', 'div']):
                txt = node.get_text(strip=True)
                if txt and len(txt) > 10:
                    steps_items.append(txt)

        # Compléter depuis headings si sections manquantes
        if not ingredients_items or not steps_items:
            extra = _fallback_extract_from_soup(soup, title)
            if extra:
                # Merge uniquement si besoin pour garder le contexte Jow
                if not ingredients_items and extra.get('ingredients_text'):
                    ingredients_items = [line.lstrip('- ').strip() for line in extra['ingredients_text'].splitlines() if line.strip()]
                if not steps_items and extra.get('instructions_text'):
                    steps_items = [line.split('. ', 1)[-1].strip() if '. ' in line else line for line in extra['instructions_text'].splitlines() if line.strip()]

        image_url = ''
        og_image = soup.find('meta', property='og:image') or soup.find('meta', attrs={'name': 'twitter:image'})
        if og_image and og_image.has_attr('content'):
            image_url = og_image['content']
        else:
            first_img = soup.find('img')
            if first_img and first_img.has_attr('src'):
                image_url = first_img['src']

        if title and (ingredients_items or steps_items):
            return {
                'title': title,
                'description': '',
                'ingredients_text': '\n'.join([f"- {ing}" for ing in ingredients_items]),
                'instructions_text': '\n'.join([f"{i+1}. {step}" for i, step in enumerate(steps_items)]),
                'prep_time': None,
                'cook_time': None,
                'servings': None,
                'image_path': image_url,
            }

        # 3) Dernier recours : fallback générique
        return _fallback_extract_from_soup(soup, title)

    except Exception as e:
        logger.error(f"Erreur lors de l'extraction Jow: {e}")
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
        for json_ld in soup.find_all('script', type='application/ld+json'):
            try:
                data = json.loads(json_ld.string)
                recipe = _extract_recipe_from_json_ld(data, title)
                if recipe:
                    return recipe
            except (json.JSONDecodeError, KeyError, TypeError):
                continue
        
        # Fallback : extraction basique
        return _fallback_extract_from_soup(soup, title)
        
    except Exception as e:
        logger.error(f"Erreur lors de l'extraction générique: {e}")
        return None


def extract_with_jina(url: str) -> Optional[Dict]:
    """Extraction via Jina Reader (fallback générique)"""
    token = config('JINA_API_TOKEN', default=None)
    if not token:
        logger.warning("[JinaExtractor] JINA_API_TOKEN manquant, fallback ignoré")
        return None

    try:
        jina_url = f"https://r.jina.ai/{url}"
        logger.info("[JinaExtractor] Fetching via Jina: %s", jina_url)
        response = requests.get(
            jina_url,
            timeout=REQUEST_TIMEOUT,
            headers={
                'Authorization': f"Bearer {token}",
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
            },
        )
        if response.status_code >= 400:
            logger.warning(
                "[JinaExtractor] Non-2xx (status=%s) for url=%s (body_len=%s)",
                response.status_code,
                url,
                len(response.text or ''),
            )
        response.raise_for_status()
        logger.info("[JinaExtractor] Success (status=%s) for url=%s", response.status_code, url)

        soup = BeautifulSoup(response.text, 'html.parser')

        # Titre
        title_elem = soup.find('h1') or soup.find('title')
        title = title_elem.get_text(strip=True) if title_elem else ''

        # JSON-LD d'abord
        for json_ld in soup.find_all('script', type='application/ld+json'):
            try:
                data = json.loads(json_ld.string)
                recipe = _extract_recipe_from_json_ld(data, title)
                if recipe:
                    return recipe
            except (json.JSONDecodeError, KeyError, TypeError):
                continue

        # Heuristique fallback
        return _fallback_extract_from_soup(soup, title)

    except Exception as e:
        logger.error(f"[JinaExtractor] Erreur lors de l'extraction via Jina: {e}")
        return None


from ..utils import canonicalize_import_url


def import_recipe_from_url(url: str) -> Tuple[Optional[Dict], Optional[str]]:
    """
    Importe une recette depuis une URL
    
    Returns:
        Tuple[Optional[Dict], Optional[str]]: (données de la recette, type de source)
    """
    if not url or not url.startswith(('http://', 'https://')):
        return None, None
    
    source_type = detect_source_type(url)
    logger.info(f"Import de recette depuis {url} (source detectée: {source_type})")
    
    recipe_data = None
    used_source = None
    
    if source_type == 'instagram':
        logger.info("[RecipeImporter] Trying extractor=instagram")
        recipe_data = extract_instagram_recipe(url)
        used_source = 'instagram' if recipe_data else None
    elif source_type == 'bergamot':
        logger.info("[RecipeImporter] Trying extractor=bergamot")
        recipe_data = extract_bergamot_recipe(url)
        used_source = 'bergamot' if recipe_data else None
    else:
        # Tous les autres sites passent d'abord par recipe-scrapers
        logger.info("[RecipeImporter] Trying extractor=recipe-scrapers")
        recipe_data = extract_with_recipe_scrapers(url)
        # Si la lib renvoie un résultat mais sans ingrédients / instructions, on tente nos fallbacks
        if recipe_data and not (recipe_data.get('ingredients_text') and recipe_data.get('instructions_text')):
            logger.warning(
                "[RecipeImporter] recipe-scrapers returned partial data (has_ingredients=%s, has_instructions=%s); falling back",
                bool(recipe_data.get('ingredients_text')),
                bool(recipe_data.get('instructions_text')),
            )
            recipe_data = None
        used_source = 'recipe-scrapers' if recipe_data else None

        # Fallback legacy extractors (pour les domaines historiques)
        if not recipe_data and source_type == 'marmiton':
            logger.info("[RecipeImporter] Fallback extractor=marmiton_legacy")
            recipe_data = extract_marmiton_recipe(url)
            used_source = 'marmiton' if recipe_data else None
        if not recipe_data and source_type == 'cuisineaz':
            logger.info("[RecipeImporter] Fallback extractor=cuisineaz_legacy")
            recipe_data = extract_cuisineaz_recipe(url)
            used_source = 'cuisineaz' if recipe_data else None
        if not recipe_data and source_type == 'jow':
            logger.info("[RecipeImporter] Fallback extractor=jow_legacy")
            recipe_data = extract_jow_recipe(url)
            used_source = 'jow' if recipe_data else None
    
    # Fallback Jina (scraping générique)
    if not recipe_data:
        logger.info("[RecipeImporter] Trying extractor=jina_fallback")
        recipe_data = extract_with_jina(url)
        if recipe_data:
            used_source = 'jina'
    
    # Fallback générique direct (HTML original)
    if not recipe_data:
        logger.info("[RecipeImporter] Trying extractor=generic_html_fallback")
        recipe_data = extract_generic_recipe(url)
        if recipe_data:
            used_source = used_source or 'generic'
    
    if recipe_data:
        # Stocker la version canonique de l'URL pour faciliter la déduplication
        canonical_url = canonicalize_import_url(url)
        recipe_data['import_source_url'] = canonical_url
        return recipe_data, used_source or source_type or 'generic'
    
    return None, source_type

