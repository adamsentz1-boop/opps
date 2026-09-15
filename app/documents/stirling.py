"""Client for the existing self-hosted Stirling PDF service (local document preprocessing layer).

Verified endpoints (Stirling-PDF source / docs):
  GET  /api/v1/info/status                      -> {"version": "...", "status": "UP"}   (no auth required)
  POST /api/v1/convert/pdf/text                 multipart fileInput, outputFormat=txt -> text/plain (PDFBox)
  POST /api/v1/misc/ocr-pdf                     multipart fileInput, languages (repeatable), ocrType
                                                (skip-text|force-ocr|auto), ocrRenderType (hocr|sandwich),
                                                sidecar=true -> zip containing the OCR'd PDF and a .txt sidecar
  POST /api/v1/security/get-info-on-pdf         multipart fileInput -> JSON incl. PerPageInfo."Page N"."Text Characters Count"
Auth (only when Stirling login is enabled): header X-API-KEY.

Documents never leave the local network: the service URL is owner-configured (host.docker.internal by default).
"""
from __future__ import annotations

import io
import json
import logging
import uuid
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass, field

from app.config import get_settings

log = logging.getLogger(__name__)


class PDFServiceError(RuntimeError):
    """The PDF service is unreachable or returned an error. Never fatal for the scheduler."""


class PDFServiceUnavailable(PDFServiceError):
    """Stirling PDF is not reachable (stopped container, wrong URL)."""


@dataclass
class ExtractResult:
    text: str
    method: str            # "text" | "ocr"
    pages: int | None = None
    chars_per_page: float | None = None
    notes: list[str] = field(default_factory=list)


