"""
Command line entry point.

Built up one subcommand per pipeline stage so each stage can be run and
inspected on its own:

    scan     list the photos that would be processed
    quality  measure and rank photographic quality
"""

from __future__ import annotations

import argparse
import os
import sys
from concurrent.futures import ProcessPoolExecutor
from typing import List

from . import (
    __version__, batch, config, faces, labels, organizer, people, pipeline,
    flat, naming, preview, quality, scanner, sections, selection,
)


# ============================================================================
# shoots
# ============================================================================

def cmd_shoots(args: argparse.Namespace) -> int:
    _rule("SHOOTS")
    print(f"Root: {args.source}\n")

    shoots = scanner.find_shoots(args.source)
    print(scanner.describe_shoots(args.source, shoots))

    if not shoots:
        return 1

    print("\nEach of these gets its own album, written beside its photos.")
    return 0


# ============================================================================
# batch
# ============================================================================

def cmd_batch(args: argparse.Namespace) -> int:
    _rule("BATCH")
    print(f"Root: {args.source}\n")

    if not faces.models_present():
        print("Face models are missing. Run download-models first.", file=sys.stderr)
        return 2

    def progress(stage: str, done: int, total: int) -> None:
        if done == total or done % 50 == 0:
            pct = (done / total * 100) if total else 0
            print(f"    {stage}: {done}/{total} ({pct:.0f}%)")

    shoots = scanner.find_shoots(args.source)
    if not shoots:
        print("No photos found.")
        return 1

    total_photos = sum(len(p) for _, p in shoots)
    print(f"{len(shoots)} shoot(s), {total_photos} photo(s)\n")

    print("Analyzing the whole collection so people stay consistent between shoots...")
    shoots, results, found, scores, phashes, times = batch.analyze_collection(
        args.source, workers=args.workers, progress=progress,
        limit_per_shoot=args.limit,
    )
    print(f"    {sum(r.count for r in results)} faces, {len(found)} people\n")

    unlabeled = [p for p in found if not p.is_labeled]
    if unlabeled and not args.allow_unlabeled:
        print(f"error: {len(unlabeled)} of {len(found)} people are unlabeled.",
              file=sys.stderr)
        print("Label them once and they apply to every shoot:", file=sys.stderr)
        print("    python3 -m wedding_album people   "
              f"--source '{args.source}'", file=sys.stderr)
        print("    python3 -m wedding_album label --interactive "
              f"--source '{args.source}'", file=sys.stderr)
        print("or re-run with --allow-unlabeled.", file=sys.stderr)
        return 2

    shoot_results = batch.prepare_shoots(
        shoots, results, found, scores, phashes,
        percent=args.percent, per_section=args.picks,
        drop_duplicates=not args.keep_duplicates,
        times=times,
    )

    result = batch.BatchResult(root=args.source, shoots=shoot_results, people=found)
    print(batch.summarize(result))

    if args.flat:
        by_shoot = {s.folder: s.assignments for s in shoot_results}
        albums = flat.build(
            args.source, by_shoot, phashes, times=times,
            picks_per_group=args.top,
            percent=args.percent if args.use_percent else 0.0,
        )
        print()
        print(flat.summarize(albums))

        if args.dry_run:
            print("\nDry run: nothing was written.")
            return 0

        output_name = organizer.target_version_name(
            args.source, clean=args.clean, explicit=args.name or None)
        print(f"\nWriting one album into {output_name} ...")
        try:
            written, errors, album_dir = flat.write(
                args.source, albums, output_name, clean=args.clean)
        except organizer.OutputExists as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2

        print(f"Wrote {written} file(s) to {album_dir}")
        if errors:
            print(f"{len(errors)} problem(s):", file=sys.stderr)
            for e in errors[:6]:
                print(f"    {e}", file=sys.stderr)
        print("\nOriginals were not modified.")
        return 0

    if args.dry_run:
        print("\nDry run: nothing was written.")
        return 0

    print("\nWriting albums...")
    batch.write_shoots(shoot_results, clean=args.clean, progress=None,
                       times=times)

    failed = [s for s in shoot_results if s.errors]
    print(f"\nCopied {result.total_copied} file(s) across "
          f"{len([s for s in shoot_results if s.copied])} shoot(s).")
    if failed:
        print(f"{len(failed)} shoot(s) had problems:", file=sys.stderr)
        for shoot in failed[:10]:
            print(f"    {shoot.name}: {shoot.errors[0]}", file=sys.stderr)

    print("\nOriginals were not modified; your folder structure is unchanged.")
    return 0 if not failed else 1


