# agent/main.py — Servidor FastAPI + Webhook de WhatsApp
# Generado por AgentKit

"""
Servidor principal del agente.
Funciona con cualquier proveedor (Zernio, Meta) gracias a la capa de providers.
"""

import asyncio
import logging
import os
from collections import defaultdict, deque
from contextlib import asynccontextmanager, suppress
from time import monotonic

from dotenv import load_dotenv
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.responses import PlainTextResponse

from agent.brain import generar_respuesta, obtener_mensaje_error
from agent.memory import (
    guardar_mensaje,
    inicializar_db,
    liberar_evento,
    limpiar_datos_personales_viejos,
    limpiar_eventos_viejos,
    marcar_evento_procesado,
    obtener_historial,
)
from agent.providers import obtener_proveedor
from agent.providers.base import MensajeEntrante
from agent.security import ocultar_telefono

load_dotenv()

ENVIRONMENT = os.getenv("ENVIRONMENT", "development")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("agentkit")
# En desarrollo queremos el detalle de NUESTRO agente, no el de las librerias.
# Poner el nivel raiz en DEBUG llena la terminal de ruido de aiosqlite y httpx
# y hace imposible leer lo que hizo el agente.
logger.setLevel(logging.DEBUG if ENVIRONMENT == "development" else logging.INFO)

PORT = int(os.getenv("PORT", "8000"))
MAX_WEBHOOK_BODY_BYTES = int(os.getenv("MAX_WEBHOOK_BODY_BYTES") or "262144")
MAX_WEBHOOKS_PER_MINUTE = int(os.getenv("MAX_WEBHOOKS_PER_MINUTE") or "60")
MAX_MENSAJES_POR_WEBHOOK = int(os.getenv("MAX_MENSAJES_POR_WEBHOOK") or "10")
MAX_PROCESAMIENTOS_CONCURRENTES = int(os.getenv("MAX_PROCESAMIENTOS_CONCURRENTES") or "20")
RETENCION_DATOS_DIAS = int(os.getenv("DATA_RETENTION_DAYS") or "90")

# Un candado por numero de telefono. En WhatsApp es normal que alguien mande "hola" y
# medio segundo despues la pregunta de verdad: sin esto los dos mensajes se procesarian
# en paralelo, los dos leerian el mismo historial y las escrituras quedarian intercaladas.
_candados: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
_procesamientos = asyncio.Semaphore(MAX_PROCESAMIENTOS_CONCURRENTES)
_solicitudes_por_ip: dict[str, deque[float]] = defaultdict(deque)


def _permitir_webhook(ip: str) -> bool:
    """Rate limit en memoria por IP; el proxy debe reenviar la IP real de forma confiable."""
    ahora = monotonic()
    ventana = _solicitudes_por_ip[ip]
    while ventana and ventana[0] <= ahora - 60:
        ventana.popleft()
    if len(ventana) >= MAX_WEBHOOKS_PER_MINUTE:
        return False
    ventana.append(ahora)
    return True


async def _limpiar_datos_periodicamente():
    """Ejecuta la retencion a diario sin depender de un redespliegue."""
    while True:
        await asyncio.sleep(24 * 60 * 60)
        eliminados = await limpiar_datos_personales_viejos(RETENCION_DATOS_DIAS)
        if any(eliminados.values()):
            logger.info("Se eliminaron datos personales que excedian la retencion configurada")

# Si la configuracion esta mal, guardamos el error y lo mostramos en el health check,
# en vez de reventar en el import y dejar a Railway reiniciando el contenedor a ciegas.
proveedor = None
error_configuracion: str | None = None
try:
    proveedor = obtener_proveedor()
except Exception as e:  # noqa: BLE001 — cualquier problema de configuracion
    error_configuracion = str(e)

