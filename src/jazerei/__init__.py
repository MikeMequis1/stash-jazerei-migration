"""Cliente do Ja Zerei (destino). Somente leitura."""

from src.jazerei.auth import Auth, MissingAuthError
from src.jazerei.client import JazereiClient

__all__ = ["Auth", "JazereiClient", "MissingAuthError"]
