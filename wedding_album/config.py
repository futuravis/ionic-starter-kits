"""
Configuration: album sections, the priority rule, and quality weights.

Everything tunable lives here so the pipeline modules stay free of magic
numbers.
"""

from __future__ import annotations

import os
from enum import Enum
from typing import Dict, List


# ============================================================================
# PATHS
# ============================================================================

# Default collection to organize. Overridable on the command line.
DEFAULT_SOURCE_DIR = os.path.expanduser("~/Desktop/A.4 Get Ready Outdoor")

# Album folders are versioned: BestImages_v1, BestImages_v2, and so on. Runs
# therefore never overwrite each other, so you can change a threshold and
# compare the two results side by side instead of losing the first.
OUTPUT_DIR_PREFIX = "BestImages"

# The folder actually written. organizer.next_version_name() suggests the next
# free version, and the web UI pre-fills it.
OUTPUT_DIR_NAME = f"{OUTPUT_DIR_PREFIX}_v1"

# Where per-collection state (person clusters, labels, cached metrics) is kept.
# Lives inside the source folder so a collection stays self-describing and can
# be re-run without redoing the expensive work.
STATE_DIR_NAME = ".album_state"

# Directory holding the downloaded ONNX face models.
MODELS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "models")


# ============================================================================
# SCANNING
# ============================================================================

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".bmp", ".webp"}

# Directory names skipped while scanning. These are either our own output
# (re-ingesting it would double-count photos) or non-photo material.
EXCLUDED_DIR_NAMES = {
    OUTPUT_DIR_NAME.lower(),
    STATE_DIR_NAME.lower(),
    # The face crops you rename to name people. These are pictures of faces cut
    # out of the collection, so scanning them would feed the tool its own output
    # and count every guest as an extra photograph.
    "name_these_people",
    "best",              # output of the earlier per-person organizer
    "selected_images",   # output of the old flat curator
    "2nd_option",
    "vedio",             # (sic) as it appears in the collection
    "video",
    "__pycache__",
}

# Any folder starting with one of these is skipped too. Needed because album
# folders are versioned: BestImages_v1, BestImages_v2 and every later run must
# all be ignored, or the second run would ingest the first one's output and
# count every photograph twice.
EXCLUDED_DIR_PREFIXES = (
    OUTPUT_DIR_PREFIX.lower(),
)


# ============================================================================
# ROLES
# ============================================================================

class Role(str, Enum):
    """
    The relationship a labeled person has to the wedding.

    This is the only thing the tool needs to know about a person. A finer
    relation such as "Sister" or "Mother-in-law" is kept as a free-text note
    on the person and never affects which section a photo goes to.
    """

    BRIDE = "bride"
    GROOM = "groom"
    FAMILY = "family"
    FRIEND = "friend"
    KID = "kid"

    # Not a person at all. The face detector legitimately fires on statues,
    # idols, portraits on a wall, faces on a poster and printed photographs.
    # The test collection produced a brass Ganesha idol as a confident,
    # perfectly cohesive "person" across three frames. Marking such a cluster
    # IGNORE drops its faces from every decision, so the photos containing it
    # are judged on the real people in them.
    IGNORE = "ignore"

    UNKNOWN = "unknown"   # detected but not yet reviewed


# Roles a user may assign during labeling.
ASSIGNABLE_ROLES = [
    Role.BRIDE, Role.GROOM, Role.FAMILY, Role.FRIEND, Role.KID, Role.IGNORE,
]

# Roles that contribute nothing to section assignment.
NON_PERSON_ROLES = {Role.IGNORE, Role.UNKNOWN}


# ============================================================================
# ALBUM SECTIONS
# ============================================================================