def cmd_names(args: argparse.Namespace) -> int:
    """Export faces to be named by renaming, or read the names back."""
    _rule("NAMES")
    print(f"Source: {args.source}\n")

    if args.export:
        if not faces.models_present():
            print("Face models are missing. Run download-models first.",
                  file=sys.stderr)
            return 2

        images = pipeline.load_images(args.source)
        if not images:
            print("No photos found.", file=sys.stderr)
            return 1

        results, _ = pipeline.analyze_faces(
            args.source, images, workers=args.workers)
        found = people.cluster(results, threshold=args.threshold)
        people.carry_labels(found, args.source)
        # Persist the very clustering that is being exported. The manifest
        # records cluster ids, and if people.json still held an older run's
        # clustering those ids would refer to different people - a renamed face
        # would then label the wrong guest.
        people.save(found, args.source)

        out = naming.export_faces(
            args.source, found, results,
            only_unlabeled=not args.all, clean=args.clean,
        )
        print(f"Wrote {out.written} face(s) to:")
        print(f"    {os.path.join(out.directory, naming.UNIDENTIFIED)}")
        if out.skipped:
            print(f"    ({out.skipped} skipped - already named, or no usable crop)")
        print()
        print("Drag each face into a role folder and rename it to the person's")
        print("name, then run:  names --apply")
        print(f"Instructions are in {os.path.join(out.directory, 'READ_ME.txt')}")
        return 0

    entries, problems = naming.collect(args.source)
    print(naming.summarize(entries, problems))

    if not args.apply:
        print()
        print(f"{len(entries)} name(s) ready. Re-run with --apply to save them.")
        return 0

    if not entries:
        return 1

    changes, errors = naming.apply(args.source, entries, force=args.force)
    print()
    if errors:
        for error in errors:
            print(f"error: {error}", file=sys.stderr)
        return 2
    for change in changes:
        print(f"    {change}")
    print()
    print(labels.summary(args.source))
    print()
    print("Re-run the batch to refile photos under the new names.")
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    # Imported here so the rest of the CLI does not pay for it, and so a
    # missing ui.html only ever breaks this one command.
    from . import server

    return server.serve(
        host=args.host,
        port=args.port,
        source=args.source,
        open_browser=not args.no_browser,
    )


def _rule(title: str) -> None:
    print("=" * 68)
    print(f"  {title}")
    print("=" * 68)


# ============================================================================
# scan
# ============================================================================

def cmd_scan(args: argparse.Namespace) -> int:
    _rule("SCAN")
    print(f"Source: {args.source}\n")

    images = scanner.find_images(args.source)
    print(scanner.describe(args.source, images))

    if not images:
        print("\nNothing to do.")
        return 1

    print(f"\nFirst {min(args.show, len(images))} of {len(images)}:")
    for path in images[: args.show]:
        print(f"    {os.path.relpath(path, args.source)}")

    return 0


# ============================================================================
# quality
# ============================================================================

def cmd_quality(args: argparse.Namespace) -> int:
    _rule("QUALITY")
    print(f"Source: {args.source}\n")

    images = scanner.find_images(args.source)
    if not images:
        print("No images found.")
        return 1

    if args.limit:
        images = images[: args.limit]

    print(f"Measuring {len(images)} images on {args.workers} workers...")
    metrics: List[quality.QualityMetrics] = []

    if args.workers == 1:
        for i, path in enumerate(images, 1):
            metrics.append(quality.measure(path))
            _progress(i, len(images))
    else:
        with ProcessPoolExecutor(max_workers=args.workers) as pool:
            for i, m in enumerate(pool.map(quality.measure, images), 1):
                metrics.append(m)
                _progress(i, len(images))

    failed = [m for m in metrics if not m.ok]
    if failed:
        print(f"\n{len(failed)} image(s) could not be read:")
        for m in failed[:10]:
            print(f"    {os.path.basename(m.filepath)}: {m.error}")

    scores = quality.score_collection(metrics)
    if not scores:
        print("\nNo readable images.")
        return 1

    ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)

    print(f"\nRanked {len(ranked)} images (best first):\n")
    print(f"    {'#':>3}  {'score':>6}  filename")
    for rank, (path, score) in enumerate(ranked, 1):
        print(f"    {rank:>3}  {score:6.4f}  {os.path.basename(path)}")

    if args.raw:
        print("\nRaw metrics:\n")
        by_path = {m.filepath: m for m in metrics}
        for path, _ in ranked:
            print(f"    {quality.explain(by_path[path])}")

    return 0


