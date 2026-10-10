# agent/brain.py — Cerebro del agente: conexion con un proveedor compatible con OpenAI
# Generado por AgentKit (adaptado a OpenRouter/Groq a pedido del usuario, para poder probar el
# agente sin costo mientras se decide si pasar a Claude en produccion)

"""
Logica de IA del agente. Lee el system prompt de config/prompts.yaml y genera las
respuestas con cualquier API compatible con el formato de OpenAI (POST /chat/completions):
OpenRouter, Groq, etc. Se cambia de proveedor desde el .env. Se usa httpx, que ya es dependencia del proyecto, asi que
no hace falta instalar ningun SDK nuevo.

Nota para el futuro: si mas adelante se quiere migrar a otro motor (Claude, etc.), la
unica pieza que hay que reescribir es este archivo. main.py, memory.py y tools.py no
saben ni les importa que motor de IA esta detras: solo llaman a generar_respuesta().
"""

import asyncio
import json
import logging
import os

import httpx
import yaml
from dotenv import load_dotenv

from agent.tools import crear_solicitud_inscripcion

load_dotenv()
logger = logging.getLogger("agentkit")

# Si falta LLM_API_KEY el servidor tiene que poder arrancar igual y avisarlo, en vez de
# morirse en el import y dejar a Railway reiniciando el contenedor a ciegas. Por eso la
# clave se lee recien al momento de llamar, no al importar el modulo.
BASE_URL = (os.getenv("LLM_BASE_URL") or "https://openrouter.ai/api/v1").rstrip("/")

# El modelo se cambia desde .env, sin tocar el codigo. Ejemplos en OpenRouter (gratis):
#   nvidia/nemotron-3-super-120b-a12b:free
#   google/gemma-4-31b-it:free
# Los ids cambian con el tiempo: si el proveedor responde "model not found", revisa
# la lista vigente en openrouter.ai/models.
# El "or" y no el default de os.getenv: una variable declarada vacia en el .env
# devuelve "" y dejaria al agente sin modelo.
MODELO = os.getenv("LLM_MODEL") or "nvidia/nemotron-3-super-120b-a12b:free"

# WhatsApp son mensajes cortos: este tope solo evita respuestas desbordadas.
MAX_TOKENS = int(os.getenv("LLM_MAX_TOKENS") or "2048")

# ── Tools (function calling) ─────────────────────────────────────────────────
#
# Unica herramienta real conectada hoy: crear la solicitud de inscripcion en el
# Sistema Academico USPG. El "telefono" NO es un parametro que el modelo pueda
# completar — lo inyecta _ejecutar_tool con el remitente real del mensaje de
# WhatsApp, para que la cuenta nunca quede a nombre de un numero equivocado.
_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "crear_solicitud_inscripcion",
            "description": (
                "Registra a un aspirante en el Sistema Academico de USPG a partir de "
                "esta conversacion. Si la carrera tiene cupo cargado en el sistema, crea "
                "la cuenta real al instante (carne, correo institucional y contrasena "
                "temporal); si no, la solicitud queda para revision manual del equipo de "
                "admisiones. SOLO llamar despues de que el aspirante confirmo "
                "explicitamente su nombre completo y la carrera exacta que quiere — "
                "resume esos datos (nombre, carrera y correo personal) y pide un 'si' o "
                "'confirmo' antes de invocar esta funcion. Nunca la llames dos veces "
                "para el mismo aspirante en la misma conversacion."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "nombre": {
                        "type": "string",
                        "description": "Nombre completo del aspirante, tal como lo confirmó en el chat.",
                    },
                    "carrera": {
                        "type": "string",
                        "description": "Nombre exacto de la carrera de pregrado elegida (una de las 12 carreras de USPG).",
                    },
                    "correo_personal": {
                        "type": "string",
                        "description": (
                            "Correo electrónico personal del aspirante (Gmail, Outlook, etc.), "
                            "tal como lo escribió en el chat. El Sistema Académico le envía ahí "
                            "sus credenciales, porque todavía no puede entrar a su correo "
                            "institucional nuevo. Debe tener formato válido (algo@algo.dominio)."
                        ),
                    },
                },
                "required": ["nombre", "carrera", "correo_personal"],
            },
        },
    }
]

# Numero maximo de rondas de tool-calling por mensaje. Con 1 sola herramienta
# conectada nunca deberian hacer falta mas de 2, pero el limite evita que una
# alucinacion del modelo (llamar la funcion en bucle) cuelgue la respuesta.
_MAX_TOOL_ROUNDS = 3

# Si el proveedor responde 429 (limite de la capa gratuita) se espera y se reintenta una vez,
# siempre que la espera sea corta: no tiene sentido dejar al cliente colgado mucho rato.
_ESPERA_MAXIMA_REINTENTO = 8.0


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


def _detalle_error(r: httpx.Response) -> str:
    """Resume un error del proveedor sin volcar el cuerpo crudo: solo el mensaje del error."""
    try:
        return str((r.json().get("error") or {}).get("message", ""))[:300]
    except ValueError:
        return ""


