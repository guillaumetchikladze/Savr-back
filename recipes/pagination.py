from rest_framework.pagination import PageNumberPagination


class CustomPageNumberPagination(PageNumberPagination):
    """
    Pagination personnalisée qui permet au client de spécifier la taille de page
    via le paramètre 'page_size' dans la requête
    """
    page_size = 20  # Taille par défaut
    page_size_query_param = 'page_size'  # Permet au client de changer la taille
    max_page_size = 100  # Limite maximale pour éviter les abus