def _progress(done: int, total: int) -> None:
    if done == total or done % 25 == 0:
        pct = done / total * 100
        print(f"    {done}/{total} ({pct:.0f}%)")


# ============================================================================
# download-models
# ============================================================================

def cmd_download_models(args: argparse.Namespace) -> int:
    _rule("DOWNLOAD MODELS")
    print(f"Target: {config.MODELS_DIR}\n")
    print("These are plain ONNX weight files run by OpenCV locally on the CPU.")
    print("No API key, no account, and the only network access in the project.\n")

    faces.download_models(force=args.force)

    print()
    if faces.models_present():
        print("Both models present. Face analysis can run fully offline from here.")
        return 0
    print("Models still missing.", file=sys.stderr)
    return 1


# ============================================================================
# faces
# ============================================================================

def cmd_faces(args: argparse.Namespace) -> int:
    _rule("FACES")
    print(f"Source: {args.source}\n")

    if not faces.models_present():
        print("Face models are missing. Run:", file=sys.stderr)
        print("    python3 -m wedding_album download-models", file=sys.stderr)
        return 2

    images = scanner.find_images(args.source)
    if not images:
        print("No images found.")
        return 1
    if args.limit:
        images = images[: args.limit]

    print(f"Detecting faces in {len(images)} images on {args.workers} workers...")
    results = []

    if args.workers == 1:
        analyzer = faces.FaceAnalyzer()
        for i, path in enumerate(images, 1):
            results.append(analyzer.analyze(path))
            _progress(i, len(images))
    else:
        with ProcessPoolExecutor(max_workers=args.workers) as pool:
            for i, r in enumerate(pool.map(faces.analyze_one, images), 1):
                results.append(r)
                _progress(i, len(images))

    failed = [r for r in results if not r.ok]
    total_faces = sum(r.count for r in results)
    no_face = [r for r in results if r.ok and r.count == 0]

    print(f"\n{total_faces} faces across {len(results)} images")
    print(f"    {len(no_face)} image(s) with no face")
    if failed:
        print(f"    {len(failed)} image(s) failed")
        for r in failed[:10]:
            print(f"        {os.path.basename(r.filepath)}: {r.error}")

    print("\nPer image (size_ratio@score, * = prominent):\n")
    for r in results:
        print(f"    {faces.describe(r)}")

    return 0


# ============================================================================
# people
# ============================================================================