# Resultado del chequeo de credenciales que se hace al arrancar. Se expone en el health
# check: que el servidor conteste no significa que el agente pueda responder por WhatsApp.
estado_proveedor: dict = {"ok": None, "detalle": "sin verificar"}


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Prepara la base de datos y chequea el proveedor al arrancar."""
    await inicializar_db()
    await limpiar_eventos_viejos()
    eliminados = await limpiar_datos_personales_viejos(RETENCION_DATOS_DIAS)
    logger.info("Base de datos lista")
    if any(eliminados.values()):
        logger.info("Se eliminaron datos personales que excedian la retencion configurada")
    logger.info(f"Servidor AgentKit escuchando en el puerto {PORT}")

    global estado_proveedor
    if proveedor is not None:
        logger.info(f"Proveedor de WhatsApp: {proveedor.__class__.__name__}")
        ok, detalle = await proveedor.verificar_conexion()
        estado_proveedor = {"ok": ok, "detalle": detalle}
        logger.info(f"Conexion con el proveedor: {'OK' if ok else 'ERROR'} — {detalle}")
    else:
        logger.error(f"Proveedor de WhatsApp NO configurado: {error_configuracion}")

    tarea_limpieza = asyncio.create_task(_limpiar_datos_periodicamente())
    try:
        yield
    finally:
        tarea_limpieza.cancel()
        with suppress(asyncio.CancelledError):
            await tarea_limpieza


app = FastAPI(title="AgentKit — WhatsApp AI Agent", version="2.0.0", lifespan=lifespan)


@app.get("/")
async def health_check():
    """Endpoint de salud para Railway y monitoreo."""
    if error_configuracion:
        if ENVIRONMENT == "production":
            return {"status": "error", "service": "agentkit"}
        return {"status": "error", "service": "agentkit", "detalle": error_configuracion}

    # Se responde 200 aunque las credenciales esten mal, para que Railway no marque el
    # deploy como caido y puedas leer el diagnostico. El detalle esta en el cuerpo.
    respuesta = {
        "status": "ok" if estado_proveedor["ok"] else "degradado",
        "service": "agentkit",
    }
    if ENVIRONMENT != "production":
        respuesta.update({
            "proveedor": proveedor.__class__.__name__ if proveedor else None,
            "conexion": estado_proveedor,
        })
    return respuesta


@app.get("/webhook")
async def webhook_verificacion(request: Request):
    """Verificacion GET del webhook. La pide Meta; para Zernio no hace nada."""
    if proveedor is None:
        raise HTTPException(status_code=503, detail=error_configuracion or "Proveedor no configurado")

    respuesta = await proveedor.validar_webhook(request)
    if respuesta is not None:
        return PlainTextResponse(respuesta)

    # Meta pide un 403 cuando manda hub.mode=subscribe y el verify_token no coincide.
    # Devolverle 200 le hace creer que la URL quedo verificada cuando no es cierto.
    if request.query_params.get("hub.mode") == "subscribe":
        raise HTTPException(status_code=403, detail="Verify token incorrecto")

    return {"status": "ok"}


@app.post("/webhook")
async def webhook_handler(request: Request, tareas: BackgroundTasks):
    """
    Recibe los mensajes de WhatsApp.

    Contesta 200 de inmediato y procesa el mensaje en segundo plano.

    Esto NO es un detalle de estilo. Los proveedores esperan un 2xx en unos 5 segundos y,
    si no lo reciben, reintentan el mismo evento hasta 7 veces. Como llamar a la IA tarda
    mas que eso, procesar antes de contestar hace que el cliente reciba la misma respuesta
    repetida. Por eso: responder primero, trabajar despues.
    """
    if proveedor is None:
        raise HTTPException(status_code=503, detail=error_configuracion or "Proveedor no configurado")

    ip = request.client.host if request.client else "desconocida"
    if not _permitir_webhook(ip):
        raise HTTPException(status_code=429, detail="Demasiadas solicitudes")

    content_length = request.headers.get("content-length")
    if content_length:
        try:
            tamanio_declarado = int(content_length)
        except ValueError:
            raise HTTPException(status_code=400, detail="Content-Length invalido") from None
        if tamanio_declarado < 0 or tamanio_declarado > MAX_WEBHOOK_BODY_BYTES:
            raise HTTPException(status_code=413, detail="Webhook demasiado grande")

    # request.body() queda cacheado por Starlette, por lo que la verificacion HMAC y
    # el parser posterior leen exactamente los mismos bytes sin volver a consumirlos.
    cuerpo = await request.body()
    if len(cuerpo) > MAX_WEBHOOK_BODY_BYTES:
        raise HTTPException(status_code=413, detail="Webhook demasiado grande")

    if not await proveedor.verificar_firma(request):
        raise HTTPException(status_code=401, detail="Firma del webhook invalida")

    try:
        mensajes = await proveedor.parsear_webhook(request)
    except Exception:  # noqa: BLE001
        # Un payload raro no debe hacer que el proveedor reintente para siempre
        logger.exception("No se pudo leer el webhook")
        return {"status": "ignorado"}

    if len(mensajes) > MAX_MENSAJES_POR_WEBHOOK:
        logger.warning("Webhook rechazo: excede el limite de mensajes por entrega")
        raise HTTPException(status_code=413, detail="Demasiados mensajes en el webhook")

    encolados = 0
    for msg in mensajes:
        if msg.es_propio or not msg.texto.strip():
            continue

        # La entrega es "al menos una vez": el mismo evento puede llegar dos veces
        evento_id = msg.contexto.get("evento_id") or msg.mensaje_id
        if evento_id and not await marcar_evento_procesado(evento_id):
            logger.info(f"Evento repetido, se ignora: {evento_id}")
            continue

        logger.info(f"Mensaje recibido de {ocultar_telefono(msg.telefono)}")
        tareas.add_task(procesar_mensaje, msg)
        encolados += 1

    return {"status": "ok", "encolados": encolados}


async def procesar_mensaje(msg: MensajeEntrante):
    """
    Genera la respuesta y la manda de vuelta. Corre fuera del ciclo del webhook.

    Se toma un candado por telefono: dos mensajes seguidos del mismo cliente se
    atienden en orden, no en paralelo, para que el historial no se mezcle.
    """
    evento_id = msg.contexto.get("evento_id") or msg.mensaje_id

    async with _candados[msg.telefono]:
        async with _procesamientos:
            try:
                # El historial se lee ANTES de guardar el mensaje actual: brain.py agrega
                # el mensaje nuevo al final, y asi no queda duplicado.
                historial = await obtener_historial(msg.telefono)
                respuesta, es_respuesta_real = await generar_respuesta(msg.texto, historial, msg.telefono)

                enviado = await proveedor.enviar_mensaje(msg.telefono, respuesta, msg.contexto)

                if not enviado:
                    # El evento se marco como procesado ANTES de llegar hasta aca, para que dos
                    # entregas simultaneas no se dupliquen. Si el envio fallo, hay que soltarlo:
                    # si no, el reintento del proveedor se descartaria por duplicado y el cliente
                    # se quedaria sin respuesta para siempre.
                    logger.error(f"No se pudo enviar la respuesta a {ocultar_telefono(msg.telefono)}; se libera el evento")
                    await liberar_evento(evento_id)
                    return

                # Solo se guarda en el historial lo que de verdad es conversacion. Los avisos
                # tecnicos ("estoy teniendo problemas") no son un turno del agente: guardarlos
                # los deja contaminando el contexto de todos los mensajes que vengan despues.
                if es_respuesta_real:
                    await guardar_mensaje(msg.telefono, "user", msg.texto)
                    await guardar_mensaje(msg.telefono, "assistant", respuesta)

                logger.info(f"Respuesta enviada a {ocultar_telefono(msg.telefono)}")

            except Exception:  # noqa: BLE001
                logger.exception(f"Error procesando el mensaje de {ocultar_telefono(msg.telefono)}")
                await liberar_evento(evento_id)
                try:
                    await proveedor.enviar_mensaje(msg.telefono, obtener_mensaje_error(), msg.contexto)
                except Exception:  # noqa: BLE001
                    logger.error("Tampoco se pudo avisarle al cliente del error")
