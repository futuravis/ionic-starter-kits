"""
Photo Funnel: a shoot reviewed in twelve steps, narrowing to one photograph.

    1 Add            2 Straighten      3 Duplicates     4 Exposure
    5 Focus          6 Subject         7 Expressions    8 Grain
    9 Narrow        10 People         11 Per moment    12 The best

Each step removes photos for one stated reason, exposes the thresholds it
uses, and shows exactly which frames it took. Anything removed stays
reachable, and can be claimed into a separate manual selection that never
alters the funnel's own result.

The design was agreed as docs/mockups/funnel_v4.html before any of this was
written.

    python3 -m photo_funnel

Architecture, in one paragraph: photos are measured once and cached by path,
modification time and size (engine.Cache). Every step after that is a pure
function of those measurements and the current parameters (engine.run_funnel),
so moving a slider or stepping backwards recomputes in milliseconds rather than
re-reading 8 GB of JPEGs. Measurement itself is not reimplemented: it comes
from photo_shortlist.metrics, which reuses wedding_album.quality.

Security, stated plainly: there is no authentication. The server binds to
127.0.0.1 and that loopback binding is the entire security boundary. The
export step writes to a folder you choose, which is the point of it; nothing
else writes outside the session's temporary working directory, and nothing
ever modifies or deletes the photographs you started with.
"""

__version__ = "0.04"
