"""
Wedding Image Curator
=====================
Selects the best N images from a large set based on:
1. Technical quality (sharpness, exposure, contrast, saturation, noise)
2. Near-duplicate detection (perceptual hashing to avoid picking similar shots)
3. Face detection bonus (wedding photos with faces tend to be keepers)

Usage:
    python image_curator.py

Output:
    - Creates a "selected_images" folder on Desktop with the top 400 images (copied)
    - Generates a CSV report with scores for all images
"""

import os
import sys
import csv
import shutil
import hashlib
from pathlib import Path
from dataclasses import dataclass, field
from concurrent.futures import ProcessPoolExecutor, as_completed
from typing import List, Tuple

import cv2
import numpy as np
from PIL import Image
import imagehash

# ============================================================================
# CONFIGURATION
# ============================================================================

# Source folder containing wedding images
SOURCE_DIR = os.path.expanduser("~/Desktop/wedding images")

# Output folder for selected images
OUTPUT_DIR = os.path.expanduser("~/Desktop/selected_images")

# Number of images to select
TARGET_COUNT = 400

# Similarity threshold for duplicate detection (lower = stricter)
# Two images with a hash distance <= this value are considered duplicates
DUPLICATE_HASH_THRESHOLD = 8

# Number of parallel workers for processing
NUM_WORKERS = os.cpu_count() or 4

# Weights for scoring components (tweak these to prioritize different qualities)
WEIGHTS = {
    "sharpness": 0.30,      # Laplacian variance - detects blur
    "exposure": 0.15,       # How well-exposed the image is (penalize over/under)
    "contrast": 0.15,       # Dynamic range of the image
    "saturation": 0.15,     # Color vibrancy (wedding photos should pop)
    "noise": 0.10,          # Lower noise is better
    "face_bonus": 0.15,     # Bonus for detecting faces
}


# ============================================================================
# IMAGE QUALITY METRICS
# ============================================================================

@dataclass
class ImageScore:
    filepath: str
    sharpness: float = 0.0
    exposure: float = 0.0
    contrast: float = 0.0
    saturation: float = 0.0
    noise: float = 0.0
    face_bonus: float = 0.0
    total_score: float = 0.0
    phash: str = ""
    is_duplicate: bool = False


def compute_sharpness(gray: np.ndarray) -> float:
    """Laplacian variance - higher means sharper."""
    laplacian = cv2.Laplacian(gray, cv2.CV_64F)
    return float(laplacian.var())


def compute_exposure(gray: np.ndarray) -> float:
    """
    Score based on how close the mean brightness is to the ideal midpoint (128).
    Returns 0-1 where 1 is perfectly exposed.
    """
    mean_brightness = np.mean(gray)
    # Penalize deviation from ideal midpoint
    deviation = abs(mean_brightness - 128) / 128.0
    return max(0.0, 1.0 - deviation)


def compute_contrast(gray: np.ndarray) -> float:
    """Standard deviation of pixel intensities - higher means more contrast."""
    return float(np.std(gray))


def compute_saturation(img_bgr: np.ndarray) -> float:
    """Mean saturation in HSV space."""
    hsv = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2HSV)
    return float(np.mean(hsv[:, :, 1]))


def compute_noise(gray: np.ndarray) -> float:
    """
    Estimate noise using the median absolute deviation of the Laplacian.
    Lower is better - we return an inverse score.
    """
    # Sigma estimation using MAD of Laplacian
    H, W = gray.shape
    # Use a smaller kernel for noise estimation
    noise_map = cv2.Laplacian(gray, cv2.CV_64F, ksize=3)
    sigma = np.median(np.abs(noise_map)) * 1.4826
    # Invert: lower noise = higher score
    # Cap at reasonable range
    return max(0.0, 100.0 - sigma)


def detect_faces(gray: np.ndarray) -> int:
    """Detect faces using Haar cascades. Returns count of faces found."""
    face_cascade_path = cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
    face_cascade = cv2.CascadeClassifier(face_cascade_path)

    # Resize for faster detection
    scale = 800.0 / max(gray.shape)
    if scale < 1.0:
        small = cv2.resize(gray, None, fx=scale, fy=scale)
    else:
        small = gray

    faces = face_cascade.detectMultiScale(
        small, scaleFactor=1.1, minNeighbors=5, minSize=(30, 30)
    )
    return len(faces)


