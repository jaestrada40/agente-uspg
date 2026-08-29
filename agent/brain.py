# agent/brain.py — Cerebro del agente: conexion con Gemini
# Generado por AgentKit (adaptado a Gemini a pedido del usuario, en vez de Anthropic,
# para poder probar el agente sin costo mientras se decide si pasar a Claude en produccion)

"""
Logica de IA del agente. Lee el system prompt de config/prompts.yaml y genera las
respuestas con la API de Gemini (Google AI Studio / google-genai SDK).

Nota para el futuro: si mas adelante se quiere migrar a la API de Anthropic (Claude),
la unica pieza que hay que reescribir es este archivo. main.py, memory.py y tools.py
no saben ni les importa que motor de IA esta detras: solo llaman a generar_respuesta().
"""

import logging
import os

import yaml
from dotenv import load_dotenv
from google import genai
from google.genai import types

load_dotenv()
logger = logging.getLogger("agentkit")

client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))

# El modelo se cambia desde .env, sin tocar el codigo.
# gemini-3.5-flash es el default: rapido y con capa gratuita para probar.
# gemini-3.5-flash-lite es todavia mas barato/rapido para preguntas muy simples.
# El "or" y no el default de os.getenv: una variable declarada vacia en el .env
# devuelve "" y dejaria al agente sin modelo.
MODELO = os.getenv("GEMINI_MODEL") or "gemini-3.5-flash"

# WhatsApp son mensajes cortos, pero este tope tambien cubre el razonamiento interno
# del modelo en los que lo tienen: con el margen justo, una pregunta que exija pensar
# un poco deja al agente sin espacio para contestar.
MAX_TOKENS = int(os.getenv("GEMINI_MAX_TOKENS") or "2048")


def cargar_config_prompts() -> dict:
    """Lee toda la configuracion desde config/prompts.yaml."""
    try:
        with open("config/prompts.yaml", "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except FileNotFoundError:
        logger.error("config/prompts.yaml no encontrado")
        return {}


def cargar_system_prompt() -> str:
    """El system prompt: quien es el agente y que sabe del negocio."""
    return cargar_config_prompts().get(
        "system_prompt", "Eres un asistente util. Responde siempre en espanol."
    )


def obtener_mensaje_error() -> str:
    """Que decirle al cliente cuando algo falla de nuestro lado."""
    return cargar_config_prompts().get(
        "error_message",
        "Lo siento, estoy teniendo problemas tecnicos. Por favor intenta de nuevo en unos minutos.",
    )


def obtener_mensaje_fallback() -> str:
    """Que decirle al cliente cuando no se entendio el mensaje."""
    return cargar_config_prompts().get(
        "fallback_message", "Disculpa, no entendi tu mensaje. Podrias reformularlo?"
    )


def _mapear_historial(historial: list[dict]) -> list[dict]:
    """
    Convierte el historial guardado (formato "user"/"assistant", igual al de la API de
    Claude, para no tener que tocar memory.py) al formato que espera Gemini: los turnos
    del agente van con role "model", no "assistant".
    """
    contenidos = []
    for m in historial:
        rol = "model" if m.get("role") == "assistant" else "user"
        contenidos.append({"role": rol, "parts": [{"text": m.get("content", "")}]})
    return contenidos


def _extraer_texto(respuesta) -> str:
    """
    Junta el texto de la respuesta de Gemini.

    El accesor .text de la libreria ya concatena las partes de texto del primer
    candidato, pero puede fallar o venir vacio (por ejemplo si el filtro de seguridad
    corto la respuesta antes de emitir texto), asi que se arma un respaldo manual.
    """
    try:
        texto = respuesta.text
    except Exception:  # noqa: BLE001
        texto = None
    if texto:
        return texto.strip()

    partes = []
    for candidato in getattr(respuesta, "candidates", None) or []:
        contenido = getattr(candidato, "content", None)
        for parte in getattr(contenido, "parts", None) or []:
            if getattr(parte, "text", None):
                partes.append(parte.text)
    return "\n".join(partes).strip()


async def generar_respuesta(mensaje: str, historial: list[dict]) -> tuple[str, bool]:
    """
    Genera una respuesta con Gemini.

    Args:
        mensaje: el mensaje nuevo del cliente
        historial: los mensajes anteriores, [{"role": "user"|"assistant", "content": "..."}]

    Returns:
        (texto, es_respuesta_real)

        "es_respuesta_real" es False cuando lo que se devuelve es un aviso tecnico
        (error o fallback) y no una respuesta del agente. main.py lo usa para no
        guardar esos avisos en el historial: si se guardaran, quedarian contaminando
        el contexto de todos los mensajes siguientes.
    """
    if not mensaje or len(mensaje.strip()) < 2:
        return obtener_mensaje_fallback(), False

    contenidos = _mapear_historial(historial)
    contenidos.append({"role": "user", "parts": [{"text": mensaje}]})

    system_prompt = cargar_system_prompt()

    try:
        respuesta = await client.aio.models.generate_content(
            model=MODELO,
            contents=contenidos,
            config=types.GenerateContentConfig(
                system_instruction=system_prompt,
                max_output_tokens=MAX_TOKENS,
            ),
        )
    except Exception as e:  # noqa: BLE001
        logger.error(f"Error llamando a Gemini: {e}")
        return obtener_mensaje_error(), False

    candidatos = getattr(respuesta, "candidates", None) or []
    razon_final = getattr(candidatos[0], "finish_reason", None) if candidatos else None
    if razon_final and "MAX_TOKENS" in str(razon_final):
        logger.warning(
            f"La respuesta se corto por llegar al tope de {MAX_TOKENS} tokens. "
            "Si pasa seguido, sube GEMINI_MAX_TOKENS o acorta el system prompt."
        )

    texto = _extraer_texto(respuesta)
    if not texto:
        logger.warning(f"Gemini devolvio una respuesta sin texto (finish_reason={razon_final})")
        return obtener_mensaje_fallback(), False

    uso = getattr(respuesta, "usage_metadata", None)
    if uso:
        logger.info(
            f"Respuesta generada con {MODELO} "
            f"({uso.prompt_token_count} in / {uso.candidates_token_count} out)"
        )
    else:
        logger.info(f"Respuesta generada con {MODELO}")

    return texto, True