def cmd_people(args: argparse.Namespace) -> int:
    _rule("PEOPLE")
    print(f"Source: {args.source}\n")

    if not faces.models_present():
        print("Face models are missing. Run:", file=sys.stderr)
        print("    python3 -m wedding_album download-models", file=sys.stderr)
        return 2

    images = pipeline.load_images(args.source, args.limit)
    if not images:
        print("No images found.")
        return 1

    print(f"Analyzing faces in {len(images)} images...")
    results, from_cache = pipeline.analyze_faces(
        args.source,
        images,
        workers=args.workers,
        use_cache=not args.no_cache,
        progress=_progress,
    )
    if from_cache:
        print(f"    {from_cache} of {len(images)} served from cache")

    total_faces = sum(r.count for r in results)
    print(f"    {total_faces} faces found\n")

    print(f"Clustering at similarity >= {args.threshold:.3f} ...")
    found = people.cluster(results, threshold=args.threshold)
    if not found:
        print("No embeddable faces; nothing to cluster.")
        return 1

    restored = people.carry_labels(found, args.source)
    if restored:
        print(f"    carried over {restored} label(s) from a previous run")

    print(f"    {len(found)} distinct people\n")

    print(f"    {'id':<5} {'photos':>6} {'faces':>6} {'cohesion':>9}  label")
    for person in found:
        who = person.name or ("(unnamed)" if person.is_labeled else "-")
        role = person.role if person.is_labeled else ""
        print(
            f"    {person.id:<5} {person.photo_count:>6} {person.face_count:>6} "
            f"{person.cohesion():>9.3f}  {who} {role}".rstrip()
        )

    singletons = [p for p in found if p.face_count == 1]
    loose = [p for p in found if p.face_count > 1 and p.cohesion() < 0.5]
    if singletons:
        print(f"\n    {len(singletons)} person(s) seen in only one face: "
              f"{', '.join(p.id for p in singletons)}")
        print("    Often a passer-by or a bad angle rather than a real guest.")
    if loose:
        print(f"\n    Low cohesion, may have merged two people: "
              f"{', '.join(p.id for p in loose)}")

    saved = people.save(found, args.source)
    print(f"\nSaved people to {saved}")

    if not args.no_preview:
        preview.register_boxes(results)
        out = preview.export_people(found, args.source, per_person=args.crops)
        print(f"Wrote face crops to {out}")
        print("    Open ALL_PEOPLE.jpg for one face per person,")
        print("    or pNN_sheet.jpg to check a single person's cluster.")

    return 0


# ============================================================================
# label
# ============================================================================

def cmd_label(args: argparse.Namespace) -> int:
    _rule("LABEL")
    print(f"Source: {args.source}\n")

    try:
        if args.clear is not None:
            cleared = labels.clear(args.source, args.clear or None)
            scope = ", ".join(args.clear) if args.clear else "everyone"
            print(f"Cleared {cleared} label(s) for {scope}.\n")
            print(labels.summary(args.source))
            return 0

        if args.who:
            print(labels.who_next(args.source))
            return 0

        if args.interactive:
            changed = labels.interactive(args.source)
            print(f"\nLabeled {changed} person(s).\n")
            print(labels.summary(args.source))
            return 0

        if args.set:
            assignments = labels.parse_assignments(args.set)
            _, changes = labels.apply(args.source, assignments, force=args.force)
            if changes:
                print("Applied:")
                for change in changes:
                    print(f"    {change}")
            else:
                print("Nothing changed.")
            print()

        print(labels.summary(args.source))
        return 0

    except labels.LabelError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


# ============================================================================
# sections
# ============================================================================

def _prepare(args: argparse.Namespace):
    """
    Shared front half of `sections` and `organize`: faces, people, labels and
    quality, all reusing the cache.
    """
    images = pipeline.load_images(args.source, getattr(args, "limit", 0))
    if not images:
        raise FileNotFoundError(f"No images found under {args.source}")

    results, from_cache = pipeline.analyze_faces(
        args.source, images, workers=args.workers, progress=None,
    )
    print(f"    faces: {sum(r.count for r in results)} "
          f"({from_cache}/{len(images)} from cache)")

    found = people.cluster(results)
    restored = people.carry_labels(found, args.source)
    labeled = [p for p in found if p.is_labeled]
    print(f"    people: {len(found)} clusters, {len(labeled)} labeled "
          f"({restored} carried over)")

    unlabeled = [p for p in found if not p.is_labeled]
    if unlabeled:
        print(f"    {len(unlabeled)} unlabeled: "
              f"{', '.join(p.id for p in unlabeled)}")

    metrics = pipeline.analyze_quality(images, workers=args.workers)
    scores = quality.score_collection(metrics)
    phashes = {m.filepath: m.phash for m in metrics if m.ok}
    print(f"    quality: scored {len(scores)} photos")

    return results, found, scores, phashes


def cmd_sections(args: argparse.Namespace) -> int:
    _rule("SECTIONS")
    print(f"Source: {args.source}\n")

    if not faces.models_present():
        print("Face models are missing. Run download-models first.", file=sys.stderr)
        return 2

    results, found, scores, phashes = _prepare(args)

    assignments = sections.assign_all(results, found, scores)
    flagged = sections.mark_duplicates(assignments, phashes)
    print(f"    duplicates: {flagged} flagged\n")

    print(sections.summarize(assignments))

    groups = selection.group_shots(assignments, phashes)
    picks = selection.choose(groups, per_section=args.picks)

    print("\nShot groups and picks:\n")
    print(selection.summarize(groups, picks))
    print(selection.describe_groups(groups))

    return 0


