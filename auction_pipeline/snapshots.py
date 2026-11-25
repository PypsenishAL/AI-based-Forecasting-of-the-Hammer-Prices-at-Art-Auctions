"""Snapshot publication, retention and agent input packages."""

import argparse
import csv
import hashlib
import json
import re
import shutil
import stat
import sys
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

import requests
from filelock import FileLock, Timeout
from PIL import Image, ImageOps

from .common import (
    IMAGE_EXTENSIONS, DeadlineError, HistoryReadinessError, PipelineError,
    check_deadline, lot_label, plain_text, utc_datetime, utc_now, utc_text,
)


LOT_FIELDS = (
    "lot_id", "lot_number", "sale_id", "lot_url", "artist", "title",
    "creation_date_raw", "medium_raw", "dimensions_raw", "details_raw",
    "provenance_raw", "exhibited_raw", "literature_raw", "lot_essay_raw",
    "condition_raw", "image_path", "collected_at_utc", "session_start_utc",
    "cutoff_source_url",
)
ESTIMATE_FIELDS = (
    "lot_id", "estimate_low", "estimate_high", "estimate_currency", "estimate_raw",
    "collected_at_utc",
)
MANIFEST = "collection.json"
HISTORY_FILE = "artist_history.json"
BACKFILL_SUFFIX = ".history-backfill.json"
def write_csv(path, fields, rows):
    with path.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fields)
        writer.writeheader()
        writer.writerows(rows)


def read_csv(path, fields):
    with path.open(encoding="utf-8-sig", newline="") as file:
        reader = csv.DictReader(file, strict=True)
        if reader.fieldnames != list(fields):
            raise PipelineError(f"Unexpected CSV columns: {path.name}")
        rows = list(reader)
        if any(None in row or None in row.values() for row in rows):
            raise PipelineError(f"Malformed CSV record: {path.name}")
        return rows


def collection_name(auction, args, collected):
    title = plain_text(auction.get("title") or "Christie's live auction")
    title = re.sub(r'[<>:"/\\|?*\x00-\x1f]', " ", title)
    title = " ".join(title.split())[:65].rstrip(" .") or "Christie's live auction"
    return (f"{title} - {auction['sale_number']} - lots {lot_label(args.lot_from)}-{lot_label(args.lot_to)}"
            f" - {collected:%Y-%m-%d %H-%M-%S} UTC")