class Section(str, Enum):
    BRIDE = "01_Bride"
    GROOM = "02_Groom"
    COUPLE = "03_Couple"
    FAMILY = "04_Family"
    FRIENDS = "05_Friends"
    KIDS = "06_Kids"
    GROUP = "07_Group"

    # No face was found in this photo.
    #
    # Named for what is actually known, which is less than one would like. It
    # does contain the object and detail shots - the pillaiyar, the panai, the
    # oil bottles, the cake - but it also contains people photographed in
    # profile, from behind, or with their face turned away, because a face
    # detector finding nothing is not evidence that nobody is there.
    #
    # Checking the first twelve of these on the test collection, roughly half
    # were genuine object shots and half were people whose faces were not
    # visible. Separating the two needs a detector that can see bodies and
    # objects, not only faces; until then the folder is named honestly rather
    # than called Objects.
    NO_FACE = "08_No_Face_Found"

    # Real people who have not been identified yet. Held back rather than
    # guessed at; labelling them moves these photos to a proper section.
    UNSORTED = "00_Unsorted"


# Folder creation order for the finished album.
SECTION_ORDER: List[Section] = [
    Section.BRIDE,
    Section.GROOM,
    Section.COUPLE,
    Section.FAMILY,
    Section.FRIENDS,
    Section.KIDS,
    Section.GROUP,
    Section.NO_FACE,
    Section.UNSORTED,
]


# ============================================================================
# THE PRIORITY RULE
# ============================================================================
#
# A photo often contains several kinds of people at once. An album designer
# places such a photo on exactly one page, so we pick exactly one section.
#
# The ordering below is the resolution of a conflict in the original spec: the
# numbered wishlist put Couple above Family, while the stated rule said
# "Family/Friends/Kids take priority over Couple/Solo when those people are
# present" with the worked example "Rekha + Siva + Mom -> Family". The worked
# example is the clearer statement of intent, so relationship groups outrank
# the couple and solo portraits.
#
# Read as: walk down the list, take the first section whose test passes.
#
#   1. KIDS     - a child is present
#   2. FAMILY   - any family member is present
#   3. FRIENDS  - any friend is present
#   4. COUPLE   - bride and groom together, nobody else labeled
#   5. BRIDE    - bride alone
#      GROOM    - groom alone
#   6. GROUP    - several people, none of them labeled
#
# Flip KIDS/FAMILY/FRIENDS below COUPLE here if you'd rather a couple shot win
# over the family it contains.

SECTION_PRIORITY: List[Section] = [
    Section.KIDS,
    Section.FAMILY,
    Section.FRIENDS,
    Section.COUPLE,
    Section.BRIDE,
    Section.GROOM,
    Section.GROUP,
    Section.NO_FACE,
]

# A photo with at least this many detected people, none of them labeled,
# counts as a group shot.
GROUP_MIN_PEOPLE = 3


# ============================================================================
# LOW QUALITY
# ============================================================================
#
# The weakest slice of a collection is set aside before picking, so a soft or
# badly exposed frame cannot win a pick simply by being the only photo of its
# pose.
#
# Expressed as a percentile rather than a fixed score because quality is scored
# relative to the collection: 0.3 means something quite different in a sharp
# studio set and a dim reception. The bottom 10% is deliberately modest - it is
# meant to catch the obviously unusable, not to curate.
#
# Set to 0 to disable.
LOW_QUALITY_PERCENTILE = 10.0


# ============================================================================
# UNSORTED, BROKEN DOWN BY WHO RECURS
# ============================================================================
#
# Unsorted holds photos of real people who have not been named yet, and as one
# flat folder it is useless: nothing tells you which stranger is worth naming
# first. So it is filed by the most-photographed unidentified person in each
# frame.
#
# A face appearing in this many photos across the collection is a recurring
# figure - a relative or close friend - and gets a folder of their own. Naming
# them once moves every one of those photos into a proper section. On the test
# collection there were fourteen such faces, the largest appearing in 210
# photos.
UNSORTED_COMMON_MIN_PHOTOS = 20

# Everyone else - the guest who wanders through two frames - goes here. Half the
# clusters in the test collection appeared in one or two photos, and they do not
# each deserve a folder.
UNSORTED_OTHERS_DIR_NAME = "Others"


# ============================================================================
# QUALITY SCORING
# ============================================================================

# Longest edge used for analysis. Full-resolution decoding is the slowest part
# of the pipeline and the metrics are stable well below native size.
ANALYSIS_MAX_DIM = 1200

