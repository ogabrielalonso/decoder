from __future__ import annotations

from pathlib import Path

EXTENSION_TO_LANGUAGE: dict[str, str] = {
    ".py": "python",
    ".pyi": "python",
    ".js": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".jsx": "javascript",
    ".ts": "typescript",
    ".tsx": "tsx",
    ".go": "go",
    ".rs": "rust",
    ".java": "java",
    ".kt": "kotlin",
    ".kts": "kotlin",
    ".c": "c",
    ".h": "c",
    ".cpp": "cpp",
    ".cc": "cpp",
    ".cxx": "cpp",
    ".hpp": "cpp",
    ".hh": "cpp",
    ".rb": "ruby",
    ".cs": "csharp",
    ".php": "php",
    ".swift": "swift",
    ".scala": "scala",
    ".sh": "bash",
    ".bash": "bash",
    ".zsh": "bash",
    ".json": "json",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".toml": "toml",
    ".md": "markdown",
    ".html": "html",
    ".css": "css",
    ".sql": "sql",
}

# Languages that the static analyzer has symbol/import extraction support for.
ANALYZED_LANGUAGES: set[str] = {
    "python", "javascript", "typescript", "tsx", "go", "rust", "java", "kotlin",
}

# Binary or otherwise non-textual extensions we skip entirely.
BINARY_EXTENSIONS: set[str] = {
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".ico",
    ".webp",
    ".pdf",
    ".zip",
    ".tar",
    ".gz",
    ".bz2",
    ".7z",
    ".mp3",
    ".mp4",
    ".mov",
    ".wav",
    ".woff",
    ".woff2",
    ".ttf",
    ".eot",
    ".otf",
    ".so",
    ".dll",
    ".dylib",
    ".exe",
    ".bin",
    ".class",
    ".o",
    ".a",
}


def detect_language(filename: str) -> str | None:
    lower = filename.lower()
    for ext, lang in EXTENSION_TO_LANGUAGE.items():
        if lower.endswith(ext):
            return lang
    return None


def is_binary(filename: str) -> bool:
    lower = filename.lower()
    return any(lower.endswith(ext) for ext in BINARY_EXTENSIONS)


def is_probably_text(path: Path, sniff_bytes: int = 8192) -> bool:
    """True if the file looks textual (decodable head, no NUL bytes).

    Lets the pipeline include ANY extension that is actually text: not just the
    known ones: so nothing textual is silently dropped. Binary content (NUL
    bytes / undecodable) returns False so it routes to the asset inventory.
    """
    try:
        head = path.read_bytes()[:sniff_bytes]
    except OSError:
        return False
    if b"\x00" in head:
        return False
    try:
        head.decode("utf-8")
    except UnicodeDecodeError:
        try:
            head.decode("latin-1")
        except UnicodeDecodeError:
            return False
    return True