def _multipart(fields: list[tuple[str, str]], file_field: str, filename: str, data: bytes,
               content_type: str = "application/pdf") -> tuple[bytes, str]:
    boundary = f"----OpportunityEngine{uuid.uuid4().hex}"
    body = io.BytesIO()
    for name, value in fields:
        body.write(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n{value}\r\n".encode())
    body.write(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{file_field}\"; filename=\"{filename}\"\r\n"
               f"Content-Type: {content_type}\r\n\r\n".encode())
    body.write(data)
    body.write(f"\r\n--{boundary}--\r\n".encode())
    return body.getvalue(), f"multipart/form-data; boundary={boundary}"


class StirlingClient:
    def __init__(self, base_url: str | None = None, api_key: str | None = None, timeout: int | None = None,
                 languages: list[str] | None = None):
        s = get_settings()
        self.base_url = (base_url or s.pdf_service_url).rstrip("/")
        self.api_key = api_key if api_key is not None else s.pdf_service_api_key
        self.timeout = timeout or s.pdf_request_timeout_seconds
        self.languages = languages or s.pdf_languages

    # ------------------------------------------------------------------ transport
    def _headers(self, extra: dict | None = None) -> dict:
        h = {"Accept": "*/*", "User-Agent": "OpportunityEngine/0.1 (+local)"}
        if self.api_key:
            h["X-API-KEY"] = self.api_key
        h.update(extra or {})
        return h

    def _open(self, req: urllib.request.Request, timeout: int | None = None):
        try:
            return urllib.request.urlopen(req, timeout=timeout or self.timeout)  # noqa: S310 - owner-configured local URL
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", errors="replace")[:300]
            except Exception:  # noqa: BLE001
                pass
            raise PDFServiceError(f"Stirling PDF returned HTTP {exc.code} for {req.selector}: {detail}") from None
        except urllib.error.URLError as exc:
            raise PDFServiceUnavailable(f"Stirling PDF unreachable at {self.base_url}: {exc.reason}") from None
        except (TimeoutError, OSError) as exc:
            raise PDFServiceUnavailable(f"Stirling PDF unreachable at {self.base_url}: {exc}") from None

    def _post_file(self, path: str, fields: list[tuple[str, str]], filename: str, data: bytes) -> tuple[bytes, str]:
        body, ctype = _multipart(fields, "fileInput", filename, data)
        req = urllib.request.Request(self.base_url + path, data=body, method="POST",
                                     headers=self._headers({"Content-Type": ctype}))
        with self._open(req) as resp:
            return resp.read(), (resp.headers.get("Content-Type") or "")

    # ------------------------------------------------------------------ operations
    def status(self) -> dict:
        """Returns {"reachable": bool, "version": str|None, "status": str|None, "error": str|None}."""
        req = urllib.request.Request(self.base_url + "/api/v1/info/status", headers=self._headers())
        try:
            with self._open(req, timeout=5) as resp:
                data = json.loads(resp.read().decode("utf-8", errors="replace") or "{}")
            return {"reachable": True, "version": data.get("version"), "status": data.get("status"), "error": None,
                    "url": self.base_url}
        except PDFServiceError as exc:
            return {"reachable": False, "version": None, "status": None, "error": str(exc), "url": self.base_url}
        except json.JSONDecodeError:
            return {"reachable": True, "version": None, "status": "unknown", "error": None, "url": self.base_url}

    def pdf_to_text(self, filename: str, data: bytes) -> str:
        raw, _ = self._post_file("/api/v1/convert/pdf/text", [("outputFormat", "txt")], filename, data)
        return raw.decode("utf-8", errors="replace")

    def page_info(self, filename: str, data: bytes) -> dict:
        raw, _ = self._post_file("/api/v1/security/get-info-on-pdf", [], filename, data)
        try:
            return json.loads(raw.decode("utf-8", errors="replace"))
        except json.JSONDecodeError:
            return {}

    def ocr_text(self, filename: str, data: bytes, force: bool = False) -> str:
        fields = [("languages", lang) for lang in self.languages]
        fields += [("ocrType", "force-ocr" if force else "skip-text"), ("ocrRenderType", "hocr"),
                   ("sidecar", "true"), ("deskew", "true"), ("clean", "false"), ("cleanFinal", "false")]
        raw, ctype = self._post_file("/api/v1/misc/ocr-pdf", fields, filename, data)
        if raw[:2] == b"PK" or "zip" in ctype or "octet-stream" in ctype:
            try:
                with zipfile.ZipFile(io.BytesIO(raw)) as zf:
                    txt_names = [n for n in zf.namelist() if n.lower().endswith(".txt")]
                    if txt_names:
                        return zf.read(txt_names[0]).decode("utf-8", errors="replace")
                    pdf_names = [n for n in zf.namelist() if n.lower().endswith(".pdf")]
                    if pdf_names:
                        return self.pdf_to_text(filename, zf.read(pdf_names[0]))
            except zipfile.BadZipFile:
                pass
        if raw[:5] == b"%PDF-":
            # server returned the OCR'd PDF without a sidecar: extract its text layer
            return self.pdf_to_text(filename, raw)
        return raw.decode("utf-8", errors="replace")

    def extract(self, filename: str, data: bytes, min_chars_per_page: int = 40) -> ExtractResult:
        """Digital PDFs: text layer only. Scanned/image PDFs: OCR. Decided per document to spare the CPU."""
        text = self.pdf_to_text(filename, data)
        pages = _count_pages(data)
        stripped = text.strip()
        per_page = (len(stripped) / pages) if pages else float(len(stripped))
        notes = [f"text layer: {len(stripped)} chars over {pages or '?'} page(s)"]
        if pages and per_page >= min_chars_per_page:
            return ExtractResult(text=text, method="text", pages=pages, chars_per_page=per_page, notes=notes)
        if not pages and len(stripped) >= min_chars_per_page * 2:
            return ExtractResult(text=text, method="text", pages=None, chars_per_page=per_page, notes=notes)
        notes.append("little or no text layer; running OCR (skip-text)")
        ocr = self.ocr_text(filename, data, force=False)
        if len(ocr.strip()) < len(stripped):
            ocr = text
            notes.append("OCR produced less text than the text layer; kept the text layer")
        return ExtractResult(text=ocr, method="ocr", pages=pages,
                             chars_per_page=(len(ocr.strip()) / pages) if pages else None, notes=notes)


def _count_pages(data: bytes) -> int | None:
    """Cheap page count from the raw PDF (no parser dependency); None if it cannot be determined."""
    import re
    matches = re.findall(rb"/Type\s*/Page[^s]", data)
    if matches:
        return len(matches)
    m = re.search(rb"/Count\s+(\d+)", data)
    return int(m.group(1)) if m else None
