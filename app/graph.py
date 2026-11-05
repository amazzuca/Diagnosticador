"""Grafo LangGraph del bot diagnosticador de automatización.

Flujo por cada mensaje del usuario:

    START ─► (¿ya terminó?) ─► after ─► END
                  │
                  └► extract ─► (¿faltan datos?) ─► ask ──► END
                                      │
                                      └► report ─► END   (genera diagnóstico + webhook n8n)

Webhook a n8n (campo "evento"):
  - "contacto_capturado": apenas el usuario deja mail o teléfono (no se pierde el lead si abandona)
  - "diagnostico": al terminar la entrevista, con el informe completo
El estado se persiste por thread_id con un checkpointer (memoria o Postgres).
"""
import json
import logging
import os
import re
from typing import Annotated, Literal, Optional, TypedDict

import httpx
from langchain.chat_models import init_chat_model
from langchain_core.messages import AIMessage, SystemMessage
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from pydantic import BaseModel, Field, create_model

logger = logging.getLogger("diagnosticador")

MODEL = os.getenv("MODEL", "openai:gpt-4o-mini")
MAX_TURNS = int(os.getenv("MAX_TURNS", "20"))
N8N_WEBHOOK_URL = os.getenv("N8N_WEBHOOK_URL", "")
N8N_WEBHOOK_TOKEN = os.getenv("N8N_WEBHOOK_TOKEN", "")
AGENCY_NAME = os.getenv("AGENCY_NAME", "nuestra agencia")

llm = init_chat_model(MODEL, temperature=0.3, timeout=60, max_retries=3)

# ---------------------------------------------------------------------------
# Campos que el bot necesita relevar (orden = prioridad de preguntas).
# El contacto va 3º a propósito: si el usuario abandona, el lead ya está capturado.
# ---------------------------------------------------------------------------
FIELDS = [
    ("empresa", "Nombre de la empresa, rubro y tamaño (cantidad de empleados)"),
    ("proceso", "Qué proceso quiere automatizar y cuál es el objetivo"),
    ("contacto", "Nombre y cargo de la persona de contacto, junto con su mail y su teléfono/WhatsApp"),
    ("pasos", "Pasos actuales del proceso, de principio a fin, y quién hace cada uno"),
    ("disparador_frecuencia", "Qué dispara el proceso y con qué frecuencia/volumen (por día, semana o mes)"),
    ("tiempo_costo", "Tiempo que toma cada ejecución, cuántas personas intervienen y costo aproximado"),
    ("sistemas", "Sistemas y herramientas involucrados (ERP, Excel, mail, WhatsApp, CRM, etc.)"),
    ("datos_entrada", "Tipo y formato de los datos de entrada (planillas, PDFs, mails, formularios, imágenes) y su calidad"),
    ("reglas_excepciones", "Reglas de decisión, aprobaciones y excepciones frecuentes"),
    ("dolor", "Principales problemas hoy: errores, demoras, retrabajo, impacto en el negocio"),
    ("accesos_apis", "Si los sistemas tienen API, exportaciones o acceso a base de datos; quién administra los accesos"),
    ("datos_sensibles", "Datos sensibles o requisitos de seguridad/cumplimiento"),
    ("exito_plazo", "Cómo medirían el éxito (KPI), plazo y presupuesto estimado"),
]
TOTAL = len(FIELDS)

Extraction = create_model(
    "Extraction",
    quiere_cerrar=(
        bool,
        Field(default=False, description="True solo si el usuario pide explícitamente terminar o recibir ya el diagnóstico"),
    ),
    email=(Optional[str], Field(default=None, description="Email de contacto, copiado tal como lo escribió el usuario")),
    telefono=(Optional[str], Field(default=None, description="Teléfono o WhatsApp de contacto, copiado tal como lo escribió el usuario")),
    **{k: (Optional[str], Field(default=None, description=d)) for k, d in FIELDS},
)


class Diagnostico(BaseModel):
    resumen_cliente: str = Field(description="Diagnóstico preliminar en markdown para mostrar al cliente (sin precios)")
    informe_interno: str = Field(description="Informe interno en markdown para la agencia")
    score_viabilidad: int = Field(ge=1, le=10, description="Viabilidad global de automatizar (1-10)")
    complejidad: Literal["baja", "media", "alta"]
    herramienta_recomendada: Literal["n8n", "Power Automate", "Zapier", "Python a medida", "Combinada"]
    ahorro_horas_mes: Optional[float] = Field(default=None, description="Horas/mes ahorradas, solo si se puede calcular con los datos dados")
    calidad_lead: Literal["alta", "media", "baja"]


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------
EXTRACT_SYSTEM = """Sos un analista que extrae información de una entrevista de diagnóstico de automatización.
Leé la conversación y completá SOLO los campos sobre los que el usuario aportó información.

Reglas:
- No inventes ni infieras datos que el usuario no dijo.
- email y telefono: copialos exactamente como los escribió el usuario. Si no los dio, null.
- Si un campo ya tiene valor y el usuario agrega detalle, devolvé el valor combinado y actualizado.
- Si el usuario dice que no sabe o prefiere no responder sobre algo que se le preguntó, poné "No informado" en ese campo.
- Dejá en null lo que no se mencionó.

Datos ya relevados:
{data}"""

