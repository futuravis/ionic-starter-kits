"""
Small HTTP helpers shared by every front end.

Lives here rather than in app.py so photo_studio and photo_funnel can use it
without reaching into another module's private names.
"""

from __future__ import annotations

from email.parser import BytesParser
from email.policy import HTTP
from typing import List, Tuple

def parse_multipart(body: bytes, content_type: str) -> List[Tuple[str, bytes]]:
    """
    Pull (filename, data) pairs out of a multipart/form-data body.

    Uses the stdlib email parser rather than `cgi`, which is deprecated and
    removed in Python 3.13. The body is handed over with a synthesized header
    block so the parser sees a well-formed MIME document.
    """
    header = f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n"
    message = BytesParser(policy=HTTP).parsebytes(header.encode("latin-1") + body)

    if not message.is_multipart():
        return []

    out: List[Tuple[str, bytes]] = []
    for part in message.iter_parts():
        filename = part.get_filename()
        if not filename:
            continue
        payload = part.get_payload(decode=True)
        if payload:
            out.append((filename, payload))
    return out