async def _llamar_llm(mensajes: list[dict]) -> dict | None:
    """
    Hace una llamada a /chat/completions. Retorna el JSON de la respuesta, o None si
    fallo (el motivo queda en el log).
    """
    clave = os.getenv("LLM_API_KEY", "")
    if not clave:
        logger.error("LLM_API_KEY no esta configurada: no se puede llamar al modelo")
        return None

    cuerpo = {
        "model": MODELO,
        "messages": mensajes,
        "max_tokens": MAX_TOKENS,
        "tools": _TOOLS,
    }
    cabeceras = {"Authorization": f"Bearer {clave}", "Content-Type": "application/json"}

    for intento in range(2):
        try:
            async with httpx.AsyncClient(timeout=60.0) as cliente:
                r = await cliente.post(
                    f"{BASE_URL}/chat/completions", json=cuerpo, headers=cabeceras
                )
        except httpx.HTTPError as e:
            logger.error(f"Error de red hablando con el proveedor de IA: {e}")
            return None

        if r.status_code == 200:
            return r.json()

        if r.status_code == 429 and intento == 0:
            try:
                espera = float(r.headers.get("retry-after", "2"))
            except ValueError:
                espera = 2.0
            if espera <= _ESPERA_MAXIMA_REINTENTO:
                logger.warning(f"El proveedor limito la tasa (429); se reintenta en {espera:.0f}s")
                await asyncio.sleep(espera)
                continue

        logger.error(f"El proveedor de IA rechazo la llamada [{r.status_code}]: {_detalle_error(r)}")
        return None

    return None


async def _ejecutar_tool(nombre_funcion: str, argumentos: dict, telefono: str) -> dict:
    """
    Ejecuta la herramienta que el modelo pidio invocar. El telefono NUNCA sale de
    `argumentos` (lo que escribio el modelo): siempre es el remitente real del
    mensaje de WhatsApp que main.py le pasa a generar_respuesta.
    """
    if nombre_funcion == "crear_solicitud_inscripcion":
        return await crear_solicitud_inscripcion(
            telefono=telefono,
            nombre=str(argumentos.get("nombre", "")),
            carrera=str(argumentos.get("carrera", "")),
            correo_personal=str(argumentos.get("correo_personal", "")),
        )
    logger.warning(f"El modelo pidio una funcion desconocida: {nombre_funcion}")
    return {"status": "error", "message": "Esa acción no está disponible."}


async def generar_respuesta(mensaje: str, historial: list[dict], telefono: str) -> tuple[str, bool]:
    """
    Genera una respuesta con el modelo configurado.

    Args:
        mensaje: el mensaje nuevo del cliente
        historial: los mensajes anteriores, [{"role": "user"|"assistant", "content": "..."}]
        telefono: el remitente real del mensaje de WhatsApp (lo usan las tools, nunca
            se lo pedimos al modelo)

    Returns:
        (texto, es_respuesta_real)

        "es_respuesta_real" es False cuando lo que se devuelve es un aviso tecnico
        (error o fallback) y no una respuesta del agente. main.py lo usa para no
        guardar esos avisos en el historial: si se guardaran, quedarian contaminando
        el contexto de todos los mensajes siguientes.
    """
    if not mensaje or len(mensaje.strip()) < 2:
        return obtener_mensaje_fallback(), False

    # El historial guardado ya viene en formato "user"/"assistant", el mismo que usa
    # la API de OpenAI: solo hay que anteponer el system prompt.
    mensajes: list[dict] = [{"role": "system", "content": cargar_system_prompt()}]
    mensajes += [{"role": m["role"], "content": m["content"]} for m in historial]
    mensajes.append({"role": "user", "content": mensaje})

    datos: dict | None = None
    eleccion: dict = {}

    for ronda in range(_MAX_TOOL_ROUNDS):
        datos = await _llamar_llm(mensajes)
        if datos is None:
            return obtener_mensaje_error(), False

        elecciones = datos.get("choices") or []
        eleccion = elecciones[0] if elecciones else {}
        msg = eleccion.get("message") or {}

        if eleccion.get("finish_reason") == "length":
            logger.warning(
                f"La respuesta se corto por llegar al tope de {MAX_TOKENS} tokens. "
                "Si pasa seguido, sube LLM_MAX_TOKENS."
            )

        llamadas = msg.get("tool_calls") or []
        if not llamadas:
            break

        # Se agrega el turno del modelo (con sus llamadas a funcion) y despues el
        # resultado de cada una, para que el modelo redacte el mensaje final con eso.
        mensajes.append(
            {"role": "assistant", "content": msg.get("content") or "", "tool_calls": llamadas}
        )
        for llamada in llamadas:
            funcion = llamada.get("function") or {}
            nombre = funcion.get("name", "")
            try:
                argumentos = json.loads(funcion.get("arguments") or "{}")
                if not isinstance(argumentos, dict):
                    argumentos = {}
            except ValueError:
                argumentos = {}
            logger.info(f"El modelo pidio ejecutar {nombre}")
            resultado = await _ejecutar_tool(nombre, argumentos, telefono)
            mensajes.append(
                {
                    "role": "tool",
                    "tool_call_id": llamada.get("id", ""),
                    "content": json.dumps(resultado, ensure_ascii=False),
                }
            )
    else:
        logger.warning(f"Se alcanzo el limite de {_MAX_TOOL_ROUNDS} rondas de tool-calling")

    texto = ((eleccion.get("message") or {}).get("content") or "").strip()
    if not texto:
        logger.warning(
            f"El modelo devolvio una respuesta sin texto (finish_reason={eleccion.get('finish_reason')})"
        )
        return obtener_mensaje_fallback(), False

    uso = (datos or {}).get("usage") or {}
    logger.info(
        f"Respuesta generada con {MODELO} "
        f"({uso.get('prompt_tokens', '?')} in / {uso.get('completion_tokens', '?')} out)"
    )
    return texto, True