ASK_SYSTEM = """Sos el asistente de diagnóstico de {agency}, una consultora de automatización de procesos para pymes.
Conducís una entrevista breve para entender un proceso que la empresa quiere automatizar.

Estilo: español rioplatense, cálido, profesional y conciso.

Reglas:
- Hacé UNA pregunta por turno (máximo dos si están estrechamente relacionadas).
- Si el usuario aportó información, reconocelo en una frase corta antes de preguntar.
- Si es el primer mensaje, presentate en una línea, aclará que son unos 10 minutos de preguntas y empezá.
- Si la respuesta anterior fue vaga, pedí un ejemplo concreto o un número aproximado.
- Aceptá "no sé" como respuesta válida y seguí adelante.
- Si el tema es el contacto, explicá en una frase que es para enviarle el diagnóstico y que un consultor lo contacte; pedí nombre, mail y WhatsApp o teléfono.
- NO des recomendaciones ni soluciones todavía. NO menciones campos internos ni listas.

Tema a cubrir ahora: {next_topic}
Datos ya relevados: {data}
Progreso: {filled}/{total}"""

REPORT_SYSTEM = f"""Sos un consultor senior de automatización de {AGENCY_NAME}. Con los datos de la entrevista, generá un diagnóstico.

Criterios para recomendar herramienta:
- n8n: flujos complejos, APIs, self-hosting, integración de bases de datos, bajo costo por ejecución.
- Power Automate: el cliente vive en Microsoft 365 / SharePoint / Dataverse / Teams.
- Zapier: integraciones simples SaaS a SaaS, bajo volumen, el cliente quiere autonomía.
- Python a medida: scraping, ML/visión por computadora, procesamiento pesado de datos, lógica muy específica.
- Combinada: cuando ninguna sola cubre el caso. Sumá un LLM cuando haya documentos o texto no estructurado.

Reglas:
- No inventes cifras. Si estimás ahorro, calculalo SOLO con frecuencia × tiempo informados y explicitá los supuestos. Si no alcanza, ahorro_horas_mes = null.
- Si faltan datos críticos, decilo y listalo como "a validar".
- resumen_cliente (máx. ~350 palabras, markdown): qué entendimos del proceso, viabilidad, enfoque recomendado en lenguaje simple, beneficios esperados, prerrequisitos o riesgos, próximos pasos. Sin precios ni jerga innecesaria.
- informe_interno (markdown): resumen del lead, proceso AS-IS, arquitectura propuesta, stack, complejidad y por qué, riesgos, dudas pendientes, oportunidades de ampliar el alcance, esquema sugerido de propuesta comercial.
- calidad_lead: considerá presupuesto/plazo, dolor real, volumen y decisor de contacto."""


# ---------------------------------------------------------------------------
# Estado y utilidades
# ---------------------------------------------------------------------------
class State(TypedDict, total=False):
    messages: Annotated[list, add_messages]
    data: dict
    turns: int
    close: bool
    done: bool
    lead_sent: bool


def _filled(data: dict, key: str) -> bool:
    """El contacto se considera completo si hay mail o teléfono (o si el usuario no quiso darlo)."""
    if key == "contacto":
        return bool(data.get("email") or data.get("telefono") or str(data.get("contacto", "")).lower() == "no informado")
    return bool(data.get(key))


def _missing(data: dict):
    return [(k, d) for k, d in FIELDS if not _filled(data, k)]


EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
PHONE_RE = re.compile(r"\+?\d[\d\s().-]{6,}\d")
PHONE_HINT = re.compile(r"tel|cel|whats|wsp|wpp|m[oó]vil|llam", re.I)


def _regex_contact(state: State, data: dict):
    """Respaldo determinista: no dependemos solo del LLM para capturar mail y teléfono."""
    text = state["messages"][-1].content
    if not isinstance(text, str):
        return
    m = EMAIL_RE.search(text)
    if m:
        data["email"] = m.group(0)
    if not data.get("telefono"):
        asking_contact = any(k == "contacto" for k, _ in _missing(data)[:2])
        for m in PHONE_RE.finditer(text):
            raw = m.group(0).strip()
            digits = re.sub(r"\D", "", raw)
            if 8 <= len(digits) <= 15 and (asking_contact or raw.startswith("+") or PHONE_HINT.search(text)):
                data["telefono"] = raw
                break