def is_link(path):
    file_stat = path.lstat()
    return (stat.S_ISLNK(file_stat.st_mode)
            or bool(getattr(file_stat, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT))


def hash_files(folder):
    """Fingerprint only the expected, flat snapshot layout."""
    entries = {entry.name: entry for entry in folder.iterdir()}
    allowed = {"lots.csv", "estimates.csv", "errors.txt", "images", MANIFEST, HISTORY_FILE}
    if ("images" not in entries or set(entries) - allowed
            or any(is_link(entry) for entry in entries.values())):
        raise PipelineError("Snapshot contains additional files or links")
    images = entries.pop("images")
    entries.pop(MANIFEST, None)
    files = entries
    for photo in images.iterdir():
        if (is_link(photo) or not photo.is_file() or not re.fullmatch(
                r"(?:[0-9]+|[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12})", photo.stem)
                or photo.suffix not in IMAGE_EXTENSIONS.values()):
            raise PipelineError("Snapshot contains an unexpected image entry")
        files[f"images/{photo.name}"] = photo
    hashes = {}
    for name, path in files.items():
        with path.open("rb") as source:
            hashes[name] = hashlib.file_digest(source, "sha256").hexdigest()
    return hashes


def write_json(path, payload):
    temp = path.with_name(f".{uuid4().hex}.tmp")
    try:
        with temp.open("x", encoding="utf-8", newline="\n") as file:
            json.dump(payload, file, ensure_ascii=False, indent=2)
            file.write("\n")
        temp.replace(path)
    finally:
        temp.unlink(missing_ok=True)


def write_manifest(folder, start, ready_at=None, source="christies"):
    payload = {"version": 1, "session_start_utc": utc_text(start),
               "analysis_ready_at_utc": utc_text(ready_at) if ready_at else None,
               "source": source,
               "files": hash_files(folder)}
    write_json(folder / MANIFEST, payload)


def checked_history(history, lots, start, ready_at):
    ids = [row["lot_id"] for row in lots]
    if (history.get("schema_version") != 2 or history.get("coverage_status") != "scanned"
            or history.get("source_errors") != []
            or history.get("session_start_utc") != utc_text(start)
            or [row.get("lot_id") for row in history.get("lots", [])] != ids
            or any("hammer_comparables" not in row for row in history.get("lots", []))):
        raise HistoryReadinessError("Historical coverage or lot alignment is incomplete")
    observed = [utc_datetime(row["collected_at_utc"]) for row in lots]
    generated = utc_datetime(history.get("generated_at_utc"))
    if not (utc_datetime(history.get("cutoff_utc")) == min(observed)
            and max(observed) <= generated <= ready_at < start
            and all(utc_datetime(row["session_start_utc"]) == start for row in lots)):
        raise HistoryReadinessError("History timestamps violate the shared session deadline")


def legacy_snapshot_source(folder, rows):
    urls = [row["cutoff_source_url"] for row in rows]
    if not urls and folder.name.endswith(".incomplete") and (folder / "errors.txt").is_file():
        with (folder / "errors.txt").open(encoding="utf-8", newline="") as log:
            urls = [row["url"] for row in csv.DictReader(log, delimiter="\t")]
    hosts = {urlsplit(url).hostname for url in urls}
    if hosts == {"www.sothebys.com"}:
        return "sothebys"
    if hosts == {"www.christies.com"} or not urls:
        return "christies"
    return None


def cleanup_expired(root, current, now, source="christies"):
    for folder in root.iterdir():
        if folder == current or folder.name == "used" or not folder.is_dir():
            continue
        try:
            manifest = folder / MANIFEST
            if is_link(folder) or folder.resolve().parent != root.resolve():
                continue
            if not manifest.is_file() or is_link(manifest):
                continue
            data = json.loads(manifest.read_text(encoding="utf-8"))
            if (not isinstance(data, dict) or data.get("version") != 1
                    or utc_datetime(data.get("session_start_utc")) > now()
                    or data.get("files") != hash_files(folder)):
                continue
            rows = []
            if (folder / "lots.csv").exists():
                rows = read_csv(folder / "lots.csv", LOT_FIELDS)
                if any(row["session_start_utc"] != data["session_start_utc"] for row in rows):
                    continue
            snapshot_source = data.get("source") or legacy_snapshot_source(folder, rows)
            if snapshot_source != source:
                continue
            shutil.rmtree(folder)
            print(f"Removed expired snapshot: {folder}", file=sys.stderr)
        except (PipelineError, OSError, KeyError, ValueError, csv.Error) as exc:
            print(f"Cleanup skipped {folder}: {exc}", file=sys.stderr)


def free_name(root, name):
    destination = root / name
    if destination.exists():
        destination = root / f"{name} ({uuid4().hex[:8]})"
    return destination


PRICE_WORD = re.compile(r"\b(?:estim(?:ate|ated|ation)|bid(?:ding)?|price|reserve|hammer|realised|realized)\b", re.I)
SENSITIVE_VALUE = re.compile(
    r"\b(?:usd|gbp|eur|chf|hkd|cny|jpy|aed|aud|cad|dollars?|pounds?|euros?)\b|[$€£¥]|https?://|www\."
    r"|\b\d{1,3}(?:[, ]\d{3})+\b|\b\d{5,}\b", re.I,
)
STANDALONE_AMOUNT = re.compile(r"\s*[\d,. ]+(?:\s*[-–—−]\s*[\d,. ]+)?\s*")
BLIND_FIELDS = ("artist", "title", "creation_date_raw", "medium_raw", "dimensions_raw",
                "details_raw", "provenance_raw", "exhibited_raw", "literature_raw",
                "lot_essay_raw", "condition_raw")
def blind_text(value, identity_field=False):
    retained = []
    after_price_label = False
    for line in value.replace(r"\n", "\n").splitlines():
        if not line.strip():
            continue
        price_label = not identity_field and bool(PRICE_WORD.search(line))
        if not (price_label or SENSITIVE_VALUE.search(line)
                or (after_price_label and STANDALONE_AMOUNT.fullmatch(line))):
            retained.append(line)
        after_price_label = price_label
    return "\n".join(retained)


def write_blind_packet(folder, snapshot, lots, history):
    images = folder / "images"
    images.mkdir(parents=True)
    by_id = {row["lot_id"]: row for row in history["lots"]}
    packet = []
    for index, row in enumerate(lots, 1):
        sample_id = f"work-{index:02d}"
        image_path = f"images/{sample_id}.png"
        with Image.open(snapshot / row["image_path"]) as source:
            clean = ImageOps.exif_transpose(source).convert("RGB")
            clean.info.clear()
            clean.save(folder / image_path, format="PNG")
        comparables = by_id[row["lot_id"]]["hammer_comparables"]
        fields = ("title", "medium", "dimensions_raw", "sale_date", "currency", "hammer_price")
        packet.append({
            "sample_id": sample_id, "image_path": image_path,
            **{key: blind_text(row[key], identity_field=key in {"artist", "title"}) for key in BLIND_FIELDS},
            "hammer_comparables": {
                "price_basis": "hammer_price", "status": comparables["status"],
                "groups": comparables["groups"],
                "sales": [{key: blind_text(sale[key], identity_field=key == "title")
                           if key in {"title", "medium", "dimensions_raw"}
                           else sale[key] for key in fields} for sale in comparables["sales"]],
            },
        })
    write_json(folder / "lots.json", {"prediction_deadline_utc": history["session_start_utc"], "lots": packet})


def archive_used_lots(snapshot, lot_ids):
    if not lot_ids:
        raise PipelineError("Supply at least one --lot-id")
    root = snapshot.absolute().parent
    with FileLock(root / ".collections.lock", timeout=30):
        if not snapshot.is_dir() or is_link(snapshot) or snapshot.name.endswith(".incomplete"):
            raise PipelineError("Select a completed snapshot")
        hashes = hash_files(snapshot)
        manifest_path = snapshot / MANIFEST
        if not manifest_path.is_file() or HISTORY_FILE not in hashes:
            raise HistoryReadinessError("Snapshot is not ready for analysis")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if not manifest.get("analysis_ready_at_utc") or manifest.get("files") != hashes:
            raise HistoryReadinessError("Snapshot is not ready for analysis or has changed")
        ready_at = utc_datetime(manifest["analysis_ready_at_utc"])
        start = utc_datetime(manifest["session_start_utc"])
        all_lots = read_csv(snapshot / "lots.csv", LOT_FIELDS)
        history = json.loads((snapshot / HISTORY_FILE).read_text(encoding="utf-8"))
        checked_history(history, all_lots, start, ready_at)
        lots = [row for row in all_lots if row["lot_id"] in lot_ids]
        estimates = [row for row in read_csv(snapshot / "estimates.csv", ESTIMATE_FIELDS)
                     if row["lot_id"] in lot_ids]
        for rows in (lots, estimates):
            if len(rows) != len(lot_ids) or {row["lot_id"] for row in rows} != lot_ids:
                raise PipelineError("Each requested lot must occur once in both CSV files")
        timestamps = {row["lot_id"]: row["collected_at_utc"] for row in estimates}
        if any(row["collected_at_utc"] != timestamps[row["lot_id"]] for row in lots):
            raise PipelineError("Catalog and estimate timestamps disagree")
        for row in lots:
            photo = row["image_path"]
            if photo not in hashes or not photo.startswith(f"images/{row['lot_id']}."):
                raise PipelineError(f"Missing or inconsistent image for lot {row['lot_id']}")
        if snapshot.name == "used" or root.name == "used":
            raise PipelineError("Select a snapshot from the collection folder")
        archive = root / "used"
        archive.mkdir(exist_ok=True)
        if is_link(archive) or archive.resolve().parent != root.resolve():
            raise PipelineError("The used archive must be a local directory inside the collection folder")
        name = f"{snapshot.name} - used"
        staging = archive / f".archive-{uuid4().hex[:8]}.incomplete"
        audit = staging / "audit"
        (audit / "images").mkdir(parents=True)
        try:
            write_csv(audit / "lots.csv", LOT_FIELDS, lots)
            write_csv(audit / "estimates.csv", ESTIMATE_FIELDS, estimates)
            for row in lots:
                shutil.copyfile(snapshot / row["image_path"], audit / row["image_path"])
            if "errors.txt" in hashes:
                shutil.copyfile(snapshot / "errors.txt", audit / "errors.txt")
            history["lots"] = [row for row in history["lots"] if row["lot_id"] in lot_ids]
            write_json(audit / HISTORY_FILE, history)
            write_json(audit / "source_collection.json", manifest)
            write_json(audit / "sample_map.json", {f"work-{i:02d}": row["lot_id"]
                                                   for i, row in enumerate(lots, 1)})
            write_json(audit / "archive_time.json", {"archived_at_utc": utc_text(utc_now())})
            write_blind_packet(staging / "blind", snapshot, lots, history)
            arbiter = staging / "arbiter"
            arbiter.mkdir()
            by_id = {row["lot_id"]: row for row in estimates}
            write_json(arbiter / "estimates.json", {"lots": [
                {"sample_id": f"work-{i:02d}", **{key: by_id[row["lot_id"]][key]
                 for key in ("estimate_low", "estimate_high", "estimate_currency")}}
                for i, row in enumerate(lots, 1)
            ]})
            destination = free_name(archive, name)
            staging.rename(destination)
        finally:
            if staging.is_dir() and not is_link(staging) and staging.resolve().parent == archive.resolve():
                shutil.rmtree(staging)
        return destination


def log_entry(log, url, stage, exc):
    log.append((url, stage, " ".join(str(exc).split())))


def write_log(path, log):
    path.write_text("url\tstage\treason\n" + "".join(
        f"{url}\t{stage}\t{reason}\n" for url, stage, reason in log), encoding="utf-8")


def publish_snapshot(args, auction, start, now, history_builder, collect_lots, recheck):
    source = auction.get("source", "christies")
    log = []
    name = collection_name(auction, args, now())
    staging = args.output_dir / f"{name} ({uuid4().hex[:8]}).incomplete"
    (staging / "images").mkdir(parents=True)
    try:
        lots, estimates = collect_lots(staging, log)
        write_csv(staging / "lots.csv", LOT_FIELDS, lots)
        write_csv(staging / "estimates.csv", ESTIMATE_FIELDS, estimates)
        if len(lots) < 10:
            raise PipelineError(f"Only {len(lots)} qualifying lots. Check remaining catalog links "
                                "or select another upcoming session.")
        write_log(staging / "errors.txt", log)
        if history_builder is not None:
            try:
                history = history_builder(lots, start)
                write_json(staging / HISTORY_FILE, history)
                checked_history(history, lots, start, now())
                recheck()
            except DeadlineError:
                raise
            except (PipelineError, OSError, requests.RequestException, UnicodeError, ValueError) as exc:
                raise HistoryReadinessError(str(exc)) from exc
        with FileLock(args.output_dir / ".collections.lock", timeout=30):
            ready_at = now() if history_builder is not None else None
            write_manifest(staging, start, ready_at, source)
            check_deadline(start, now)
            destination = free_name(args.output_dir, name)
            staging.rename(destination)
            if ready_at is not None or source == "sothebys":
                cleanup_expired(args.output_dir, destination, now, source)
    except (PipelineError, OSError, requests.RequestException, UnicodeError, Timeout) as exc:
        if not staging.exists():
            raise
        log_entry(log, args.catalog_url, "snapshot", exc)
        write_log(staging / "errors.txt", log)
        write_manifest(staging, start, source=source)
        error_type = type(exc) if isinstance(exc, (HistoryReadinessError, DeadlineError)) else PipelineError
        raise error_type(f"{exc}. Incomplete attempt: {staging}") from exc
    return destination, len(lots)


def mark_used_main(argv, prog):
    parser = argparse.ArgumentParser(prog=prog)
    parser.add_argument("snapshot", type=Path)
    parser.add_argument("--lot-id", required=True, action="append")
    selected = parser.parse_args(argv)
    try:
        archive = archive_used_lots(selected.snapshot, set(selected.lot_id))
    except (PipelineError, OSError, ValueError, csv.Error, Timeout) as exc:
        print(f"Archive stopped: {exc}", file=sys.stderr)
        return 1
    print(f"Saved used lots: {archive.resolve()}")
    return 0