def compute_phash(filepath: str) -> str:
    """Compute perceptual hash for duplicate detection."""
    try:
        img = Image.open(filepath)
        h = imagehash.phash(img, hash_size=16)
        return str(h)
    except Exception:
        return ""


def score_single_image(filepath: str) -> ImageScore:
    """Compute all quality metrics for a single image."""
    result = ImageScore(filepath=filepath)

    try:
        # Read image
        img_bgr = cv2.imread(filepath)
        if img_bgr is None:
            return result

        # Resize for faster processing (keep aspect ratio)
        max_dim = 1200
        h, w = img_bgr.shape[:2]
        if max(h, w) > max_dim:
            scale = max_dim / max(h, w)
            img_bgr = cv2.resize(img_bgr, None, fx=scale, fy=scale)

        gray = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2GRAY)

        # Compute metrics
        result.sharpness = compute_sharpness(gray)
        result.exposure = compute_exposure(gray)
        result.contrast = compute_contrast(gray)
        result.saturation = compute_saturation(img_bgr)
        result.noise = compute_noise(gray)

        # Face detection
        face_count = detect_faces(gray)
        result.face_bonus = min(face_count * 25.0, 100.0)  # Cap at 100

        # Compute perceptual hash
        result.phash = compute_phash(filepath)

    except Exception as e:
        print(f"  Error processing {os.path.basename(filepath)}: {e}")

    return result


# ============================================================================
# NORMALIZATION & SCORING
# ============================================================================

def normalize_scores(scores: List[ImageScore]) -> List[ImageScore]:
    """Normalize each metric to 0-1 range across all images."""
    if not scores:
        return scores

    # Collect raw values
    metrics = ["sharpness", "contrast", "saturation", "noise", "face_bonus"]

    for metric in metrics:
        values = [getattr(s, metric) for s in scores]
        min_val = min(values)
        max_val = max(values)
        range_val = max_val - min_val

        if range_val > 0:
            for s in scores:
                normalized = (getattr(s, metric) - min_val) / range_val
                setattr(s, metric, normalized)
        else:
            for s in scores:
                setattr(s, metric, 0.5)

    # Exposure is already 0-1, no normalization needed

    # Compute weighted total score
    for s in scores:
        s.total_score = (
            WEIGHTS["sharpness"] * s.sharpness +
            WEIGHTS["exposure"] * s.exposure +
            WEIGHTS["contrast"] * s.contrast +
            WEIGHTS["saturation"] * s.saturation +
            WEIGHTS["noise"] * s.noise +
            WEIGHTS["face_bonus"] * s.face_bonus
        )

    return scores


# ============================================================================
# DUPLICATE DETECTION
# ============================================================================

def mark_duplicates(scores: List[ImageScore]) -> List[ImageScore]:
    """
    Mark near-duplicate images. Among duplicates, keep the one with the
    highest total_score.
    """
    # Sort by score descending so we keep the best of each group
    scores.sort(key=lambda s: s.total_score, reverse=True)

    seen_hashes = []  # List of (hash_obj, index) for kept images

    for score in scores:
        if not score.phash:
            continue

        current_hash = imagehash.hex_to_hash(score.phash)
        is_dup = False

        for seen_hash, _ in seen_hashes:
            distance = current_hash - seen_hash
            if distance <= DUPLICATE_HASH_THRESHOLD:
                is_dup = True
                break

        if is_dup:
            score.is_duplicate = True
        else:
            seen_hashes.append((current_hash, score.filepath))

    return scores


# ============================================================================
# MAIN PIPELINE
# ============================================================================

def find_all_images(source_dir: str) -> List[str]:
    """Recursively find all image files."""
    extensions = {".jpg", ".jpeg", ".png", ".tiff", ".tif", ".bmp"}
    images = []

    for root, dirs, files in os.walk(source_dir):
        # Skip video folder
        if "vedio" in root.lower() or "video" in root.lower():
            continue
        for f in files:
            if f.startswith("."):
                continue
            ext = os.path.splitext(f)[1].lower()
            if ext in extensions:
                images.append(os.path.join(root, f))

    return sorted(images)