# Weights must sum to 1.0. Face-aware signals are deliberately absent here:
# quality answers "is this a good photograph", and who is in it is handled
# separately by the section logic.
QUALITY_WEIGHTS: Dict[str, float] = {
    "sharpness": 0.35,    # Laplacian variance - the strongest keeper signal
    "exposure": 0.18,     # penalize blown highlights and muddy shadows
    "contrast": 0.18,     # dynamic range
    "saturation": 0.17,   # colour vibrancy
    "noise": 0.12,        # estimated sensor noise, inverted
}

# Metrics where a larger raw value means a worse photo.
INVERTED_METRICS = {"noise"}

# Robust normalization range. Min/max normalization lets one freak outlier
# squash every other image into a narrow band, so we clip to these percentiles
# before scaling.
NORMALIZE_PERCENTILES = (5.0, 95.0)


# ============================================================================
# DUPLICATE DETECTION
# ============================================================================

# Perceptual hash size and the Hamming distance under which two photos are
# treated as the same shot. Wedding shoots are full of near-identical bursts.
#
# PHASH_SIZE is the side of the DCT block kept, so the hash is SIZE*SIZE bits:
# 16 means 256 bits, not the 64 bits that most published thresholds assume. A
# threshold of ~12 is the familiar figure for a 64-bit hash and is meaningless
# here; at 256 bits nothing in the test collection came within 48.
#
# 60 is calibrated against measured pairs in the test collection rather than
# taken from a reference:
#
#     48   2108/2109   identical pose, a burst        -> duplicate
#     58   2106/2108   same setup, hand moved         -> duplicate
#     78   2115/2130   same outfit, different pose    -> keep both
#     82   2103/2105   same setup, different pose     -> keep both
#     126  median across all pairs (random is 128)
#
# So the line sits between 58 and 78: below it is the same photograph twice,
# above it is a pose a couple would want in the album. Duplicates are only ever
# flagged, never dropped, unless organize is asked to skip them.
PHASH_SIZE = 16
DUPLICATE_HASH_THRESHOLD = 60


# ============================================================================
# SHOT GROUPS AND PICKING
# ============================================================================
#
# Relationship sections answer "whose page is this photo on". They do not
# answer "which of these ten bride portraits do I actually print", and taking
# the three highest-scoring photos is a poor answer: a burst of one good pose
# sweeps the top three places and you end up with the same picture three times.
#
# So within each section, photos are first grouped by how alike they look -
# same pose, same background, same moment - and picks are then taken one per
# group. Three picks become three genuinely different photographs.
#
# The threshold is looser than DUPLICATE_HASH_THRESHOLD because it answers a
# looser question. From the same measured pairs:
#
#     48   identical pose                 -> same shot group, and a duplicate
#     58   hand moved slightly            -> same shot group
#     78   same outfit, different pose    -> same shot group
#     82   same setup, different pose     -> same shot group
#     90   similar framing                -> same shot group
#     126  median across all pairs        -> different shot
#
# 100 keeps a single setup together while separating genuinely different
# scenes, so "best of each group" spreads picks across the setups you shot.
SHOT_GROUP_THRESHOLD = 100

# Two photos must also be close in time to count as the same shot.
#
# Appearance alone is not enough, and this is the case that proves it: the
# couple photographed during the portraits at 18:23 and photographed again on
# stage at 21:57 can look almost identical - same clothes, same pose, same two
# people - while being two different moments of the evening. Both belong in the
# album. Grouping on looks alone would treat the second as a repeat of the first
# and discard it.
#
# So a shot group is now "looks alike AND happened together". Four minutes is
# comfortably wider than a burst or a re-take, and far narrower than the gap
# between two stages of a wedding.
SHOT_GROUP_WINDOW_MINUTES = 4.0

# Order the picks inside each section by capture time rather than by score.
#
# An album is a story. Reading 001, 002, 003 should walk forward through the
# evening; ranking them by quality would shuffle the evening into an order that
# never happened. The best photo of each moment is still the one chosen - only
# the numbering follows the clock.
ORDER_PICKS_BY_TIME = True

# How many photos to pick per section when using an absolute count.
DEFAULT_PICKS_PER_SECTION = 3

