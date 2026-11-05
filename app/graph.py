"""Grafo LangGraph del bot diagnosticador de automatización.

Flujo por cada mensaje del usuario:

    START ─► (¿ya terminó?) ─► after ─► END
                  │
                  └► extract ─► (¿faltan datos?) ─► ask ──► END
                                      │
                                      └► report ─► END   (genera diagnóstico + webhook n8n)

El estado se persiste por thread_id con un checkpointer (memoria o Postgres).
"""
import json
import os
from typing import Annotated, Literal, Optional, TypedDict

import httpx
from langchain.chat_models import init_chat_model
from langchain_core.messages import AIMessage, SystemMessage
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from pydantic import BaseModel, Field, create_model

MODEL = os.getenv("MODEL", "openai:gpt-4o-mini")
MAX_TURNS = int(os.getenv("MAX_TURNS", "20"))
N8N_WEBHOOK_URL = os.getenv("N8N_WEBHOOK_URL", "")
AGENCY_NAME = os.getenv("AGENCY_NAME", "nuestra agencia")

llm = init_chat_model(MODEL, temperature=0.3)

# ---------------------------------------------------------------------------
# Campos que el bot necesita relevar (orden = prioridad de preguntas)
# ---------------------------------------------------------------------------
FIELDS = [
    ("empresa", "Nombre de la empresa, rubro y tamaño (cantidad de empleados)"),
    ("proceso", "Qué proceso quiere automatizar y cuál es el objetivo"),
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
    ("contacto", "Nombre, cargo y mail o WhatsApp de contacto"),
]
TOTAL = len(FIELDS)

Extraction = create_model(
    "Extraction",
    quiere_cerrar=(
        bool,
        Field(default=False, description="True solo si el usuario pide explícitamente terminar o recibir ya el diagnóstico"),
    ),
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
# Estado y nodos
# ---------------------------------------------------------------------------
class State(TypedDict, total=False):
    messages: Annotated[list, add_messages]
    data: dict
    turns: int
    close: bool
    done: bool


def _missing(data: dict):
    return [(k, d) for k, d in FIELDS if not data.get(k)]


def extract(state: State):
    data = dict(state.get("data") or {})
    system = SystemMessage(EXTRACT_SYSTEM.format(data=json.dumps(data, ensure_ascii=False)))
    result = llm.with_structured_output(Extraction).invoke([system] + state["messages"][-8:])
    update = result.model_dump()
    close = bool(update.pop("quiere_cerrar", False))
    for k, v in update.items():
        if v and str(v).strip():
            data[k] = str(v).strip()
    return {"data": data, "close": close, "turns": state.get("turns", 0) + 1}


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
    if data.get("contacto") and data["contacto"].lower() != "no informado":
        text += f"\n\n---\nUn consultor de {AGENCY_NAME} te va a contactar para coordinar los próximos pasos. ¡Gracias por tu tiempo!"
    else:
        text += f"\n\n---\nSi querés avanzar, escribinos y coordinamos una reunión con {AGENCY_NAME}. ¡Gracias por tu tiempo!"

    _notify(config["configurable"].get("thread_id"), data, diag)
    return {"messages": [AIMessage(content=text)], "done": True}


def _notify(thread_id, data: dict, diag: Diagnostico):
    """Envía el informe interno a un webhook de n8n (falla en silencio)."""
    if not N8N_WEBHOOK_URL:
        return
    payload = {"thread_id": thread_id, "datos": data, **diag.model_dump()}
    headers = {"X-Auth-Token": N8N_WEBHOOK_TOKEN} if N8N_WEBHOOK_TOKEN else {}
    try:
        httpx.post(N8N_WEBHOOK_URL, json=payload, headers=headers, timeout=10)
    except Exception as exc:  # no romper la conversación por un webhook caído
        print(f"[notify] webhook error: {exc}")


def after(state: State):
    msg = "El diagnóstico ya fue generado. Si querés analizar otro proceso, recargá la página para empezar una nueva entrevista."
    return {"messages": [AIMessage(content=msg)]}


def gate(state: State) -> Literal["after", "extract"]:
    return "after" if state.get("done") else "extract"


def progress(state: dict) -> float:
    data = state.get("data") or {}
    return round(sum(1 for k, _ in FIELDS if data.get(k)) / TOTAL, 2)


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