# ============================================================================
# organize
# ============================================================================

def cmd_organize(args: argparse.Namespace) -> int:
    _rule("ORGANIZE")
    print(f"Source: {args.source}")
    print(f"Album:  {organizer.album_dir(args.source)}\n")

    if not faces.models_present():
        print("Face models are missing. Run download-models first.", file=sys.stderr)
        return 2

    results, found, scores, phashes = _prepare(args)

    unlabeled = [p for p in found if not p.is_labeled]
    if unlabeled and not args.allow_unlabeled:
        print(
            f"\nerror: {len(unlabeled)} person(s) still unlabeled "
            f"({', '.join(p.id for p in unlabeled)}).",
            file=sys.stderr,
        )
        print("Their photos would land in 00_Unsorted or 07_Group. Either label them:",
              file=sys.stderr)
        print("    python3 -m wedding_album label --interactive", file=sys.stderr)
        print("or re-run with --allow-unlabeled to organize anyway.", file=sys.stderr)
        return 2

    assignments = sections.assign_all(results, found, scores)
    flagged = sections.mark_duplicates(assignments, phashes)
    print(f"    duplicates: {flagged} flagged")

    groups = selection.group_shots(assignments, phashes)
    picks = selection.choose(
        groups, per_section=args.picks, skip_duplicates=args.skip_duplicates,
    )
    print(f"    shot groups: {len(groups)}\n")
    print(selection.summarize(groups, picks))
    print()

    output_name = organizer.target_version_name(
        args.source, clean=args.clean, explicit=args.name)

    try:
        clean_path = organizer.check_output(
            args.source, clean=args.clean, output_name=output_name)
    except organizer.OutputExists as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    plan = organizer.build_plan(
        assignments, args.source,
        picks=picks,
        output_name=output_name,
    )

    print(organizer.describe_plan(plan))

    if args.dry_run:
        print("\nDry run: nothing was written.")
        print("Per file:\n")
        for c in plan.copies:
            tag = "pick" if c.is_pick else "alt "
            print(f"    {tag}  {os.path.relpath(c.destination, args.source):<52} "
                  f"{c.assignment.pick_reason or c.assignment.reason}")
        return 0

    if clean_path:
        print(f"\nReplacing existing album at {clean_path}")

    done, errors = organizer.execute(plan, clean_path=clean_path)
    print(f"\nCopied {done} of {len(plan.copies)} files "
          f"({len(plan.picks)} picks, {len(plan.runners_up)} in "
          f"{config.SECOND_OPTION_DIR_NAME}).")
    if errors:
        print(f"{len(errors)} error(s):", file=sys.stderr)
        for e in errors[:10]:
            print(f"    {e}", file=sys.stderr)

    report = organizer.write_report(assignments, plan, args.source)
    print(f"Report: {report}")

    print("\nOriginals were not modified; every photo was copied.")
    return 0 if not errors else 1