# Preferred way to size a selection: a percentage of what is actually there.
#
# A fixed count does not survive contact with a real wedding. Three per section
# is right for a 25-photo get-ready set and absurd for the 809-photo reception,
# where it throws away almost everything. A percentage scales: 10% of 100 keeps
# 10, 10% of 6 keeps 1.
#
# Set to 0 to go back to the absolute count.
DEFAULT_PICKS_PERCENT = 10.0

# Never leave a section empty just because it is small: a ceremony with four
# photos of the couple should still contribute one.
MIN_PICKS_PER_SECTION = 1

# Duplicates are discarded before anything else is decided, rather than being
# kept as alternatives. A real shoot is full of burst frames: the 1099-photo
# test collection contained 210 of them, a fifth of everything. Clearing them
# first shrinks every later decision. Set False to keep them as runners-up.
DROP_DUPLICATES = True

# How many runners-up to keep for each pick.
#
# Only photos from a group that actually produced a pick become alternatives,
# and only this many from each. Copying every unpicked photo would rebuild the
# original collection under a new name: on the test set, 123 picks would have
# dragged 766 alternatives along with them. One second option per pick is what
# the folder name promises and what a reviewer can actually look through.
#
# 0 writes picks only.
ALTERNATES_PER_PICK = 1

# ============================================================================
# ALBUM LAYOUT
# ============================================================================
#
# Every decision the tool makes is visible as a folder, so the classification
# can be judged by looking rather than by reading a CSV:
#
#     BestImages_vN/
#     ├── 1_Best/
#     │   ├── 1st_Option/<section>/     the picks
#     │   └── 2nd_Option/<section>/     runner-up for each pick
#     ├── 2_Duplicates/<section>/       burst frames that were cleared out
#     ├── 3_Low_Quality/                soft, shaken or badly exposed
#     ├── 4_Objects/                    cake, decor, idols - no people
#     └── _album_report.csv
#
# Nothing is hidden. If a photo was rejected you can see which bin it went to
# and why, which is the only way to tell whether the thresholds are right.

BEST_DIR_NAME = "1_Best"
FIRST_OPTION_DIR_NAME = "1st_Option"
SECOND_OPTION_DIR_NAME = "2nd_Option"
DUPLICATES_DIR_NAME = "2_Duplicates"
LOW_QUALITY_DIR_NAME = "3_Low_Quality"
NO_FACE_DIR_NAME = "4_No_Face_Found"


# ============================================================================
# FLAT ALBUM LAYOUT
# ============================================================================
#
# One album tree for the whole wedding, exactly three levels deep:
#
#     BestImages_v1/
#     ├── 1_Reception_Eve/
#     │   ├── 1_Getting_Ready/
#     │   │   ├── Top/       the picks
#     │   │   ├── Review/    runners-up, worth a look
#     │   │   └── Others/    duplicates, soft frames, no-face
#     │   └── 2_Couple_Portraits/ ...
#     └── 2_Muhurtham/ ...
#
# This replaces a layout that reached eight levels - collection, event, phase,
# shoot, BestImages, 1_Best, 1st_Option, section - before a single photograph.
# Opening one took seven clicks, which makes an album nobody browses.
#
# The event and phase come from the first two folders of the source path, so the
# album mirrors the way the day was already organised rather than inventing its
# own scheme. Which section a photo belongs to survives in its filename
# (001_Couple_7Y4A2149.JPG) instead of costing another level.

# Set False for the older per-shoot layout.
FLAT_ALBUM = True

TOP_DIR_NAME = "Top"
REVIEW_DIR_NAME = "Review"
OTHERS_DIR_NAME = "Others"

# How many photos land in Top for each phase. The count applies to the phase as
# a whole - Getting Ready is one page of the album, not four.
PICKS_PER_GROUP = 5

# The review bins exist to be looked through, not printed, so they get
# downscaled previews instead of full-resolution copies. A wedding's rejects
# would otherwise outweigh its album: 213 duplicates at 8 MB each is 1.7 GB to
# store photos you have already decided against. The filename still names the
# original, and the CSV records its full path, so anything can be recovered.
PREVIEW_REVIEW_BINS = True
REVIEW_PREVIEW_MAX_DIM = 1600


# ============================================================================
# FACE RECOGNITION
# ============================================================================

