"""Cancelamento cooperativo de operações longas da interface."""

from __future__ import annotations


class OperationCancelled(RuntimeError):
    """Solicitação humana de parada antes da publicação de um artefato."""
