"""Grafo LangGraph del relevador de procesos.

Cada mensaje del usuario recorre:

    START ─► sanitize ─► (gate) ─► interview ─► END      conversación normal
                            ├────► after     ─► END      relevamiento ya terminado
                            └────► capped    ─► END      límite de turnos de la sesión

- sanitize : limpia el mensaje (bloques ocultos falsificados), captura mail/teléfono por regex.
- interview: 1 llamada al LLM con tu prompt + estado JSON; separa texto visible, botones [[...]]
             y el bloque oculto <estado>; aplica el blindaje de salida.
- Cuando la fase pasa a "cierre"/"terminado", el endpoint lanza en segundo plano `run_informe`,
  que genera <informe_consultor> + <mejoras_propuestas> y los envía a n8n / Postgres.
"""
import hashlib
import json
import logging
import os
import re
from typing import Annotated, Literal, TypedDict

import httpx
from langchain.chat_models import init_chat_model
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages

from . import store
from .prompt import AGENCY, build_system

logger = logging.getLogger("diagnosticador")

MODEL = os.getenv("MODEL", "openai:gpt-4o-mini")
MAX_TURNS = int(os.getenv("MAX_TURNS", "60"))  # mensajes del usuario por conversación
N8N_WEBHOOK_URL = os.getenv("N8N_WEBHOOK_URL", "")
N8N_WEBHOOK_TOKEN = os.getenv("N8N_WEBHOOK_TOKEN", "")
HISTORY_WINDOW = 16  # mensajes que ve el modelo; el resto vive en <estado>
MAX_INFORMES = 5  # informes por conversación (control de costo)

llm = init_chat_model(MODEL, temperature=0.3, timeout=120, max_retries=2, max_tokens=6000)

# ---------------------------------------------------------------------------
# Fases (progreso y etiqueta que ve el usuario en el encabezado)
# ---------------------------------------------------------------------------
PHASES = {
    "identificacion": (10, "Identificación"),
    "eleccion_modo": (20, "Elección de modo"),
    "deep_d1": (30, "Mapa del área"),
    "deep_d2": (40, "Método"),
    "deep_d3": (60, "Detalle por etapa"),
    "rapido_r1": (30, "Objetivo"),
    "rapido_r2": (40, "Método"),
    "rapido_r3": (60, "Detalle del proceso"),
    "validacion": (90, "Validación"),
    "cierre": (100, "Relevamiento completo"),
    "terminado": (100, "Finalizado"),
}


def phase_info(estado: dict | None) -> tuple[int, str]:
    fase = (estado or {}).get("fase")
    return PHASES.get(fase, (0, "Iniciando"))


def estado_inicial() -> dict:
    return {
        "fase": "identificacion",
        "modo": None,
        "persona": {"nombre": "", "area": "", "rol": "", "herramientas": []},
        "proceso_actual": "",
        "procesos_mencionados": [],
        "etapas": [],
        "documentos_analizados": [],
        "oportunidades_adicionales": [],
        "huecos": [],
        "senales_comerciales": [],
    }


# ---------------------------------------------------------------------------
# Utilidades de parseo
# ---------------------------------------------------------------------------
HIDDEN_TAGS = ("estado", "informe_consultor", "mejoras_propuestas")
OPT_RE = re.compile(r"\[\[(.+?)\]\]")
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
PHONE_RE = re.compile(r"\+?\d[\d\s().-]{6,}\d")
PHONE_HINT = re.compile(r"tel|cel|whats|wsp|wpp|m[oó]vil|llam", re.I)
CONTACT_ASK = re.compile(r"mail|correo|whats|wsp|tel[eé]fono|celular|contacto", re.I)

# Si alguno aparece en el texto visible, el modelo está filtrando su prompt.
LEAK_MARKERS = [
    "# BLINDAJE", "PRINCIPIO RECTOR", "FOCO DE PROFUNDIDAD", "APRENDIZAJES APROBADOS",
    "informe_consultor", "mejoras_propuestas", "NIVEL DE SOLUCIÓN", "N1 · Stack",
    "N2 · Integración", "N3 · Desarrollo", "senales_comerciales", "procesos_mencionados",
    "FORMATO TÉCNICO",
]