# Model files, downloaded from the OpenCV model zoo. These are plain weight
# files run by OpenCV's own DNN engine: everything happens locally on the CPU,
# with no API key, no account and no photo ever leaving the machine.
YUNET_MODEL = os.path.join(MODELS_DIR, "face_detection_yunet_2023mar.onnx")
SFACE_MODEL = os.path.join(MODELS_DIR, "face_recognition_sface_2021dec.onnx")

YUNET_URL = (
    "https://github.com/opencv/opencv_zoo/raw/main/models/"
    "face_detection_yunet/face_detection_yunet_2023mar.onnx"
)
SFACE_URL = (
    "https://github.com/opencv/opencv_zoo/raw/main/models/"
    "face_recognition_sface/face_recognition_sface_2021dec.onnx"
)

# Longest edge the detector sees. Larger finds more small/background faces at
# the cost of time; 1600 reliably catches subjects in a 6000px frame.
FACE_DETECT_MAX_DIM = 1600

# Detector confidence floor and non-maximum-suppression overlap.
#
# 0.50 rather than something stricter, accepting a known tradeoff.
#
# At 0.85 the detector found only the two obvious subjects per frame. Dropping
# to 0.50 additionally surfaces faces in the 0.5-0.7 band, which in the test
# collection turned out to be a brass Ganesha idol (7Y4A2136/2139/2141) and one
# blurred patch of background - not people. A control helped rule out general
# over-detection: a known two-person shot (7Y4A2094) stayed at exactly two
# faces even with the floor at 0.30.
#
# The low floor is kept anyway, because a full collection contains real guests
# in profile or turned away from the camera who score in that same band, and
# missing a relative costs more than surfacing a statue. Non-person clusters
# are therefore an *expected* output, handled by labeling them Role.IGNORE
# during review rather than by tuning them away. An unlabeled cluster can
# never land a photo in a named section regardless.
FACE_SCORE_THRESHOLD = 0.50
FACE_NMS_THRESHOLD = 0.3
FACE_TOP_K = 500

# Smallest face worth treating as a subject rather than a background bystander,
# as a fraction of the image's longest edge.
FACE_MIN_SIZE_RATIO = 0.02

# A face at least this large (same units) is a *prominent* subject. Used later
# to decide whether a child is the focus of a photo or merely in it.
FACE_PROMINENT_SIZE_RATIO = 0.08

# Cosine similarity above which two face embeddings are the same person.
#
# 0.363 is the figure the OpenCV model zoo recommends for SFace, and it is the
# right figure for the job it was measured on: deciding whether two photographs
# show the same person, where a wrong yes is the expensive mistake.
#
# Clustering a wedding is a different job. Here a threshold that is too strict
# does not produce a wrong answer, it produces a fragmented one: the same guest
# becomes four clusters, and you are asked to name them four times. Measured on
# the 1099-photo collection:
#
#     threshold   people   largest clusters              seen once or twice
#       0.20        32     485 352 349 347 292                   4
#       0.28        44     453 307 255 227 210                   8
#       0.363       99     447 442 210 187 186                  49
#       0.45       322     435 377 158 158 155                 230
#
# I briefly lowered this to 0.28 on the strength of the cluster count: 0.363
# produced 99 clusters where 0.28 produced 44, which is closer to the number of
# people who were actually at the wedding. That reasoning was wrong, and it is
# worth recording why.
#
# Cluster count cannot tell fragmentation apart from merging. Fewer clusters is
# equally consistent with two guests collapsed into one - which is the worse
# error, since it files one person's photographs under another's name. Cohesion,
# how tightly a cluster's faces sit around their own centre, is what separates
# the two. Measuring both:
#
#   threshold  people  mean cohesion  worst of top 10  impure(<0.60)  singletons
#     0.24       36        0.568          0.462            18             5
#     0.28       44        0.607          0.503            23             8
#     0.363      99        0.627          0.521            21            49
#     0.45      322        0.751          0.548             9           230
#     0.50      523        0.791          0.562             6           404
#
# At 0.28, two clusters of genuinely different people (p02 and p10) shared 33
# photographs, and within those photographs their faces scored 0.07-0.22 against
# each other - plainly different people - while their cluster centroids scored
# 0.614. Both clusters were mixtures.
#
# The deciding column is "worst of top 10": it never rises above 0.56 at any
# threshold. The largest clusters stay impure however this is tuned, so the limit
# is the clustering method, not this number. Greedy assignment against a running
# centroid drifts - one wrong face shifts the centre, which admits more wrong
# faces - and no threshold repairs that.
#
# So this is back to 0.363, the value the SFace authors measured, chosen now for
# the honest reason that nothing better is available without replacing the
# clustering itself with something purity-preserving such as average-linkage
# agglomerative clustering.
#
# Two consequences to live with:
#   - One person can still form several clusters, especially in different states
#     such as the groom in a turban and bare-headed. Give each crop the same name
#     during labelling and they become one person.
#   - Roughly half the clusters hold one or two faces. Those are passers-by;
#     leaving them unnamed costs nothing.
FACE_MATCH_THRESHOLD = 0.363


