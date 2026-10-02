"""
Wedding Album Organizer
=======================
Organizes a wedding photo collection into album sections based on the
relationships of the people in each photo, not on isolated per-person folders.

Album structure produced:

    BestImages/
    ├── 01_Bride/     Rekha solo - portraits / getting ready
    ├── 02_Groom/     Siva solo - portraits / getting ready
    ├── 03_Couple/    Rekha + Siva together, nobody else labeled
    ├── 04_Family/    any family member present (both sides)
    ├── 05_Friends/   any friend present
    ├── 06_Kids/      children featured
    └── 07_Group/     large gatherings, nobody specifically labeled

Every photo lands in exactly one section. See wedding_album.config for the
priority order that resolves photos containing several kinds of people.
"""

# Repository release version. This package reached 1.2.0 on its own before the
# two photo apps were built alongside it; all three now ship together as one
# artifact and carry one number. The earlier history is in STORY.md.
__version__ = "0.01"