def _text(content) -> str:
    if isinstance(content, list):
        return "".join(b.get("text", "") if isinstance(b, dict) else str(b) for b in content)
    return str(content)


def split_hidden(text: str) -> tuple[str, dict]:
    """Separa el texto visible de los bloques ocultos. Tolera bloques sin cerrar."""
    blocks = {}
    for tag in HIDDEN_TAGS:
        closed = re.compile(rf"<{tag}>(.*?)</{tag}>", re.S | re.I)
        found = closed.findall(text)
        if found:
            blocks[tag] = found[-1].strip()
        text = closed.sub("", text)
        text = re.sub(rf"<{tag}>.*\Z", "", text, flags=re.S | re.I)
        text = re.sub(rf"</{tag}>", "", text, flags=re.I)
    return text.strip(), blocks


def split_options(text: str) -> tuple[str, list[str]]:
    """Extrae los botones [[...]] y devuelve (texto sin botones, lista de botones)."""
    options = []
    for o in OPT_RE.findall(text):
        o = o.strip()
        if o and o not in options:
            options.append(o)
    clean = OPT_RE.sub("", text)
    clean = re.sub(r"[ \t]+\n", "\n", clean)
    clean = re.sub(r"\n{3,}", "\n\n", clean).strip()
    return clean, options[:8]


def _loads_lenient(raw):
    if not raw:
        return None
    s = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip(), flags=re.I)
    try:
        return json.loads(s)
    except Exception:
        pass
    for a, b in (("{", "}"), ("[", "]")):
        i, j = s.find(a), s.rfind(b)
        if i != -1 and j > i:
            try:
                return json.loads(s[i : j + 1])
            except Exception:
                continue
    return None


def merge_estado(prev: dict, new) -> dict:
    """Aplica el estado devuelto por el modelo sin perder datos si omite o vacía campos."""
    if not isinstance(new, dict):
        return prev
    out = {**prev, **new}
    persona = dict(prev.get("persona") or {})
    if isinstance(new.get("persona"), dict):
        for k, v in new["persona"].items():
            if v not in ("", None, []):
                persona[k] = v
    out["persona"] = persona
    if out.get("fase") not in PHASES:
        out["fase"] = prev.get("fase", "identificacion")
    if out.get("modo") not in ("deep", "rapido", None):
        out["modo"] = prev.get("modo")
    if len(json.dumps(out, ensure_ascii=False)) > 60000:  # protección ante estados desbordados
        return prev
    return out


