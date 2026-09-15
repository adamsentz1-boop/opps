"""Attachment ingestion: download/upload -> Stirling PDF (local) -> sanitised untrusted text -> agents.

Sequential by design (one document at a time, global lock) because Stirling is heavy on the Lenovo.
Never crashes the scheduler: every failure is recorded on the Attachment row and surfaced in the dashboard.
"""
from __future__ import annotations

import logging
import re
import threading
import urllib.parse
import urllib.request
from pathlib import Path

from sqlalchemy.orm import Session

from app.audit import log_event
from app.config import BASE_DIR, get_settings
from app.documents.stirling import PDFServiceError, PDFServiceUnavailable, StirlingClient
from app.models import Attachment, Opportunity, utcnow
from app.sanitize import clean_text, detect_injection, wrap_untrusted

log = logging.getLogger(__name__)

ATTACHMENT_DIR = BASE_DIR / "data" / "attachments"
_ingest_lock = threading.Lock()


def _client() -> StirlingClient:
    s = get_settings()
    if s.pdf_service_type.lower() != "stirling":
        raise PDFServiceError(f"Unsupported PDF_SERVICE_TYPE {s.pdf_service_type!r}; only 'stirling' is implemented")
    return StirlingClient()


def pdf_service_status() -> dict:
    s = get_settings()
    base = {"enabled": s.pdf_ingestion_enabled, "type": s.pdf_service_type, "url": s.pdf_service_url}
    if not s.pdf_ingestion_enabled:
        return {**base, "reachable": False, "version": None, "error": "PDF ingestion disabled (PDF_INGESTION_ENABLED=false)"}
    try:
        return {**base, **_client().status()}
    except PDFServiceError as exc:
        return {**base, "reachable": False, "version": None, "error": str(exc)}


def _safe_name(name: str) -> str:
    name = re.sub(r"[^A-Za-z0-9._ -]+", "_", (name or "").strip()).strip(" ._")[:150]
    return name or "document.pdf"


def attachment_dir(opportunity_id: str) -> Path:
    path = ATTACHMENT_DIR / opportunity_id
    path.mkdir(parents=True, exist_ok=True)
    return path


def add_attachment(db: Session, opp: Opportunity, filename: str, data: bytes | None = None,
                   source_url: str | None = None, content_type: str | None = None) -> Attachment:
    """Register a document. With `data` the file is stored now; with only `source_url` it is downloaded at
    ingestion time (only from allowed hosts)."""
    filename = _safe_name(filename or (urllib.parse.urlparse(source_url or "").path.rsplit("/", 1)[-1]) or "document.pdf")
    existing = next((a for a in db.query(Attachment).filter_by(opportunity_id=opp.id)
                     if (source_url and a.source_url == source_url) or (data is not None and a.filename == filename)), None)
    if existing:
        return existing
    att = Attachment(opportunity_id=opp.id, filename=filename, source_url=source_url, content_type=content_type)
    if data is not None:
        path = attachment_dir(opp.id) / filename
        path.write_bytes(data)
        att.file_path = str(path)
        att.size_bytes = len(data)
    db.add(att)
    opp.attachments.append(att)
    db.flush()
    log_event(db, agent="System", action="attachment.added", object_type="attachment", object_id=att.id,
              new_state=att.status, details={"opportunity_id": opp.id, "filename": filename, "source_url": source_url})
    return att


def _download(att: Attachment) -> bytes:
    s = get_settings()
    url = att.source_url or ""
    host = urllib.parse.urlparse(url).hostname or ""
    if not url.startswith("https://"):
        raise PDFServiceError("Only https downloads are allowed")
    if not any(host == h or host.endswith("." + h) for h in s.pdf_allowed_hosts):
        raise PDFServiceError(f"Host {host!r} is not in PDF_DOWNLOAD_ALLOWED_HOSTS; upload the file manually")
    req = urllib.request.Request(url, headers={"User-Agent": "OpportunityEngine/0.1 (+local)", "Accept": "application/pdf,*/*"})
    limit = s.pdf_max_file_mb * 1024 * 1024
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:  # noqa: S310 - allow-listed host
            data = resp.read(limit + 1)
            att.content_type = resp.headers.get("Content-Type")
    except Exception as exc:  # noqa: BLE001
        raise PDFServiceError(f"Download failed: {type(exc).__name__}: {str(exc)[:200]}") from None
    if len(data) > limit:
        raise PDFServiceError(f"File exceeds PDF_MAX_FILE_MB ({s.pdf_max_file_mb} MB)")
    return data


