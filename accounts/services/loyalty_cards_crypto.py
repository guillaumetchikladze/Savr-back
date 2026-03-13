import os
import base64
from typing import Tuple

from cryptography.fernet import Fernet, InvalidToken
from decouple import config as decouple_config


def _get_fernet() -> Fernet:
    """
    Retourne une instance Fernet initialisée avec la clé LOYALTY_CARD_SECRET_KEY.

    La clé doit être une clé Fernet valide (base64 urlsafe de 32 octets),
    générée par exemple avec:

        from cryptography.fernet import Fernet
        print(Fernet.generate_key().decode())
    """
    # 1) Essayer dans les variables d'environnement classiques
    raw_key = os.environ.get("LOYALTY_CARD_SECRET_KEY")
    # 2) Sinon, utiliser python-decouple qui lit le fichier .env
    if not raw_key:
        raw_key = decouple_config("LOYALTY_CARD_SECRET_KEY", default=None)
    if not raw_key:
        raise RuntimeError("LOYALTY_CARD_SECRET_KEY is not configured")

    # Accepter soit une clé déjà encodée base64 (clé Fernet standard),
    # soit une chaîne brute qu'on convertit en bytes et qu'on re-base64 si besoin.
    key_bytes = str(raw_key).encode("utf-8")
    try:
        # Si c'est déjà une clé Fernet valide, ceci ne lève pas d'erreur
        base64.urlsafe_b64decode(key_bytes)
        return Fernet(key_bytes)
    except Exception:
        # Fallback: dériver une clé 32 octets puis l'encoder en base64 urlsafe
        # (éviter d'exposer la valeur brute en clair dans la base).
        from hashlib import sha256

        digest = sha256(key_bytes).digest()  # 32 bytes
        derived = base64.urlsafe_b64encode(digest)
        return Fernet(derived)


def encrypt_card_number(plain: str) -> Tuple[str, str]:
    """
    Chiffre le numéro de carte et retourne (ciphertext, last4).

    - `ciphertext` est une chaîne opaque stockée en base (TextField)
    - `last4` contient les 4 derniers caractères pour l'affichage (non sensible)
    """
    if plain is None:
        raise ValueError("Card number cannot be None")

    plain_str = str(plain).strip()
    if not plain_str:
        raise ValueError("Card number cannot be empty")

    f = _get_fernet()
    token = f.encrypt(plain_str.encode("utf-8")).decode("utf-8")
    last4 = plain_str[-4:] if len(plain_str) >= 4 else plain_str
    return token, last4


def decrypt_card_number(ciphertext: str) -> str:
    """
    Déchiffre le numéro de carte à partir du ciphertext stocké.

    Lève InvalidToken si la clé a changé ou si les données sont corrompues.
    """
    if not ciphertext:
        raise ValueError("Ciphertext is empty")

    f = _get_fernet()
    try:
        value = f.decrypt(ciphertext.encode("utf-8"))
    except InvalidToken as exc:
        # Ne jamais logger la valeur chiffrée ni la valeur potentielle en clair.
        raise InvalidToken("Unable to decrypt loyalty card number") from exc
    return value.decode("utf-8")

