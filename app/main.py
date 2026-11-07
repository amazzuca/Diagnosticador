import html as _html
import logging
import os
import re
import time
from collections import defaultdict, deque
from pathlib import Path
from typing import Optional

from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from langchain_core.messages import HumanMessage
from pydantic import BaseModel, Field

from .extract import MAX_BYTES, extract_text
from .graph import MODEL, _text, build_graph, phase_info, run_informe, split_options
from .prompt import AGENCY, load_base

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("diagnosticador")

graph = build_graph()

app = FastAPI(title="Relevador de procesos")

# Para embeber el chat en otro sitio, listá los dominios separados por coma.
origins = [o.strip() for o in os.getenv("ALLOWED_ORIGINS", "").split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins or ["*"],
    allow_methods=["POST", "GET"],
    allow_headers=["*"],
)

# --- Rate limit simple por IP (en memoria) para proteger el gasto de LLM ---
RATE_LIMIT = int(os.getenv("RATE_LIMIT_PER_HOUR", "120"))
_hits: dict[str, deque] = defaultdict(deque)


def _limited(ip: str) -> bool:
    now = time.time()
    q = _hits[ip]
    while q and now - q[0] > 3600:
        q.popleft()
    if len(q) >= RATE_LIMIT:
        return True
    q.append(now)
    return False


class ChatIn(BaseModel):
    thread_id: str = Field(min_length=8, max_length=64)
    message: str = Field(min_length=1, max_length=2000)
    # Id idempotente: si el cliente reintenta el mismo mensaje, LangGraph lo deduplica.
    message_id: Optional[str] = Field(default=None, max_length=64)


def _turn(thread_id: str, text: str, message_id: Optional[str], request: Request, bg: BackgroundTasks):
    ip = (request.headers.get("x-forwarded-for", "").split(",")[0].strip()) or request.client.host
    if _limited(ip):
        raise HTTPException(429, "Demasiadas solicitudes. Probá más tarde.")
    config = {"configurable": {"thread_id": thread_id}}
    try:
        out = graph.invoke({"messages": [HumanMessage(content=text, id=message_id)]}, config)
    except Exception as exc:
        # El traceback completo queda en los logs de Render.
        logger.exception("Error en turno thread=%s", thread_id)
        raise HTTPException(502, f"Error interno ({type(exc).__name__}). Revisá los logs del servidor.")
    if out.get("informe_trigger"):
        bg.add_task(run_informe, graph, thread_id)  # informe + mejoras, sin hacer esperar al usuario
    reply, options = split_options(_text(out["messages"][-1].content))
    progress, label = phase_info(out.get("estado"))
    return {
        "reply": reply,
        "options": options,
        "progress": progress / 100,
        "phase": label,
        "done": (out.get("estado") or {}).get("fase") == "terminado",
    }


@app.post("/chat")
def chat(body: ChatIn, request: Request, bg: BackgroundTasks):
    return _turn(body.thread_id, body.message, body.message_id, request, bg)


@app.post("/upload")
def upload(
    request: Request,
    bg: BackgroundTasks,
    thread_id: str = Form(..., min_length=8, max_length=64),
    message_id: Optional[str] = Form(None, max_length=64),
    file: UploadFile = File(...),
):
    data = file.file.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise HTTPException(413, f"El archivo supera los {MAX_BYTES // (1024 * 1024)} MB.")
    name = re.sub(r'[<>"\']', "", file.filename or "documento")[:80]
    try:
        text = extract_text(name, data)
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    except Exception:
        logger.exception("No se pudo leer el archivo %s", name)
        raise HTTPException(422, "No pude leer el archivo. Probá con otro formato.")
    text = text.replace("</documento", "‹/documento")  # el contenido no puede cerrar el envoltorio
    content = f'📎 Documento subido: {name}\n<documento nombre="{name}">\n{text}\n</documento>'
    return _turn(thread_id, content, message_id, request, bg)


@app.get("/health")
def health():
    """Chequeo de estado: no expone secretos, solo si están configurados."""
    return {
        "status": "ok",
        "model": MODEL,
        "api_key_set": bool(os.getenv("OPENAI_API_KEY") or os.getenv("ANTHROPIC_API_KEY")),
        "checkpointer": "postgres" if os.getenv("DATABASE_URL") else "memory",
        "n8n_webhook_set": bool(os.getenv("N8N_WEBHOOK_URL")),
        "n8n_token_set": bool(os.getenv("N8N_WEBHOOK_TOKEN")),
        "n8n_url_is_test": "/webhook-test/" in os.getenv("N8N_WEBHOOK_URL", ""),
        "prompt_chars": len(load_base()),
    }


STATIC = Path(__file__).parent / "static"


def _logo_html() -> str:
    """Logo bicolor como ProPulse ("Pro" + "Pulse" en color de acento).
    BRAND_LOGO="Pro|Pulse" marca el corte; si no está, se corta en la última palabra de AGENCY_NAME."""
    raw = (os.getenv("BRAND_LOGO") or AGENCY).strip()
    if "|" in raw:
        a, b = raw.split("|", 1)
    elif " " in raw:
        a, b = raw.rsplit(" ", 1)
        a += " "
    else:
        a, b = raw, ""
    out = _html.escape(a)
    return out + (f"<span>{_html.escape(b)}</span>" if b else "")


@app.get("/")
def index():
    page = (STATIC / "index.html").read_text(encoding="utf-8")
    page = page.replace("__LOGO__", _logo_html()).replace("__AGENCY__", _html.escape(AGENCY))
    return HTMLResponse(page)