def ingest_attachment(db: Session, att: Attachment) -> Attachment:
    """Extract text for one attachment through Stirling PDF. Records the outcome; never raises for service errors."""
    s = get_settings()
    att.attempts += 1
    att.status = "PROCESSING"
    att.error = None
    db.flush()
    try:
        if not s.pdf_ingestion_enabled:
            raise PDFServiceUnavailable("PDF ingestion disabled (PDF_INGESTION_ENABLED=false)")
        if att.file_path and Path(att.file_path).exists():
            data = Path(att.file_path).read_bytes()
        elif att.source_url:
            data = _download(att)
            path = attachment_dir(att.opportunity_id) / att.filename
            path.write_bytes(data)
            att.file_path, att.size_bytes = str(path), len(data)
        else:
            raise PDFServiceError("Attachment has neither a stored file nor a source URL")
        if len(data) > s.pdf_max_file_mb * 1024 * 1024:
            raise PDFServiceError(f"File exceeds PDF_MAX_FILE_MB ({s.pdf_max_file_mb} MB)")
        if data[:5] != b"%PDF-":
            if _looks_like_text(data):
                text = data.decode("utf-8", errors="replace")
                att.method, att.pages = "none", None
                notes = ["plain text file; no PDF processing needed"]
            else:
                raise PDFServiceError("Not a PDF (only PDF and plain-text attachments are supported)")
        else:
            result = _client().extract(att.filename, data, s.pdf_min_chars_per_page)
            text, att.method, att.pages, notes = result.text, result.method, result.pages, result.notes
        text = clean_text(text, s.pdf_max_text_chars)
        att.extracted_text = text
        att.text_chars = len(text)
        att.injection_flags = detect_injection(text)
        att.status = "EXTRACTED"
        att.processed_at = utcnow()
        db.flush()
        log_event(db, agent="System", action="attachment.extracted", object_type="attachment", object_id=att.id,
                  new_state=att.status, details={"method": att.method, "pages": att.pages, "chars": att.text_chars,
                                                 "injection_flags": att.injection_flags, "notes": notes})
    except PDFServiceUnavailable as exc:
        att.status = "PENDING"  # retry automatically once the service is back
        att.error = str(exc)[:1000]
        db.flush()
        log.warning("PDF service unavailable for attachment %s: %s", att.id, exc)
    except PDFServiceError as exc:
        att.status = "FAILED"
        att.error = str(exc)[:1000]
        db.flush()
        log_event(db, agent="System", action="attachment.failed", object_type="attachment", object_id=att.id,
                  new_state=att.status, details={"error": att.error})
    except Exception as exc:  # noqa: BLE001 - never let ingestion take down the scheduler
        att.status = "FAILED"
        att.error = f"{type(exc).__name__}: {exc}"[:1000]
        db.flush()
        log.exception("attachment ingestion crashed for %s", att.id)
    return att


def ingest_pending(db: Session, limit: int = 20) -> dict:
    """Process pending attachments one at a time. Stops early when the service is unreachable."""
    summary = {"processed": 0, "extracted": 0, "failed": 0, "skipped_unavailable": 0, "service": None}
    if not _ingest_lock.acquire(blocking=False):
        summary["service"] = "busy"
        return summary
    try:
        pending = db.query(Attachment).filter(Attachment.status == "PENDING").order_by(Attachment.created_at).limit(limit).all()
        if not pending:
            return summary
        status = pdf_service_status()
        summary["service"] = status
        if not status.get("reachable"):
            summary["skipped_unavailable"] = len(pending)
            for att in pending:
                att.error = status.get("error")
            db.flush()
            return summary
        for att in pending:
            ingest_attachment(db, att)
            db.commit()
            summary["processed"] += 1
            if att.status == "EXTRACTED":
                summary["extracted"] += 1
            elif att.status == "FAILED":
                summary["failed"] += 1
            else:
                summary["skipped_unavailable"] += 1
                break  # service went away mid-run; stop and retry later
        return summary
    finally:
        _ingest_lock.release()


def attachment_blocks(opp: Opportunity, max_total_chars: int = 120_000) -> list[str]:
    """Extracted attachment text as delimited untrusted blocks for agent prompts (never as instructions)."""
    blocks: list[str] = []
    budget = max_total_chars
    for att in opp.attachments:
        if att.status != "EXTRACTED" or not att.extracted_text:
            continue
        text = att.extracted_text[:budget]
        budget -= len(text)
        label = f"attachment:{att.filename} ({att.method or 'text'}, {att.pages or '?'} pages)"
        blocks.append(wrap_untrusted(label, text))
        if budget <= 0:
            blocks.append("[further attachments omitted: prompt size limit]")
            break
    return blocks


def _looks_like_text(data: bytes) -> bool:
    sample = data[:4000]
    if not sample:
        return False
    try:
        sample.decode("utf-8")
    except UnicodeDecodeError:
        return False
    printable = sum(1 for b in sample if 32 <= b < 127 or b in (9, 10, 13))
    return printable / len(sample) > 0.9
