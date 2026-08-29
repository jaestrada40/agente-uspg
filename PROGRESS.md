# Progreso — Asistente USPG (AgentKit)

Estado al 2026-08-29. Este archivo resume dónde quedamos para retomar sin repetir pasos.

## Hecho

- Repo de AgentKit (Hainrixz/whatsapp-agentkit) clonado en esta carpeta.
- Python 3.11.15 instalado con `uv` (la máquina tenía 3.10) + entorno virtual en `.venv`.
- Dependencias instaladas en `.venv` (FastAPI, SQLAlchemy async, etc.).
- Estructura de carpetas creada: `agent/`, `agent/providers/`, `config/`, `knowledge/`, `tests/`.
- Entrevista de negocio (Fase 2) completada:
  - Negocio: Universidad San Pablo de Guatemala (USPG)
  - Carreras y posgrados guardados en `knowledge/carreras_y_posgrados.md`
  - Casos de uso: FAQ, agendar citas de orientación vocacional, calificar/dar seguimiento
    a leads, guiar inscripción de aspirantes, seguimiento a los que no se inscriben,
    soporte a estudiantes ya inscritos
  - Nombre del agente: "Asistente USPG"
  - Tono: Empático y cálido
  - Horario: Lunes a Viernes 9am–6pm, Sábados 10am–2pm
  - Proveedor de WhatsApp elegido: **Zernio** (API key y webhook secret ya guardados en `.env`)
- Fase 3 (generación de código) completada, con una variación importante:
  - El usuario pidió usar **Gemini en vez de Anthropic** para probar sin costo (tiene
    Claude Pro pero no incluye créditos de API).
  - Se generó `agent/brain.py` adaptado a Gemini (google-genai SDK), manteniendo la
    misma interfaz (`generar_respuesta`, `obtener_mensaje_error`, etc.) para que
    `main.py` no tuviera que cambiar.
  - Se generaron también: `config/business.yaml`, `config/prompts.yaml` (system prompt
    completo con las carreras/posgrados incorporados), `agent/providers/base.py`,
    `agent/providers/__init__.py`, `agent/providers/zernio.py`, `agent/main.py`,
    `agent/memory.py` (con tablas extra `Lead` y `Cita` para el seguimiento de
    aspirantes y las citas de orientación vocacional), `agent/tools.py`, `tests/test_local.py`,
    `Dockerfile`, `docker-compose.yml`, `.dockerignore`.

## Problema encontrado — IMPORTANTE

Al probar `generar_respuesta()` con Gemini, la llamada falló con `403 Forbidden` /
`httpx.ProxyError`. Se confirmó que **el entorno de Cowork (tanto el contenedor en la
nube como esta VM local de device_bash) tiene una lista blanca de red que SOLO permite
`api.anthropic.com` y algunos registros de paquetes — NO permite los dominios de Google
(`generativelanguage.googleapis.com`, `aistudio.google.com`)**. No es un problema de la
API key ni del código: es una restricción de infraestructura de este entorno de trabajo.

Nota: si el agente se despliega a Railway más adelante, ahí sí habría internet abierto y
Gemini probablemente funcionaría — pero no se puede verificar desde aquí.

## Decisión

El usuario decidió **volver a usar Anthropic (Claude)** en vez de Gemini, específicamente
el modelo **`claude-haiku-4-5`** (el más barato, ~$1/$5 por millón de tokens) para poder
probar todo de verdad en este entorno.

## Siguiente paso pendiente (justo donde nos quedamos)

1. Pedirle al usuario su **Anthropic API Key** (sk-ant-..., se saca en
   platform.anthropic.com → Settings → API Keys). Esto es API/Console, DISTINTO de su
   suscripción Claude Pro — no incluye créditos, hay que cargar tarjeta (aunque sea con
   poco saldo, ~$5).
2. Reescribir `agent/brain.py` para usar el SDK de Anthropic (`AsyncAnthropic`) en vez
   de Gemini — el template original ya está documentado en `CLAUDE.md` sección 3.8 de
   este mismo repo.
3. Actualizar `requirements.txt`: agregar `anthropic>=0.122.0`, quitar `google-genai`
   (o dejarlo, no estorba).
4. Actualizar `.env`: quitar `GEMINI_API_KEY`/`GEMINI_MODEL`/`GEMINI_MAX_TOKENS`, agregar
   `ANTHROPIC_API_KEY`, `ANTHROPIC_MODEL=claude-haiku-4-5`, `ANTHROPIC_EFFORT=low`.
5. Reinstalar dependencias en `.venv` (`uv pip install -r requirements.txt --python .venv`).
6. Repetir la prueba de humo: levantar el servidor (`uvicorn agent.main:app`), pegarle a
   `GET /` y confirmar `status: ok`; luego probar `generar_respuesta()` con un mensaje real.
7. Fase 4 completa: correr `tests/test_local.py` de forma interactiva con el usuario.
8. Fase 5 (opcional, solo si el usuario quiere): guía de deploy a Railway + configurar
   webhook de Zernio con la URL pública.

## Ya guardado en `.env` (no hace falta volver a pedirlo)

- `WHATSAPP_PROVIDER=zernio`
- `ZERNIO_API_KEY` (ya guardada)
- `ZERNIO_WEBHOOK_SECRET=asistente-uspg-2026`
- `GEMINI_API_KEY` (ya guardada, pero quedará sin uso tras el cambio a Anthropic — se
  puede dejar o borrar, no hace daño)

## Pendiente de cargar más adelante (el usuario lo dijo explícitamente)

- Requisitos de inscripción por carrera, precios, becas y fechas del proceso de admisión
  (la página web de la universidad no estaba funcionando al momento de la entrevista).
  Cuando el usuario los tenga, van a `knowledge/` y se incorporan a `config/prompts.yaml`.
