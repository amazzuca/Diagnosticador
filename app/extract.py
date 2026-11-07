"""Extracción de texto de documentos subidos (PDF, Word, Excel, CSV, TXT)."""
import io
from pathlib import Path

MAX_BYTES = 5 * 1024 * 1024
MAX_CHARS = 15000
SUPPORTED = {".pdf", ".docx", ".xlsx", ".csv", ".tsv", ".txt", ".md"}


def _decode(data: bytes) -> str:
    for enc in ("utf-8-sig", "latin-1"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def extract_text(filename: str, data: bytes) -> str:
    ext = Path(filename).suffix.lower()
    if ext not in SUPPORTED:
        raise ValueError("Formato no soportado. Subí un PDF, Word (.docx), Excel (.xlsx), CSV o TXT.")

    if ext in {".txt", ".md", ".csv", ".tsv"}:
        text = _decode(data)
    elif ext == ".pdf":
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(data))
        text = "\n".join((p.extract_text() or "") for p in reader.pages[:40])
    elif ext == ".docx":
        import docx

        d = docx.Document(io.BytesIO(data))
        parts = [p.text for p in d.paragraphs if p.text.strip()]
        for t in d.tables:
            for row in t.rows:
                parts.append(" | ".join(c.text.strip() for c in row.cells))
        text = "\n".join(parts)
    else:  # .xlsx
        from openpyxl import load_workbook

        wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
        parts = []
        for ws in wb.worksheets[:5]:
            parts.append(f"## Hoja: {ws.title}")
            for i, row in enumerate(ws.iter_rows(values_only=True)):
                if i >= 200:
                    parts.append("…[filas recortadas]")
                    break
                cells = ["" if c is None else str(c) for c in row]
                if any(cells):
                    parts.append(" | ".join(cells))
        text = "\n".join(parts)

    text = text.strip()
    if len(text) < 20:
        raise ValueError("No pude leer texto en el archivo (¿es un escaneo o está vacío?).")
    if len(text) > MAX_CHARS:
        text = text[:MAX_CHARS] + "\n…[documento recortado]"
    return text
