"""Construction de noms de fichiers sûrs à partir des noms de clients.

Un nom de client provient d'un PDF : il peut contenir des caractères interdits
par Windows (``\\ / : * ? " < > |``), des espaces multiples, ou correspondre à un
nom de périphérique réservé. Toute écriture de fichier ou d'entrée ZIP passe
obligatoirement par `sanitize_filename`.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable

#: Caractères interdits dans un nom de fichier Windows, plus les séparateurs.
_FORBIDDEN_RE = re.compile(r'[<>:"/\\|?*]')

#: Caractères de contrôle U+0000..U+001F, refusés par la plupart des systèmes.
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")

_WHITESPACE_RE = re.compile(r"\s+")

#: Noms réservés MS-DOS, toujours invalides même avec une extension.
_RESERVED_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{i}" for i in range(1, 10)}
    | {f"LPT{i}" for i in range(1, 10)}
)

#: Marge de sécurité : bien en deçà de la limite de 255 caractères par segment.
MAX_STEM_LENGTH = 120

FALLBACK_STEM = "SANS_NOM"


def sanitize_filename(raw: str, *, max_length: int = MAX_STEM_LENGTH) -> str:
    """Transforme un nom libre en segment de chemin valide, sans extension.

    Les accents sont conservés (les archives ZIP sont écrites en UTF-8) ; seuls
    les caractères réellement dangereux sont remplacés par un tiret.

    >>> sanitize_filename("Hôtel Nord Pinus Tanger")
    'Hôtel Nord Pinus Tanger'
    >>> sanitize_filename("O & M / CONCEPT")
    'O & M - CONCEPT'
    >>> sanitize_filename("   ")
    'SANS_NOM'
    >>> sanitize_filename("CON")
    '_CON'
    """
    if max_length < 1:
        raise ValueError("max_length doit être >= 1")

    # NFC : deux écritures Unicode d'un même accent produisent le même fichier.
    text = unicodedata.normalize("NFC", raw or "")
    text = _CONTROL_RE.sub("", text)
    text = _FORBIDDEN_RE.sub("-", text)
    text = _WHITESPACE_RE.sub(" ", text).strip()

    # Windows supprime silencieusement les points et espaces finaux.
    text = text.rstrip(" .")

    # Un nom réduit à de la ponctuation ne désigne plus rien d'exploitable.
    if not any(char.isalnum() for char in text):
        return FALLBACK_STEM

    if len(text) > max_length:
        text = text[:max_length].rstrip(" .-")
        if not text:
            return FALLBACK_STEM

    if text.upper() in _RESERVED_NAMES:
        text = f"_{text}"

    return text


def unique_filename(candidate: str, taken: Iterable[str]) -> str:
    """Rend `candidate` unique par rapport à `taken`, sans distinction de casse.

    La comparaison est insensible à la casse car NTFS, APFS et les archives ZIP
    ouvertes sous Windows ne distinguent pas ``CLIENT.pdf`` de ``client.pdf``.

    >>> unique_filename("DRAKE.pdf", ["DRAKE.pdf"])
    'DRAKE (2).pdf'
    >>> unique_filename("DRAKE.pdf", ["drake.pdf", "DRAKE (2).pdf"])
    'DRAKE (3).pdf'
    """
    used = {item.casefold() for item in taken}
    if candidate.casefold() not in used:
        return candidate

    stem, dot, extension = candidate.rpartition(".")
    if not dot:  # pas d'extension
        stem, extension = candidate, ""
    suffix = f".{extension}" if dot else ""

    counter = 2
    while True:
        attempt = f"{stem} ({counter}){suffix}"
        if attempt.casefold() not in used:
            return attempt
        counter += 1
