"""
Photo Studio: a website for reviewing a shoot.

Same engine, different front end. Where `photo_shortlist` is a single dark page
built to answer one question quickly, this is a light, gallery-style site built
for working through a shoot properly: a three-step flow, a sortable table as
well as a gallery, a lightbox for judging a frame at size, and side-by-side
comparison within a burst.

Nothing about the analysis is reimplemented here. The measurement, the fault
diagnosis and the ranking all come from `photo_shortlist`:

    photo_shortlist.metrics     noise-robust sharpness, face probing
    photo_shortlist.diagnose    turning measurements into named faults
    photo_shortlist.shortlist   ranking, bundling, round-robin picking

The upload, thumbnail and export plumbing is reused too, from
`photo_shortlist.app`. That means both front ends share one in-memory batch, so
run one at a time; starting both and uploading to each would have them treading
on each other.

    python3 -m photo_studio

Security, stated plainly: there is no authentication. The server binds to
127.0.0.1 and that loopback binding is the entire security boundary. Uploads
live in a temporary directory and image requests are confined to it, so a
crafted URL cannot read arbitrary files. Do not expose this on a shared
network.
"""

# Repository release version. The three packages here ship together as one
# artifact, so they carry one number rather than drifting apart.
__version__ = "0.04"