def main():
    print("=" * 60)
    print("  WEDDING IMAGE CURATOR")
    print("=" * 60)
    print()

    # Step 1: Find all images
    print(f"[1/5] Scanning for images in: {SOURCE_DIR}")
    all_images = find_all_images(SOURCE_DIR)
    total = len(all_images)
    print(f"       Found {total} images")
    print()

    if total == 0:
        print("No images found. Check the source directory.")
        sys.exit(1)

    # Step 2: Score all images
    print(f"[2/5] Analyzing image quality ({NUM_WORKERS} workers)...")
    print("       This may take a few minutes for large collections...")
    print()

    scores: List[ImageScore] = []
    completed = 0

    with ProcessPoolExecutor(max_workers=NUM_WORKERS) as executor:
        futures = {
            executor.submit(score_single_image, img): img
            for img in all_images
        }

        for future in as_completed(futures):
            result = future.result()
            scores.append(result)
            completed += 1

            if completed % 100 == 0 or completed == total:
                pct = (completed / total) * 100
                print(f"       Progress: {completed}/{total} ({pct:.1f}%)")

    print()

    # Step 3: Normalize and rank
    print("[3/5] Normalizing scores and ranking...")
    scores = normalize_scores(scores)
    print()

    # Step 4: Remove duplicates
    print("[4/5] Detecting and removing near-duplicates...")
    scores = mark_duplicates(scores)
    duplicates = sum(1 for s in scores if s.is_duplicate)
    print(f"       Found {duplicates} near-duplicate images")
    print()

    # Step 5: Select top N and copy
    print(f"[5/5] Selecting top {TARGET_COUNT} images...")

    # Filter out duplicates, then take top N
    unique_scores = [s for s in scores if not s.is_duplicate]
    unique_scores.sort(key=lambda s: s.total_score, reverse=True)

    selected = unique_scores[:TARGET_COUNT]
    print(f"       Selected {len(selected)} images from {len(unique_scores)} unique candidates")
    print()

    # Create output directory
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    # Copy selected images
    print(f"Copying selected images to: {OUTPUT_DIR}")
    for i, score in enumerate(selected, 1):
        src = score.filepath
        # Preserve original filename with a rank prefix for easy browsing
        filename = f"{i:04d}_{os.path.basename(src)}"
        dst = os.path.join(OUTPUT_DIR, filename)
        shutil.copy2(src, dst)

        if i % 50 == 0:
            print(f"       Copied {i}/{len(selected)}")

    print(f"       Done! Copied {len(selected)} images.")
    print()

    # Generate CSV report
    report_path = os.path.join(OUTPUT_DIR, "_scoring_report.csv")
    print(f"Generating scoring report: {report_path}")

    with open(report_path, "w", newline="") as csvfile:
        writer = csv.writer(csvfile)
        writer.writerow([
            "Rank", "Filename", "Total Score", "Sharpness", "Exposure",
            "Contrast", "Saturation", "Noise", "Face Bonus",
            "Is Duplicate", "Selected"
        ])

        # Write all scores for reference
        all_sorted = sorted(scores, key=lambda s: s.total_score, reverse=True)
        selected_paths = {s.filepath for s in selected}

        for rank, s in enumerate(all_sorted, 1):
            writer.writerow([
                rank,
                os.path.basename(s.filepath),
                f"{s.total_score:.4f}",
                f"{s.sharpness:.4f}",
                f"{s.exposure:.4f}",
                f"{s.contrast:.4f}",
                f"{s.saturation:.4f}",
                f"{s.noise:.4f}",
                f"{s.face_bonus:.4f}",
                s.is_duplicate,
                s.filepath in selected_paths,
            ])

    print()
    print("=" * 60)
    print("  COMPLETE!")
    print(f"  Selected: {len(selected)} best images")
    print(f"  Output:   {OUTPUT_DIR}")
    print(f"  Report:   {report_path}")
    print("=" * 60)


if __name__ == "__main__":
    main()
