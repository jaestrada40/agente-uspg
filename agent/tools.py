# agent/tools.py — Herramientas del agente
# Generado por AgentKit

"""
Herramientas especificas de la Universidad San Pablo de Guatemala (USPG).

OJO: estas funciones NO se ejecutan solas todavia. La informacion de carreras y
posgrados le llega al agente por el system prompt (config/prompts.yaml), asi que para
CONTESTAR preguntas no hace falta nada de aca. Este archivo es el lugar para las
ACCIONES —agendar una cita, registrar un lead, dar seguimiento— y conectarlas al ciclo
de "function calling" de Gemini (agent/brain.py) es un paso aparte, todavia no hecho:
por ahora estan listas para usarse a mano o para conectarlas cuando se agregue esa parte.
"""

import logging
import os
import re
from pathlib import Path

import httpx
import yaml

from agent.memory import (
    actualizar_estado_lead,
    cancelar_cita,
    crear_cita,
    listar_citas,
    listar_leads_para_seguimiento,
    registrar_lead,
)

logger = logging.getLogger("agentkit")

CARPETA_KNOWLEDGE = Path("knowledge")


def cargar_info_negocio() -> dict:
    """Carga la informacion del negocio desde config/business.yaml."""
    try:
        with open("config/business.yaml", "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except FileNotFoundError:
        logger.error("config/business.yaml no encontrado")
        return {}


def obtener_horario() -> dict:
    """Retorna el horario de atencion de la universidad."""
    info = cargar_info_negocio()
    return {
        "horario": info.get("negocio", {}).get("horario", "No disponible"),
        "esta_abierto": True,  # TODO: calcular segun la hora actual y el horario
    }


def buscar_en_knowledge(consulta: str) -> str:
    """
    Busca informacion en los archivos de /knowledge (carreras, posgrados, y lo que se
    vaya agregando: requisitos, precios, becas, fechas).
    Retorna los fragmentos que coinciden con la consulta.
    """
    if not CARPETA_KNOWLEDGE.is_dir():
        return "No hay archivos de conocimiento disponibles."

    resultados = []
    for ruta in sorted(CARPETA_KNOWLEDGE.iterdir()):
        if ruta.name.startswith(".") or not ruta.is_file():
            continue
        try:
            contenido = ruta.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue  # binarios y archivos ilegibles se saltean
        if consulta.lower() in contenido.lower():
            resultados.append(f"[{ruta.name}]: {contenido[:500]}")

    if resultados:
        return "\n---\n".join(resultados)
    return "No encontre informacion especifica sobre eso en mis archivos."


# ── Orientacion vocacional (agendar citas) ──────────────────────────────────


async def obtener_slots_disponibles(fecha: str) -> list[str]:
    """
    Horarios disponibles para orientacion vocacional en una fecha dada.

    TODO: esto es un placeholder con horarios fijos. Cuando la universidad tenga un
    calendario real (Google Calendar, Calendly, etc.) hay que conectarlo aca y
    descontar los horarios que ya tiene una cita en `listar_citas`.
    """
    return ["09:00", "10:00", "11:00", "14:00", "15:00"]


async def reservar_cita(telefono: str, fecha: str, hora: str, nombre: str | None = None) -> dict:
    """Agenda una cita de orientacion vocacional para un aspirante."""
    cita_id = await crear_cita(
        telefono=telefono, fecha=fecha, hora=hora, nombre=nombre, motivo="Orientación vocacional"
    )
    return {"cita_id": cita_id, "fecha": fecha, "hora": hora, "estado": "pendiente"}


async def ver_citas_de(telefono: str) -> list[dict]:
    """Lista las citas agendadas por un aspirante."""
    return await listar_citas(telefono=telefono)


async def cancelar_cita_agendada(cita_id: int) -> bool:
    """Cancela una cita de orientacion vocacional."""
    return await cancelar_cita(cita_id)


# ── Leads (calificacion y seguimiento de aspirantes) ────────────────────────


async def registrar_interes(telefono: str, nombre: str | None = None, carrera: str | None = None) -> int:
    """
    Registra a un aspirante como lead cuando muestra interes en una carrera.
    Se usa para poder darle seguimiento despues si no completa su inscripcion.
    """
    return await registrar_lead(telefono=telefono, nombre=nombre, carrera_interes=carrera)


async def marcar_inscrito(telefono: str, notas: str | None = None) -> bool:
    """Marca a un aspirante como inscrito: ya no necesita seguimiento de admisiones."""
    return await actualizar_estado_lead(telefono=telefono, estado="inscrito", notas=notas)


async def marcar_en_seguimiento(telefono: str, notas: str | None = None) -> bool:
    """
    Marca a un aspirante para seguimiento: mostro interes pero no completo la
    inscripcion todavia. `listar_pendientes_de_seguimiento` los recupera despues.
    """
    return await actualizar_estado_lead(telefono=telefono, estado="en_seguimiento", notas=notas)


async def listar_pendientes_de_seguimiento() -> list[dict]:
    """
    Aspirantes que mostraron interes pero no se han inscrito todavia.

    Pensado para un proceso aparte (ej. un cron o una vista de admisiones) que revise
    esta lista y decida a quien contactar de nuevo — el agente reactivo por si solo no
    puede escribir primero fuera de la ventana de 24 horas de WhatsApp sin plantilla.
    """
    return await listar_leads_para_seguimiento()


# Validación mínima de correo: algo@algo.dominio. No pretende cubrir el RFC entero,
# solo atajar typos evidentes antes de llamar al sistema académico.
_RE_CORREO = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _correo_valido(correo: str) -> bool:
    return bool(_RE_CORREO.match(correo or ""))


# ── Sistema académico (crear cuenta de estudiante) ──────────────────────────
#
# Conecta con el Sistema Académico USPG (proyecto aparte: uspg-sistema-academico),
# vía su endpoint POST /api/integrations/whatsapp/solicitudes-inscripcion.
#
# El sistema académico, no esta funcion, decide si la solicitud termina en una cuenta
# real o en revision manual (por ejemplo, si la carrera todavia no tiene un plan
# curricular cargado). Aca solo se llama al endpoint y se traduce la respuesta.
#
# El telefono SIEMPRE lo pasa main.py desde el remitente real del mensaje de
# WhatsApp — nunca se le pide al modelo que lo escriba, para que no pueda
# equivocarse ni inventarlo.


async def crear_solicitud_inscripcion(
    telefono: str, nombre: str, carrera: str, correo_personal: str = ""
) -> dict:
    """
    Registra a un aspirante en el Sistema Académico USPG a partir de la conversación
    de WhatsApp. Si la carrera tiene un plan curricular activo, crea la cuenta real
    (usuario + estudiante) de inmediato y devuelve el carné, el correo institucional
    generado y la contraseña temporal — el sistema le manda esos datos al correo
    PERSONAL del aspirante (el que dio en el chat, porque todavía no puede entrar a su
    correo institucional nuevo) y avisa al equipo de admisiones para que lo revise. Si
    la carrera todavía no está cargada en el sistema, la solicitud queda pendiente de
    revisión manual.

    `correo_personal` es un correo externo (Gmail, Outlook, etc.). Se valida el
    formato mínimo aquí: si no parece un correo, se devuelve status "error" sin
    llamar al sistema académico, para que el agente lo vuelva a pedir.
    """
    correo_personal = correo_personal.strip()
    if not _correo_valido(correo_personal):
        return {
            "status": "error",
            "message": (
                "El correo personal no tiene un formato válido. Pídele al aspirante "
                "que lo repita (ejemplo: nombre@gmail.com)."
            ),
        }
    base_url = (os.getenv("ACADEMIC_SYSTEM_URL") or "").rstrip("/")
    api_key = os.getenv("ACADEMIC_SYSTEM_API_KEY") or ""

    if not base_url or not api_key:
        logger.error("ACADEMIC_SYSTEM_URL o ACADEMIC_SYSTEM_API_KEY no configuradas")
        return {
            "status": "error",
            "message": "No se pudo conectar con el sistema académico en este momento.",
        }

    url = f"{base_url}/api/integrations/whatsapp/solicitudes-inscripcion"
    payload = {
        "name": nombre,
        "phone": telefono,
        "careerName": carrera,
        "personalEmail": correo_personal,
    }

    try:
        # 45s: crear la cuenta puede tardar (varias escrituras + notificaciones del
        # lado del sistema académico). El proveedor de WhatsApp ya recibió su 200, así
        # que este trabajo corre en segundo plano y puede darse ese margen.
        async with httpx.AsyncClient(timeout=45.0) as cliente:
            r = await cliente.post(url, json=payload, headers={"X-API-Key": api_key})
    except httpx.HTTPError as e:
        # Solo el tipo de error (ReadTimeout, ConnectError...). Ni el mensaje crudo ni
        # el objeto de la peticion, que podrian arrastrar la URL o el payload.
        logger.error(f"Error de red hablando con el sistema académico: {type(e).__name__}")
        return {
            "status": "error",
            "message": "No se pudo conectar con el sistema académico en este momento.",
        }

    try:
        cuerpo = r.json()
    except ValueError:
        cuerpo = {}

    if r.status_code >= 500:
        # La respuesta puede incluir datos del aspirante; no se registra su cuerpo.
        logger.error(f"El sistema académico respondió HTTP {r.status_code}")
        return {
            "status": "error",
            "message": "El sistema académico tuvo un problema técnico al crear la cuenta.",
        }

    # 200/201 (creada), 202 (pendiente), 409 (ya existe): respuestas validas del
    # negocio — se devuelven tal cual para que el agente redacte con esa info.
    if r.status_code in (200, 201, 202, 409):
        return cuerpo or {"status": "error", "message": "Respuesta vacía del sistema académico."}

    # Cualquier otro codigo (401 API key, 400 datos, 403, 404, 429...) es un problema
    # de configuracion o de la peticion, no del aspirante. Se loguea SOLO el campo de
    # error del cuerpo (mensajes cortos y sin PII); nunca el cuerpo crudo, que segun el
    # endpoint podria traer datos del aspirante.
    detalle = ""
    if isinstance(cuerpo, dict):
        detalle = str(cuerpo.get("message") or cuerpo.get("error") or "")[:200]
    logger.error(
        f"El sistema académico rechazó la solicitud [HTTP {r.status_code}]"
        + (f": {detalle}" if detalle else " (sin mensaje de error en el cuerpo)")
    )
    return {
        "status": "error",
        "message": "No se pudo completar el registro automático en este momento.",
    }