def _post_webhook(payload: dict):
    """Envía a n8n. Nunca rompe la conversación, pero SIEMPRE deja rastro en los logs."""
    if not N8N_WEBHOOK_URL:
        logger.warning("N8N_WEBHOOK_URL no está configurada: no se envía evento=%s", payload.get("evento"))
        return
    headers = {"X-Auth-Token": N8N_WEBHOOK_TOKEN} if N8N_WEBHOOK_TOKEN else {}
    try:
        r = httpx.post(N8N_WEBHOOK_URL, json=payload, headers=headers, timeout=10)
        r.raise_for_status()
        logger.info("Webhook n8n OK evento=%s status=%s", payload.get("evento"), r.status_code)
    except httpx.HTTPStatusError as exc:
        logger.error(
            "Webhook n8n FALLÓ evento=%s status=%s body=%s",
            payload.get("evento"), exc.response.status_code, exc.response.text[:200],
        )
    except Exception as exc:
        logger.error("Webhook n8n FALLÓ evento=%s error=%s", payload.get("evento"), exc)


def _base_payload(evento: str, thread_id, data: dict) -> dict:
    return {
        "evento": evento,
        "thread_id": thread_id,
        "email": data.get("email"),
        "telefono": data.get("telefono"),
        "datos": data,
    }


# ---------------------------------------------------------------------------
# Nodos
# ---------------------------------------------------------------------------
def extract(state: State, config):
    data = dict(state.get("data") or {})
    system = SystemMessage(EXTRACT_SYSTEM.format(data=json.dumps(data, ensure_ascii=False)))
    result = llm.with_structured_output(Extraction).invoke([system] + state["messages"][-8:])
    update = result.model_dump()
    close = bool(update.pop("quiere_cerrar", False))
    for k, v in update.items():
        if v and str(v).strip():
            data[k] = str(v).strip()
    _regex_contact(state, data)

    out = {"data": data, "close": close, "turns": state.get("turns", 0) + 1}

    # Lead capturado: avisamos a n8n ya, sin esperar al final de la entrevista.
    if (data.get("email") or data.get("telefono")) and not state.get("lead_sent"):
        _post_webhook(_base_payload("contacto_capturado", config["configurable"].get("thread_id"), data))
        out["lead_sent"] = True
    return out


def route_after_extract(state: State) -> Literal["ask", "report"]:
    if state.get("close") or not _missing(state.get("data", {})) or state.get("turns", 0) >= MAX_TURNS:
        return "report"
    return "ask"


def ask(state: State):
    data = state.get("data", {})
    missing = _missing(data)
    next_topic = " / ".join(d for _, d in missing[:2])
    system = SystemMessage(
        ASK_SYSTEM.format(
            agency=AGENCY_NAME,
            next_topic=next_topic,
            data=json.dumps(data, ensure_ascii=False),
            filled=TOTAL - len(missing),
            total=TOTAL,
        )
    )
    reply = llm.invoke([system] + state["messages"][-8:])
    return {"messages": [reply]}


def report(state: State, config):
    data = state.get("data", {})
    system = SystemMessage(REPORT_SYSTEM)
    brief = SystemMessage("Datos relevados en la entrevista:\n" + json.dumps(data, ensure_ascii=False, indent=2))
    diag: Diagnostico = llm.with_structured_output(Diagnostico).invoke([system, brief])

    text = diag.resumen_cliente
    if data.get("email") or data.get("telefono"):
        text += f"\n\n---\nUn consultor de {AGENCY_NAME} te va a contactar para coordinar los próximos pasos. ¡Gracias por tu tiempo!"
    else:
        text += f"\n\n---\nSi querés avanzar, escribinos y coordinamos una reunión con {AGENCY_NAME}. ¡Gracias por tu tiempo!"

    payload = _base_payload("diagnostico", config["configurable"].get("thread_id"), data)
    payload.update(diag.model_dump())
    _post_webhook(payload)
    return {"messages": [AIMessage(content=text)], "done": True}


def after(state: State):
    msg = "El diagnóstico ya fue generado. Si querés analizar otro proceso, recargá la página para empezar una nueva entrevista."
    return {"messages": [AIMessage(content=msg)]}


def gate(state: State) -> Literal["after", "extract"]:
    return "after" if state.get("done") else "extract"


def progress(state: dict) -> float:
    data = state.get("data") or {}
    return round(sum(1 for k, _ in FIELDS if _filled(data, k)) / TOTAL, 2)


# ---------------------------------------------------------------------------
# Construcción
# ---------------------------------------------------------------------------
def make_checkpointer():
    url = os.getenv("DATABASE_URL")
    if url:
        from langgraph.checkpoint.postgres import PostgresSaver
        from psycopg_pool import ConnectionPool

        pool = ConnectionPool(
            conninfo=url,
            max_size=5,
            kwargs={"autocommit": True, "prepare_threshold": 0},
            check=ConnectionPool.check_connection,
            open=True,
        )
        saver = PostgresSaver(pool)
        saver.setup()
        return saver
    from langgraph.checkpoint.memory import MemorySaver

    return MemorySaver()


def build_graph():
    g = StateGraph(State)
    g.add_node("extract", extract)
    g.add_node("ask", ask)
    g.add_node("report", report)
    g.add_node("after", after)
    g.add_conditional_edges(START, gate)
    g.add_conditional_edges("extract", route_after_extract)
    g.add_edge("ask", END)
    g.add_edge("report", END)
    g.add_edge("after", END)
    return g.compile(checkpointer=make_checkpointer())