# ============================================================================
# argument parsing
# ============================================================================

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="wedding_album",
        description="Organize wedding photos into album sections by relationship.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")

    sub = parser.add_subparsers(dest="command", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--source",
        default=config.DEFAULT_SOURCE_DIR,
        help="collection to process (default: %(default)s)",
    )

    p_scan = sub.add_parser("scan", parents=[common], help="list photos that would be processed")
    p_scan.add_argument("--show", type=int, default=20, help="how many paths to print")
    p_scan.set_defaults(func=cmd_scan)

    p_quality = sub.add_parser("quality", parents=[common], help="measure and rank quality")
    p_quality.add_argument("--limit", type=int, default=0, help="only process the first N images")
    p_quality.add_argument(
        "--workers",
        type=int,
        default=os.cpu_count() or 4,
        help="parallel worker processes (default: %(default)s)",
    )
    p_quality.add_argument("--raw", action="store_true", help="also dump raw metric values")
    p_quality.set_defaults(func=cmd_quality)

    p_models = sub.add_parser("download-models", help="fetch the ONNX face models (one-time)")
    p_models.add_argument("--force", action="store_true", help="re-download even if present")
    p_models.set_defaults(func=cmd_download_models)

    p_faces = sub.add_parser("faces", parents=[common], help="detect faces and report")
    p_faces.add_argument("--limit", type=int, default=0, help="only process the first N images")
    p_faces.add_argument(
        "--workers",
        type=int,
        default=max(1, (os.cpu_count() or 4) // 2),
        help="parallel worker processes (default: %(default)s)",
    )
    p_faces.set_defaults(func=cmd_faces)

    p_people = sub.add_parser(
        "people", parents=[common], help="group faces into distinct people"
    )
    p_people.add_argument("--limit", type=int, default=0, help="only process the first N images")
    p_people.add_argument(
        "--workers", type=int, default=pipeline.default_workers(),
        help="parallel worker processes (default: %(default)s)",
    )
    p_people.add_argument(
        "--threshold", type=float, default=config.FACE_MATCH_THRESHOLD,
        help="cosine similarity for same-person (default: %(default)s)",
    )
    p_people.add_argument("--crops", type=int, default=12,
                          help="face crops to export per person")
    p_people.add_argument("--no-preview", action="store_true",
                          help="skip writing face crops")
    p_people.add_argument("--no-cache", action="store_true",
                          help="ignore cached face analysis")
    p_people.set_defaults(func=cmd_people)

    p_label = sub.add_parser(
        "label", parents=[common],
        help="assign a relationship role to each person",
        epilog=(
            "examples:\n"
            "  label                                  show current labels\n"
            "  label --set p01=bride:Rekha\n"
            "  label --set p02=groom:Siva --set p03=ignore\n"
            "  label --set p05=family:Priya:Sister\n"
            "  label --interactive                    prompt for each person\n"
            "  label --clear p04                      reset one person\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_label.add_argument(
        "--set", action="append", metavar="pNN=role[:name[:note]]",
        help="assign a role; repeatable",
    )
    p_label.add_argument("--interactive", action="store_true",
                         help="prompt for each unlabeled person")
    p_label.add_argument("--clear", nargs="*", metavar="pNN",
                         help="reset labels (all people if no id given)")
    p_label.add_argument("--who", action="store_true",
                         help="rank the unidentified people by how many photos "
                              "naming them would file")
    p_label.add_argument("--force", action="store_true",
                         help="allow a second bride or groom")
    p_label.set_defaults(func=cmd_label)

    p_sections = sub.add_parser(
        "sections", parents=[common],
        help="show which section each photo would go to, without copying",
    )
    p_sections.add_argument("--limit", type=int, default=0,
                            help="only process the first N images")
    p_sections.add_argument("--workers", type=int, default=pipeline.default_workers(),
                            help="parallel worker processes (default: %(default)s)")
    p_sections.add_argument("--picks", type=int,
                            default=config.DEFAULT_PICKS_PER_SECTION,
                            help="photos to pick per section (0 = all)")
    p_sections.set_defaults(func=cmd_sections)

    p_org = sub.add_parser(
        "organize", parents=[common],
        help=f"copy photos into {config.OUTPUT_DIR_NAME}/ and write the report",
        epilog=(
            "The source photos are only ever read. Everything is copied into\n"
            f"<collection>/{config.OUTPUT_DIR_NAME}/, so a bad run costs disk space only.\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_org.add_argument("--limit", type=int, default=0,
                       help="only process the first N images")
    p_org.add_argument("--workers", type=int, default=pipeline.default_workers(),
                       help="parallel worker processes (default: %(default)s)")
    p_org.add_argument("--picks", type=int,
                       default=config.DEFAULT_PICKS_PER_SECTION,
                       help="photos to pick per section, one per shot group "
                            "(0 = keep all as picks; default: %(default)s)")
    p_org.add_argument("--name", default="",
                       help="album folder name (default: next unused "
                            f"{config.OUTPUT_DIR_PREFIX}_vN)")
    p_org.add_argument("--skip-duplicates", action="store_true",
                       help="do not let near-duplicate shots be picked")
    p_org.add_argument("--clean", action="store_true",
                       help="replace the album folder if it already exists")
    p_org.add_argument("--dry-run", action="store_true",
                       help="show what would be copied and stop")
    p_org.add_argument("--allow-unlabeled", action="store_true",
                       help="organize even with unlabeled people")
    p_org.set_defaults(func=cmd_organize)

    p_serve = sub.add_parser(
        "serve", parents=[common],
        help="open the web UI for configuring, labeling and organizing",
        epilog=(
            "The server has no authentication and binds to loopback only, so\n"
            "nothing outside this machine can reach it. Ctrl-C to stop.\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_serve.add_argument("--host", default="127.0.0.1",
                         help="bind address; loopback only (default: %(default)s)")
    p_serve.add_argument("--port", type=int, default=8766,
                         help="port (default: %(default)s)")
    p_serve.add_argument("--no-browser", action="store_true",
                         help="do not open a browser window")
    p_names = sub.add_parser(
        "names", parents=[common],
        help="name people by renaming a picture of them",
        epilog=(
            "  names --export     write one face per unnamed person\n"
            "  names              show what you have named so far\n"
            "  names --apply      save those names as labels\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_names.add_argument("--export", action="store_true",
                         help="write the faces to be named")
    p_names.add_argument("--apply", action="store_true",
                         help="save the names you have given")
    p_names.add_argument("--all", action="store_true",
                         help="export everyone, including already-named people")
    p_names.add_argument("--clean", action="store_true",
                         help="start the naming folder over")
    p_names.add_argument("--force", action="store_true",
                         help="allow a second bride or groom")
    p_names.add_argument("--threshold", type=float,
                         default=config.FACE_MATCH_THRESHOLD,
                         help="same-person similarity (default: %(default)s)")
    p_names.add_argument("--workers", type=int, default=pipeline.default_workers(),
                         help="parallel worker processes")
    p_names.set_defaults(func=cmd_names)

    p_serve.set_defaults(func=cmd_serve)

    p_shoots = sub.add_parser(
        "shoots", parents=[common],
        help="list the shoot folders a collection splits into",
    )
    p_shoots.set_defaults(func=cmd_shoots)

    p_batch = sub.add_parser(
        "batch", parents=[common],
        help="organize every shoot folder, each album written in place",
        epilog=(
            "People are clustered once across the whole collection, so you label\n"
            "Rekha and Siva a single time and it applies to every ceremony.\n"
            "Sections and picks are then worked out per shoot, and each shoot gets\n"
            f"its own {config.OUTPUT_DIR_PREFIX}_vN folder beside its photos.\n"
            "\n"
            "Your existing folders are never modified.\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_batch.add_argument("--flat", action="store_true", default=config.FLAT_ALBUM,
                         help="one album tree: event / phase / Top-Review-Others "
                              "(default)")
    p_batch.add_argument("--per-shoot", dest="flat", action="store_false",
                         help="older layout: an album inside every shoot folder")
    p_batch.add_argument("--top", type=int, default=config.PICKS_PER_GROUP,
                         help="photos in Top for each phase (default: %(default)s)")
    p_batch.add_argument("--use-percent", action="store_true",
                         help="size Top as a percentage instead of a fixed count")
    p_batch.add_argument("--percent", type=float, default=config.DEFAULT_PICKS_PERCENT,
                         help="keep this %% when --use-percent is set "
                              "(default: %(default)s)")
    p_batch.add_argument("--picks", type=int, default=0,
                         help="absolute count per section instead of a percentage")
    p_batch.add_argument("--keep-duplicates", action="store_true",
                         help="keep near-duplicates as alternatives instead of "
                              "discarding them")
    p_batch.add_argument("--limit", type=int, default=0,
                         help="only the first N photos of each shoot")
    p_batch.add_argument("--workers", type=int, default=pipeline.default_workers(),
                         help="parallel worker processes (default: %(default)s)")
    p_batch.add_argument("--name", default="",
                         help="album folder name (default: next unused "
                              f"{config.OUTPUT_DIR_PREFIX}_vN)")
    p_batch.add_argument("--clean", action="store_true",
                         help="replace album folders that already exist")
    p_batch.add_argument("--dry-run", action="store_true",
                         help="show the plan and stop")
    p_batch.add_argument("--allow-unlabeled", action="store_true",
                         help="run even with unidentified people")
    p_batch.set_defaults(func=cmd_batch)

    return parser


def main(argv: List[str] | None = None) -> int:
    config.validate()
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except (NotADirectoryError, FileNotFoundError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
