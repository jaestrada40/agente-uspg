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

from agent.tools import crear_solicitud_inscripcion

load_dotenv()
logger = logging.getLogger("agentkit")

# El cliente se crea recien cuando se necesita, no al importar el modulo: si falta
# GEMINI_API_KEY, el servidor tiene que poder arrancar igual y avisarlo en el health
# check, en vez de morirse en el import y dejar a Railway reiniciando el contenedor a ciegas.
_client: genai.Client | None = None


def _obtener_cliente() -> genai.Client:
    global _client
    if _client is None:
        _client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))
    return _client


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

# ── Tools (function calling) ─────────────────────────────────────────────────
#
# Unica herramienta real conectada hoy: crear la solicitud de inscripcion en el
# Sistema Academico USPG. El "telefono" NO es un parametro que el modelo pueda
# completar — lo inyecta _ejecutar_tool con el remitente real del mensaje de
# WhatsApp, para que la cuenta nunca quede a nombre de un numero equivocado.
_TOOLS = [
    types.Tool(
        function_declarations=[
            types.FunctionDeclaration(
                name="crear_solicitud_inscripcion",
                description=(
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
                parameters=types.Schema(
                    type=types.Type.OBJECT,
                    properties={
                        "nombre": types.Schema(
                            type=types.Type.STRING,
                            description="Nombre completo del aspirante, tal como lo confirmó en el chat.",
                        ),
                        "carrera": types.Schema(
                            type=types.Type.STRING,
                            description="Nombre exacto de la carrera de pregrado elegida (una de las 12 carreras de USPG).",
                        ),
                        "correo_personal": types.Schema(
                            type=types.Type.STRING,
                            description=(
                                "Correo electrónico personal del aspirante (Gmail, Outlook, etc.), "
                                "tal como lo escribió en el chat. El Sistema Académico le envía ahí "
                                "sus credenciales, porque todavía no puede entrar a su correo "
                                "institucional nuevo. Debe tener formato válido (algo@algo.dominio)."
                            ),
                        ),
                    },
                    required=["nombre", "carrera", "correo_personal"],
                ),
            )
        ]
    )
]

# Numero maximo de rondas de tool-calling por mensaje. Con 1 sola herramienta
# conectada nunca deberian hacer falta mas de 2, pero el limite evita que una
# alucinacion del modelo (llamar la funcion en bucle) cuelgue la respuesta.
_MAX_TOOL_ROUNDS = 3


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


async def _ejecutar_tool(nombre_funcion: str, argumentos: dict, telefono: str) -> dict:
    """
    Ejecuta la herramienta que Gemini pidio invocar. El telefono NUNCA sale de
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
    logger.warning(f"Gemini pidio una funcion desconocida: {nombre_funcion}")
    return {"status": "error", "message": "Esa acción no está disponible."}


async def generar_respuesta(mensaje: str, historial: list[dict], telefono: str) -> tuple[str, bool]:
    """
    Genera una respuesta con Gemini.

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

    contenidos = _mapear_historial(historial)
    contenidos.append({"role": "user", "parts": [{"text": mensaje}]})

    system_prompt = cargar_system_prompt()

    if not os.getenv("GEMINI_API_KEY"):
        logger.error("GEMINI_API_KEY no esta configurada: no se puede llamar a Gemini")
        return obtener_mensaje_error(), False

    config = types.GenerateContentConfig(
        system_instruction=system_prompt,
        max_output_tokens=MAX_TOKENS,
        tools=_TOOLS,
    )

    for ronda in range(_MAX_TOOL_ROUNDS):
        try:
            respuesta = await _obtener_cliente().aio.models.generate_content(
                model=MODELO, contents=contenidos, config=config
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

        llamadas = getattr(respuesta, "function_calls", None) or []
        if not llamadas:
            break

        # Se agrega el turno del modelo (con la/s llamada/s a funcion) al historial
        # de la conversacion con Gemini, y despues la respuesta de cada funcion —
        # asi Gemini puede redactar el mensaje final usando ese resultado.
        contenido_modelo = candidatos[0].content if candidatos else None
        if contenido_modelo:
            contenidos.append(contenido_modelo)

        partes_respuesta = []
        for llamada in llamadas:
            logger.info(f"Gemini pidio ejecutar {llamada.name}({dict(llamada.args or {})})")
            resultado = await _ejecutar_tool(llamada.name, dict(llamada.args or {}), telefono)
            partes_respuesta.append(
                types.Part.from_function_response(name=llamada.name, response=resultado)
            )
        contenidos.append(types.Content(role="user", parts=partes_respuesta))
    else:
        logger.warning(f"Se alcanzo el limite de {_MAX_TOOL_ROUNDS} rondas de tool-calling")

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
