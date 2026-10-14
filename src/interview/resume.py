import io
import logging
import re

import docx
from fastapi import HTTPException, UploadFile, status
from pypdf import PdfReader

logger = logging.getLogger(__name__)

MAX_RESUME_BYTES = 5 * 1024 * 1024  # 5 MB
# a long CV is ~10k characters; more than this is cut so prompts stay small
MAX_RESUME_CHARS = 15000
# fewer characters than this means we couldn't really read it (e.g. a scanned image PDF)
MIN_RESUME_CHARS = 100
MAX_PDF_PAGES = 20

RESUME_TOO_LARGE = "Your resume must be 5 MB or smaller."
RESUME_BAD_TYPE = "Upload your resume as a PDF or Word (.docx) file."
RESUME_UNREADABLE = (
    "We couldn't read any text from your resume. If it is a scanned image, "
    "upload a PDF or Word file with selectable text."
)


def _bad_request(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=detail)


def _pdf_text(data: bytes) -> str:
    reader = PdfReader(io.BytesIO(data))
    return "\n".join(page.extract_text() or "" for page in reader.pages[:MAX_PDF_PAGES])


def _docx_text(data: bytes) -> str:
    document = docx.Document(io.BytesIO(data))
    parts = [p.text for p in document.paragraphs]
    # skills and experience are often laid out in tables
    for table in document.tables:
        for row in table.rows:
            parts.append(" | ".join(cell.text for cell in row.cells))
    return "\n".join(parts)


def _clean(text: str) -> str:
    text = text.replace("\x00", "")  # Postgres text columns reject NUL bytes
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n\n", text)
    return text.strip()[:MAX_RESUME_CHARS]


def read_resume(file: UploadFile) -> tuple[str, str]:
    """Returns (plain text of the resume, original file name). Raises 422 if it can't be used."""
    # read one byte past the limit, so a huge upload is never loaded fully into memory
    data = file.file.read(MAX_RESUME_BYTES + 1)
    if len(data) > MAX_RESUME_BYTES:
        raise _bad_request(RESUME_TOO_LARGE)

    filename = (file.filename or "resume").strip()[-255:]
    # check the file's own bytes, not the name or content type the browser sent
    if data.startswith(b"%PDF"):
        extract = _pdf_text
    elif data.startswith(b"PK") and filename.lower().endswith(".docx"):  # .docx is a zip file
        extract = _docx_text
    else:
        raise _bad_request(RESUME_BAD_TYPE)
    try:
        text = extract(data)
    # broken or unusual files can fail in many ways inside the parsers
    except Exception as e:
        logger.info("Could not parse resume %r: %s", filename, type(e).__name__)
        raise _bad_request(RESUME_UNREADABLE)

    text = _clean(text)
    if len(text) < MIN_RESUME_CHARS:
        raise _bad_request(RESUME_UNREADABLE)
    return text, filename
