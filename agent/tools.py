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
from pathlib import Path

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
