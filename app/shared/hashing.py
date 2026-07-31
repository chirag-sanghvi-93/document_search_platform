"""Content hashing — the identity of a document.

⚠️ This lives in `shared/`, not in `engine/ingest/`, for a reason discovered at
runtime rather than by design.

`hash_file` originally sat in `engine/ingest/parse.py`. When the upload endpoint
imported it, that pulled `parse.py`'s module-level `import docling` into the API
process — and the backend image does not install the ingestion extras, because
parsing is the worker's job. The backend crashed on startup with
`ModuleNotFoundError: No module named 'docling'`.

The dependency was never real: hashing a file is `hashlib` and nothing else. But
a function's imports travel with it, so *where* a helper lives decides what a
caller is forced to install. Anything both the API and the engine need belongs
here, where the import list is small enough to be safe for either.
"""

from __future__ import annotations

import hashlib
from pathlib import Path


def hash_file(path: Path) -> str:
    """SHA-256 of the file's bytes.

    Content, not filename: a renamed-but-unchanged file must be recognised as the
    same document, and a changed-but-identically-named file must not be. This is
    what the upload endpoint checks before queueing any work, and what ingestion
    uses to skip documents it has already processed.
    """
    return hashlib.sha256(path.read_bytes()).hexdigest()