# ============================================================================
# RUNTIME OVERRIDES
# ============================================================================
#
# The web UI lets you change thresholds and weights per run. Rather than
# threading a settings object through every function, those runs rebind the
# module attributes below.
#
# The catch worth knowing: worker processes re-import this module, so rebinding
# in the parent does NOT reach them. pipeline.py passes the same overrides to a
# ProcessPoolExecutor initializer for exactly that reason.

# Only these may be overridden at runtime. Anything else is a typo, and a typo
# that silently did nothing would be worse than an error.
OVERRIDABLE = {
    "FACE_DETECT_MAX_DIM",
    "FACE_SCORE_THRESHOLD",
    "FACE_NMS_THRESHOLD",
    "FACE_MIN_SIZE_RATIO",
    "FACE_PROMINENT_SIZE_RATIO",
    "FACE_MATCH_THRESHOLD",
    "DUPLICATE_HASH_THRESHOLD",
    "GROUP_MIN_PEOPLE",
    "ANALYSIS_MAX_DIM",
    "QUALITY_WEIGHTS",
    "OUTPUT_DIR_NAME",
}


class ConfigError(ValueError):
    """An override that cannot be applied."""


def apply_overrides(overrides: Dict[str, object]) -> Dict[str, object]:
    """
    Rebind config values for this run. Returns the previous values so a caller
    can restore them.
    """
    previous: Dict[str, object] = {}

    for key, value in (overrides or {}).items():
        if key not in OVERRIDABLE:
            raise ConfigError(f"{key} is not overridable")

        current = globals()[key]

        if key == "QUALITY_WEIGHTS":
            if not isinstance(value, dict):
                raise ConfigError("QUALITY_WEIGHTS must be a mapping")
            unknown = set(value) - set(current)
            if unknown:
                raise ConfigError(f"unknown quality weight(s): {', '.join(sorted(unknown))}")
            merged = {**current, **{k: float(v) for k, v in value.items()}}
            total = sum(merged.values())
            if total <= 0:
                raise ConfigError("quality weights must sum to more than zero")
            # Renormalize so the UI's sliders need not add up to exactly 1.0.
            value = {k: v / total for k, v in merged.items()}
        elif isinstance(current, bool):
            value = bool(value)
        elif isinstance(current, int) and not isinstance(current, bool):
            value = int(value)
        elif isinstance(current, float):
            value = float(value)
        elif isinstance(current, str):
            value = str(value)

        previous[key] = current
        globals()[key] = value

    return previous


def snapshot() -> Dict[str, object]:
    """Current values of everything the UI is allowed to change."""
    return {key: globals()[key] for key in sorted(OVERRIDABLE)}


def validate() -> None:
    """Fail fast on a mis-edited config rather than silently misbehaving."""
    total = sum(QUALITY_WEIGHTS.values())
    if abs(total - 1.0) > 1e-6:
        raise ValueError(f"QUALITY_WEIGHTS must sum to 1.0, got {total:.6f}")

    missing = set(SECTION_PRIORITY) ^ (set(SECTION_ORDER) - {Section.UNSORTED})
    if missing:
        raise ValueError(f"SECTION_PRIORITY and SECTION_ORDER disagree on: {missing}")
