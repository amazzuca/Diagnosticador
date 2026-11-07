"""Persistencia opcional en Postgres (la misma DATABASE_URL del checkpointer).

Tablas (se crean solas):
  - relevamientos: una fila por conversación (estado, contacto, último informe del consultor).
  - aprendizajes: mejoras propuestas por el bot (status 'pendiente'). El consultor las aprueba
    cambiando status a 'aprobada' desde el Table Editor de Supabase; el bot las inserta en su prompt.

Todo es "best effort": si la base falla, la conversación sigue.
"""
import logging
import os
import re
import time
from pathlib import Path

logger = logging.getLogger("diagnosticador")

DATABASE_URL = os.getenv("DATABASE_URL", "")
LEARN_FILE = Path(__file__).parent / "prompts" / "aprendizajes.md"

SCHEMA = [
    """create table if not exists relevamientos (
        thread_id text primary key,
        email text, telefono text,
        persona jsonb, estado jsonb,
        informe text, version int not null default 0,
        created_at timestamptz not null default now(),
        updated_at timestamptz not null default now())""",
    """create table if not exists aprendizajes (
        id bigserial primary key,
        tipo text, propuesta text, evidencia text, motivo text, donde_aplica text,
        status text not null default 'pendiente',
        thread_id text,
        created_at timestamptz not null default now())""",
    # Estas tablas guardan datos de clientes: sin RLS quedarían expuestas por la API pública de Supabase.
    "alter table relevamientos enable row level security",
    "alter table aprendizajes enable row level security",
]

_pool = None
_failed_at = 0.0
_cache = {"t": 0.0, "text": ""}


def enabled() -> bool:
    return bool(DATABASE_URL)


def _get_pool():
    global _pool, _failed_at
    if not DATABASE_URL:
        return None
    if _pool is not None:
        return _pool
    if time.time() - _failed_at < 300:
        return None
    try:
        from psycopg_pool import ConnectionPool

        pool = ConnectionPool(
            conninfo=DATABASE_URL,
            min_size=1,
            max_size=3,
            kwargs={"autocommit": True, "prepare_threshold": 0},
            check=ConnectionPool.check_connection,
            open=True,
        )
        with pool.connection() as conn:
            for stmt in SCHEMA:
                conn.execute(stmt)
        _pool = pool
    except Exception:
        logger.exception("store: no se pudo inicializar la base (se reintenta en 5 min)")
        _failed_at = time.time()
    return _pool


def save_relevamiento(thread_id, estado, contacto, informe=None, version=None):
    pool = _get_pool()
    if pool is None:
        return
    try:
        from psycopg.types.json import Jsonb

        contacto = contacto or {}
        with pool.connection() as conn:
            conn.execute(
                """insert into relevamientos (thread_id, email, telefono, persona, estado, informe, version)
                   values (%s,%s,%s,%s,%s,%s,%s)
                   on conflict (thread_id) do update set
                     email = coalesce(excluded.email, relevamientos.email),
                     telefono = coalesce(excluded.telefono, relevamientos.telefono),
                     persona = excluded.persona,
                     estado = excluded.estado,
                     informe = coalesce(excluded.informe, relevamientos.informe),
                     version = greatest(excluded.version, relevamientos.version),
                     updated_at = now()""",
                (
                    thread_id,
                    contacto.get("email"),
                    contacto.get("telefono"),
                    Jsonb((estado or {}).get("persona") or {}),
                    Jsonb(estado or {}),
                    informe,
                    version or 0,
                ),
            )
    except Exception:
        logger.exception("store: error guardando relevamiento thread=%s", thread_id)


def save_mejoras(thread_id, mejoras):
    pool = _get_pool()
    if pool is None or not mejoras:
        return
    try:
        with pool.connection() as conn:
            for m in mejoras:
                conn.execute(
                    """insert into aprendizajes (tipo, propuesta, evidencia, motivo, donde_aplica, thread_id)
                       values (%s,%s,%s,%s,%s,%s)""",
                    (m["tipo"], m["propuesta"], m["evidencia"], m.get("motivo", ""), m.get("donde_aplica", ""), thread_id),
                )
    except Exception:
        logger.exception("store: error guardando mejoras thread=%s", thread_id)


def approved_learnings() -> str:
    """Texto para {{APRENDIZAJES_APROBADOS}}: archivo + filas aprobadas en la base (cache 60 s)."""
    if time.time() - _cache["t"] < 60:
        return _cache["text"]
    parts = []
    try:
        if LEARN_FILE.exists():
            txt = re.sub(r"<!--.*?-->", "", LEARN_FILE.read_text(encoding="utf-8"), flags=re.S).strip()
            if txt:
                parts.append(txt)
    except Exception:
        logger.exception("store: no se pudo leer aprendizajes.md")
    pool = _get_pool()
    if pool is not None:
        try:
            with pool.connection() as conn:
                rows = conn.execute(
                    "select tipo, propuesta, donde_aplica from aprendizajes where status = 'aprobada' order by id"
                ).fetchall()
            for tipo, propuesta, donde in rows:
                parts.append(f"- [{tipo}] ({donde or 'general'}) {propuesta}")
        except Exception:
            logger.exception("store: no se pudieron leer aprendizajes aprobados")
    _cache.update(t=time.time(), text="\n".join(parts))
    return _cache["text"]