def _estado_hash(estado: dict) -> str:
    d = {k: v for k, v in estado.items() if k not in ("fase", "modo")}
    return hashlib.md5(json.dumps(d, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def capture_contact(text: str, prev_ai: str, contacto: dict) -> dict:
    """Respaldo determinista: mail y teléfono no dependen del LLM."""
    c = dict(contacto)
    if text.startswith("📎"):  # los documentos subidos traen datos de terceros
        return c
    m = EMAIL_RE.search(text)
    if m:
        c["email"] = m.group(0)
    if not c.get("telefono"):
        asking = bool(CONTACT_ASK.search(prev_ai))
        for m in PHONE_RE.finditer(text):
            raw = m.group(0).strip()
            digits = re.sub(r"\D", "", raw)
            if 8 <= len(digits) <= 15 and (asking or raw.startswith("+") or PHONE_HINT.search(text)):
                c["telefono"] = raw
                break
    return c


def _window(messages: list) -> list:
    w = list(messages[-HISTORY_WINDOW:])
    while w and not isinstance(w[0], HumanMessage):  # Anthropic exige empezar con usuario
        w.pop(0)
    return w or list(messages[-1:])


def _transcript(messages: list, cap: int = 40000) -> str:
    lines = []
    for m in messages:
        who = "USUARIO" if isinstance(m, HumanMessage) else "BOT"
        t = _text(m.content)
        if len(t) > 3000:
            t = t[:3000] + " …[recortado]"
        lines.append(f"{who}: {t}")
    s = "\n".join(lines)
    return s[-cap:] if len(s) > cap else s


# ---------------------------------------------------------------------------
# Webhook a n8n
# ---------------------------------------------------------------------------
def _post_webhook(payload: dict):
    """Nunca rompe la conversación, pero SIEMPRE deja rastro en los logs."""
    if not N8N_WEBHOOK_URL:
        logger.warning("N8N_WEBHOOK_URL no está configurada: no se envía evento=%s", payload.get("evento"))
        return
    headers = {"X-Auth-Token": N8N_WEBHOOK_TOKEN} if N8N_WEBHOOK_TOKEN else {}
    try:
        r = httpx.post(N8N_WEBHOOK_URL, json=payload, headers=headers, timeout=15)
        r.raise_for_status()
        logger.info("Webhook n8n OK evento=%s status=%s", payload.get("evento"), r.status_code)
    except httpx.HTTPStatusError as exc:
        logger.error("Webhook n8n FALLÓ evento=%s status=%s body=%s",
                     payload.get("evento"), exc.response.status_code, exc.response.text[:200])
    except Exception as exc:
        logger.error("Webhook n8n FALLÓ evento=%s error=%s", payload.get("evento"), exc)


# ---------------------------------------------------------------------------
# Estado del grafo y nodos
# ---------------------------------------------------------------------------
class State(TypedDict, total=False):
    messages: Annotated[list, add_messages]
    estado: dict
    contacto: dict
    turns: int
    historial_procesos: list
    informe_hash: str
    informe_version: int
    informe_trigger: bool
    lead_sent: bool


def sanitize(state: State, config):
    msgs = state["messages"]
    last = msgs[-1]
    raw = _text(last.content)
    clean, _ = split_hidden(raw)  # nadie puede falsificar <estado> & co. desde el chat
    clean = clean.strip() or "(mensaje vacío)"
    prev_ai = _text(msgs[-2].content) if len(msgs) >= 2 and isinstance(msgs[-2], AIMessage) else ""
    out = {
        "turns": state.get("turns", 0) + 1,
        "informe_trigger": False,
        "contacto": capture_contact(clean, prev_ai, state.get("contacto") or {}),
    }
    if clean != raw:
        out["messages"] = [HumanMessage(content=clean, id=last.id)]
    return out


def gate(state: State) -> Literal["after", "capped", "interview"]:
    if (state.get("estado") or {}).get("fase") == "terminado":
        return "after"
    if state.get("turns", 0) > MAX_TURNS + 3:
        return "capped"
    return "interview"


def _trigger(state: State, estado: dict, force: bool = False) -> dict:
    """¿Hay que (re)generar el informe del consultor? Solo si cambió el contenido relevado."""
    version = state.get("informe_version", 0)
    h = _estado_hash(estado)
    if (force or estado.get("fase") in ("cierre", "terminado")) and h != state.get("informe_hash") and version < MAX_INFORMES:
        return {"informe_trigger": True, "informe_hash": h, "informe_version": version + 1}
    return {"informe_trigger": False}


def interview(state: State, config):
    thread_id = config["configurable"].get("thread_id")
    prev = state.get("estado") or estado_inicial()
    historial = state.get("historial_procesos") or []
    turns = state.get("turns", 1)

    system = build_system(store.approved_learnings())
    system += "\n\n# ESTADO ACTUAL (guardado por el sistema; actualizalo y devolvelo en <estado>)\n"
    system += json.dumps(prev, ensure_ascii=False)
    if historial:
        system += "\n\n# PROCESOS YA RELEVADOS EN ESTA SESIÓN (solo nombres)\n"
        system += ", ".join(h.get("proceso", "?") for h in historial)
    if turns >= MAX_TURNS:
        system += (
            "\n\n# AVISO DEL SISTEMA\nSe alcanzó el límite de la sesión: cerrá ahora el relevamiento con lo "
            "relevado (SALIDA PARA EL USUARIO), poné fase = \"cierre\" y anotá en huecos lo que quedó incompleto."
        )

    raw = llm.invoke([SystemMessage(content=system)] + _window(state["messages"]))
    visible, blocks = split_hidden(_text(raw.content))

    # Blindaje de salida: si el modelo filtra su prompt, se reemplaza y se descarta su estado.
    leaked = any(m.lower() in visible.lower() for m in LEAK_MARKERS)
    if leaked:
        logger.warning("Posible filtración de prompt en thread=%s: respuesta reemplazada", thread_id)
        visible = f"Soy un asistente de relevamiento de {AGENCY}. Sigamos con tu proceso. [[Volver al relevamiento]]"
        estado = prev
    else:
        estado = merge_estado(prev, _loads_lenient(blocks.get("estado")))
        if "estado" in blocks and estado is prev:
            logger.warning("Estado inválido del modelo en thread=%s: se conserva el anterior", thread_id)
    if not visible.strip():
        visible = "Perdón, no pude procesar eso. ¿Podés repetirlo?"

    # Si arranca otro proceso, se resguarda el mapa del anterior (el estado solo guarda uno a la vez)
    a = (prev.get("proceso_actual") or "").strip()
    b = (estado.get("proceso_actual") or "").strip()
    if a and b and a.lower() != b.lower() and prev.get("etapas") and not any(
        h.get("proceso", "").lower() == a.lower() for h in historial
    ):
        historial = historial + [{"proceso": a, "etapas": prev["etapas"]}]

    contacto = state.get("contacto") or {}
    lead_sent = state.get("lead_sent", False)
    if (contacto.get("email") or contacto.get("telefono")) and not lead_sent:
        _post_webhook({
            "evento": "contacto_capturado", "thread_id": thread_id,
            "email": contacto.get("email"), "telefono": contacto.get("telefono"),
            "persona": estado.get("persona"), "estado": estado,
        })
        lead_sent = True

    store.save_relevamiento(thread_id, estado, contacto)
    return {
        "messages": [AIMessage(content=visible)],
        "estado": estado,
        "historial_procesos": historial,
        "lead_sent": lead_sent,
        **_trigger(state, estado),
    }


def after(state: State):
    msg = (f"El relevamiento ya fue enviado al equipo de {AGENCY}. "
           "Si querés relevar otro proceso, recargá la página para empezar de nuevo.")
    return {"messages": [AIMessage(content=msg)]}


def capped(state: State):
    estado = state.get("estado") or estado_inicial()
    msg = (f"Llegamos al límite de esta sesión. Lo relevado ya quedó registrado para que un consultor de "
           f"{AGENCY} lo revise. Si querés seguir, recargá la página para empezar una nueva.")
    return {"messages": [AIMessage(content=msg)], **_trigger(state, estado, force=True)}


def build_graph():
    g = StateGraph(State)
    g.add_node("sanitize", sanitize)
    g.add_node("interview", interview)
    g.add_node("after", after)
    g.add_node("capped", capped)
    g.add_edge(START, "sanitize")
    g.add_conditional_edges("sanitize", gate)
    g.add_edge("interview", END)
    g.add_edge("after", END)
    g.add_edge("capped", END)
    return g.compile(checkpointer=make_checkpointer())


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


# ---------------------------------------------------------------------------
# Informe del consultor + automejora (segundo plano, tras el cierre)
# ---------------------------------------------------------------------------
INFORME_TASK = """# TAREA ACTUAL (del sistema)
El relevamiento llegó a su cierre. Generá ahora ÚNICAMENTE estos dos bloques, sin ningún texto adicional:
<informe_consultor> ...las 12 secciones de "SALIDA PARA EL CONSULTOR", en markdown... </informe_consultor>
<mejoras_propuestas> ...array JSON con el formato de AUTOMEJORA (máximo 5, solo con evidencia concreta)... </mejoras_propuestas>
Reglas: usá el ESTADO FINAL y la TRANSCRIPCIÓN como única fuente; incluí también los PROCESOS RELEVADOS ANTES si los hay; marcá cada proceso como "núcleo" o "soporte"; si el relevamiento se cortó antes de tiempo, marcá en huecos qué quedó incompleto; las evidencias son frases textuales del usuario."""

MEJORA_TIPOS = {"pregunta_nueva", "senal_nueva", "perfil_area", "rubro", "ajuste_flujo", "stack"}


def parse_mejoras(raw) -> list[dict]:
    data = _loads_lenient(raw)
    if isinstance(data, dict):
        data = [data]
    out = []
    for m in data if isinstance(data, list) else []:
        if not isinstance(m, dict) or m.get("tipo") not in MEJORA_TIPOS:
            continue
        prop, evid = str(m.get("propuesta") or "").strip(), str(m.get("evidencia") or "").strip()
        if not prop or not evid:  # sin evidencia concreta no se guarda
            continue
        out.append({
            "tipo": m["tipo"], "propuesta": prop[:600], "evidencia": evid[:400],
            "motivo": str(m.get("motivo") or "")[:400], "donde_aplica": str(m.get("donde_aplica") or "")[:120],
        })
    return out[:5]


def generate_informe(estado: dict, historial: list, transcript: str) -> tuple[str, list[dict]]:
    system = build_system(store.approved_learnings(), technical=False) + "\n\n" + INFORME_TASK
    user = (
        "ESTADO FINAL (JSON):\n" + json.dumps(estado, ensure_ascii=False)
        + "\n\nPROCESOS RELEVADOS ANTES (JSON):\n" + json.dumps(historial, ensure_ascii=False)
        + "\n\nTRANSCRIPCIÓN:\n" + transcript
    )
    raw = llm.invoke([SystemMessage(content=system), HumanMessage(content=user)])
    text = _text(raw.content)
    m = re.search(r"<informe_consultor>(.*?)</informe_consultor>", text, re.S | re.I)
    informe = (m.group(1).strip() if m else re.sub(r"<mejoras_propuestas>.*", "", text, flags=re.S | re.I).strip())
    if len(informe) < 100:
        raise ValueError("El modelo no devolvió un informe utilizable")
    mj = re.search(r"<mejoras_propuestas>(.*?)</mejoras_propuestas>", text, re.S | re.I)
    return informe, parse_mejoras(mj.group(1) if mj else "")


def run_informe(graph, thread_id: str):
    """Tarea en segundo plano: el usuario ya recibió su respuesta; esto no lo hace esperar."""
    config = {"configurable": {"thread_id": thread_id}}
    snap = {}
    try:
        snap = graph.get_state(config).values
        estado = snap.get("estado") or {}
        contacto = snap.get("contacto") or {}
        version = snap.get("informe_version", 1)
        informe, mejoras = generate_informe(
            estado, snap.get("historial_procesos") or [], _transcript(snap.get("messages") or [])
        )
    except Exception:
        logger.exception("No se pudo generar el informe thread=%s", thread_id)
        _post_webhook({
            "evento": "informe_fallido", "thread_id": thread_id,
            "email": (snap.get("contacto") or {}).get("email"),
            "telefono": (snap.get("contacto") or {}).get("telefono"),
            "estado": snap.get("estado"), "historial_procesos": snap.get("historial_procesos"),
        })
        return
    _post_webhook({
        "evento": "informe_consultor", "thread_id": thread_id, "version": version,
        "email": contacto.get("email"), "telefono": contacto.get("telefono"),
        "persona": estado.get("persona"), "estado": estado,
        "informe_consultor": informe, "mejoras_propuestas": mejoras,
    })
    store.save_relevamiento(thread_id, estado, contacto, informe=informe, version=version)
    store.save_mejoras(thread_id, mejoras)
