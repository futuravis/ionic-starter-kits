"""
Photo Shortlist: drop in 20-50 photos, get back the best 3-5.

A deliberately small application. It answers one question - "which of these
shots should we keep?" - and nothing else. No folder conventions, no people
labelling, no album tree. Upload, review, download.

The scoring itself is not reimplemented here. `wedding_album.quality` already
measures sharpness, exposure, contrast, saturation and noise and normalizes
them across a collection, and that code is tested and working; this package
reuses it and adds only the two things it needs on top:

  * shortlist.py - group near-identical frames, then pick across the groups
  * app.py       - an upload page and a JSON API over the top

Run it with:

    python3 -m photo_shortlist

Security, stated plainly: there is no authentication. The server binds to
127.0.0.1, so only this machine can reach it, and that loopback binding is the
entire security boundary. Uploads land in a per-session temp directory and
image requests are resolved and confined to it, so a crafted URL cannot read
arbitrary files from disk. Do not expose this on a shared network.
"""

# Repository release version. The three packages here ship together as one
# artifact, so they carry one number rather than drifting apart.
__version__ = "0.03"
