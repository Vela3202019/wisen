"""
Reads notes and documents from a configured folder.
Supports: .md, .txt, .rst, .pdf, .docx, .html

Configure via NOTES_FOLDER env var (default: ~/Developer/learnings).
Works with Obsidian vaults, Notion exports, local wikis, or any notes folder.
"""
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import AsyncIterator

_DEFAULT_NOTES_FOLDER = Path.home() / "Developer" / "learnings"

_SUPPORTED_EXTENSIONS = {".md", ".txt", ".rst", ".pdf", ".docx", ".html"}
_SKIP_DIRS = {"node_modules", ".git", ".claude", "__pycache__", ".obsidian"}


class NoteArtifact:
    def __init__(self, file_path: Path, root: Path):
        self.file_path = file_path
        rel = file_path.relative_to(root)
        parts = rel.parts
        self.topic_folder = parts[0] if len(parts) > 1 else "root"
        self.filename = file_path.name
        self.extension = file_path.suffix.lower()
        self.content = _extract_text(file_path)
        stat = file_path.stat()
        self.date_modified = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)

    def to_dict(self) -> dict:
        return {
            "file_path": str(self.file_path),
            "topic_folder": self.topic_folder,
            "filename": self.filename,
            "extension": self.extension,
            "content": self.content,
            "date_modified": self.date_modified,
            "embedding": [],
            "topics": [],
            "ingested_at": datetime.now(timezone.utc),
        }


def _extract_text(path: Path) -> str:
    ext = path.suffix.lower()

    if ext in (".md", ".txt", ".rst"):
        return path.read_text(encoding="utf-8", errors="replace")

    if ext == ".html":
        return _extract_html(path)

    if ext == ".pdf":
        return _extract_pdf(path)

    if ext == ".docx":
        return _extract_docx(path)

    return ""


def _extract_html(path: Path) -> str:
    try:
        from html.parser import HTMLParser

        class _Stripper(HTMLParser):
            def __init__(self):
                super().__init__()
                self._parts: list[str] = []
                self._skip = False

            def handle_starttag(self, tag, attrs):
                if tag in ("script", "style"):
                    self._skip = True

            def handle_endtag(self, tag):
                if tag in ("script", "style"):
                    self._skip = False

            def handle_data(self, data):
                if not self._skip:
                    self._parts.append(data)

        stripper = _Stripper()
        stripper.feed(path.read_text(encoding="utf-8", errors="replace"))
        return " ".join(stripper._parts).strip()
    except Exception:
        return ""


def _extract_pdf(path: Path) -> str:
    try:
        import pypdf
        reader = pypdf.PdfReader(str(path))
        pages = [page.extract_text() or "" for page in reader.pages]
        return "\n\n".join(pages).strip()
    except ImportError:
        return f"[PDF: install pypdf to extract text from {path.name}]"
    except Exception:
        return ""


def _extract_docx(path: Path) -> str:
    try:
        import docx
        doc = docx.Document(str(path))
        return "\n\n".join(p.text for p in doc.paragraphs if p.text.strip())
    except ImportError:
        return f"[DOCX: install python-docx to extract text from {path.name}]"
    except Exception:
        return ""


async def ingest(folder: str | None = None) -> AsyncIterator[NoteArtifact]:
    root = Path(folder) if folder else Path(
        os.getenv("NOTES_FOLDER", str(_DEFAULT_NOTES_FOLDER))
    )
    if not root.exists():
        return

    for file in root.rglob("*"):
        if not file.is_file():
            continue
        if file.suffix.lower() not in _SUPPORTED_EXTENSIONS:
            continue
        if any(part in _SKIP_DIRS or part.startswith(".") for part in file.parts):
            continue
        try:
            artifact = NoteArtifact(file_path=file, root=root)
            if artifact.content.strip():
                yield artifact
        except Exception:
            continue
