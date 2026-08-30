"""Controles de seguridad reutilizables para el servidor."""

import re


def ocultar_telefono(valor: str) -> str:
    """Devuelve una referencia util para correlacionar eventos sin revelar PII."""
    digitos = re.sub(r"\D", "", valor or "")
    if len(digitos) <= 4:
        return "***"
    return f"***{digitos[-4:]}"
