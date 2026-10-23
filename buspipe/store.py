"""SQLite manifest, block checkpoints, review queue and manual overrides."""

from __future__ import annotations

import csv
import hashlib
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping, Sequence

STATUS_PENDING = "pending"
STATUS_LAYOUT_DONE = "layout_done"
STATUS_OCR_DONE = "ocr_done"
STATUS_VALIDATED = "validated"
STATUS_VLM_REVIEW = "vlm_review"
STATUS_DONE = "done"
STATUS_FAILED = "failed"

# Terminal for the automatic pipeline.  "failed" is retried by policy.
PIPELINE_STATUSES = (
    STATUS_PENDING,
    STATUS_LAYOUT_DONE,
    STATUS_OCR_DONE,
    STATUS_VALIDATED,
    STATUS_VLM_REVIEW,
    STATUS_DONE,
    STATUS_FAILED,
)

REASON_PRIORITY = {
    "layout_error": 0,
    "unknown_template": 5,
    "route_number_unknown": 6,
    "route_number_uncertain": 7,
    "direction_uncertain": 8,
    "cross_column": 10,
    "wide_box": 12,
    "low_coverage": 14,
    "empty_ocr": 15,
    "next_stop_mismatch": 20,
    "current_stop_unknown": 25,
    "bidirectional_mismatch": 30,
    "low_confidence": 40,
}


def review_sort_key(item: dict[str, Any]) -> tuple[int, float, int]:
    score = item.get("score")
    return (
        REASON_PRIORITY.get(str(item.get("reason", "")), 99),
        float(score) if isinstance(score, (int, float)) else 1.0,
        int(item.get("id") or 0),
    )


SCHEMA = """
CREATE TABLE IF NOT EXISTS posters (
    image_hash TEXT PRIMARY KEY,
    path TEXT NOT NULL,
    station TEXT NOT NULL DEFAULT '',
    direction TEXT NOT NULL DEFAULT '',
    size_bytes INTEGER NOT NULL DEFAULT 0,
    width INTEGER,
    height INTEGER,
    status TEXT NOT NULL DEFAULT 'pending',
    error TEXT NOT NULL DEFAULT '',
    attempts INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS paths (
    path TEXT PRIMARY KEY,
    image_hash TEXT NOT NULL,
    size_bytes INTEGER NOT NULL DEFAULT 0,
    mtime_ns INTEGER NOT NULL DEFAULT 0,
    is_primary INTEGER NOT NULL DEFAULT 0,
    seen_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_paths_hash ON paths(image_hash);
CREATE TABLE IF NOT EXISTS blocks (
    image_hash TEXT NOT NULL,
    block_index INTEGER NOT NULL,
    status TEXT NOT NULL,
    route TEXT NOT NULL DEFAULT '',
    error TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL,
    PRIMARY KEY (image_hash, block_index)
);
CREATE TABLE IF NOT EXISTS review_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    image_hash TEXT NOT NULL,
    route TEXT NOT NULL DEFAULT '',
    direction TEXT NOT NULL DEFAULT '',
    seq INTEGER NOT NULL DEFAULT 0,
    reason TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '',
    ocr_text TEXT NOT NULL DEFAULT '',
    vlm_text TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT 'ocr',
    score REAL,
    crop_path TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'open',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(image_hash, route, direction, seq, reason)
);
CREATE INDEX IF NOT EXISTS idx_review_hash ON review_items(image_hash);
CREATE TABLE IF NOT EXISTS manual_overrides (
    image_hash TEXT NOT NULL,
    route TEXT NOT NULL,
    direction TEXT NOT NULL,
    seq INTEGER NOT NULL,
    name TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL,
    PRIMARY KEY (image_hash, route, direction, seq)
);
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def poster_json_path(out_dir: Path | str, row: Mapping[str, Any]) -> Path:
    """Single source of truth for the per-poster JSON location."""
    stem = Path(row["path"]).stem
    return Path(out_dir) / "posters" / f"{stem}_{row['image_hash'][:8]}_raw.json"


def hash_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


@dataclass
class RecordResult:
    image_hash: str
    state: str  # new | duplicate | existing
    status: str


class ManifestStore:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(SCHEMA)
            conn.commit()

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            yield conn
        finally:
            conn.close()

    # ------------------------------------------------------------------ scan

    def path_record(self, path: Path | str) -> dict[str, Any] | None:
        with self.connection() as conn:
            row = conn.execute("SELECT * FROM paths WHERE path = ?", (str(path),)).fetchone()
        return dict(row) if row else None

    def record_image(
        self,
        *,
        image_hash: str,
        path: Path | str,
        size_bytes: int,
        mtime_ns: int,
        width: int | None,
        height: int | None,
        station: str,
        direction: str,
        error: str = "",
    ) -> RecordResult:
        path = str(path)
        now = utc_now()
        with self.connection() as conn, conn:
            existing = conn.execute(
                "SELECT image_hash, path, status, error FROM posters WHERE image_hash = ?", (image_hash,)
            ).fetchone()
            if existing:
                primary_exists = Path(existing["path"]).exists()
                if not primary_exists:
                    # Content identity is stable across renames.  Promote the
                    # surviving path so the poster remains processable.
                    conn.execute(
                        "UPDATE posters SET path = ?, station = ?, direction = ?, updated_at = ? "
                        "WHERE image_hash = ?",
                        (path, station, direction, now, image_hash),
                    )
                    conn.execute("DELETE FROM paths WHERE path = ?", (existing["path"],))
                    conn.execute(
                        "UPDATE paths SET is_primary = 0 WHERE image_hash = ? AND path <> ?",
                        (image_hash, path),
                    )
                    conn.execute(
                        """
                        INSERT INTO paths(path, image_hash, size_bytes, mtime_ns, is_primary, seen_at)
                        VALUES (?, ?, ?, ?, 1, ?)
                        ON CONFLICT(path) DO UPDATE SET
                            image_hash = excluded.image_hash,
                            size_bytes = excluded.size_bytes,
                            mtime_ns = excluded.mtime_ns,
                            is_primary = 1,
                            seen_at = excluded.seen_at
                        """,
                        (path, image_hash, size_bytes, mtime_ns, now),
                    )
                    return RecordResult(image_hash, "existing", existing["status"])
                conn.execute(
                    """
                    INSERT INTO paths(path, image_hash, size_bytes, mtime_ns, is_primary, seen_at)
                    VALUES (?, ?, ?, ?, 0, ?)
                    ON CONFLICT(path) DO UPDATE SET
                        image_hash = excluded.image_hash,
                        size_bytes = excluded.size_bytes,
                        mtime_ns = excluded.mtime_ns,
                        is_primary = 0,
                        seen_at = excluded.seen_at
                    """,
                    (path, image_hash, size_bytes, mtime_ns, now),
                )
                return RecordResult(image_hash, "duplicate", existing["status"])
            status = STATUS_FAILED if error else STATUS_PENDING
            conn.execute(
                """
                INSERT INTO posters(image_hash, path, station, direction, size_bytes, width, height,
                                    status, error, attempts, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)
                """,
                (
                    image_hash,
                    path,
                    station,
                    direction,
                    size_bytes,
                    width,
                    height,
                    status,
                    error,
                    now,
                    now,
                ),
            )
            conn.execute(
                """
                INSERT INTO paths(path, image_hash, size_bytes, mtime_ns, is_primary, seen_at)
                VALUES (?, ?, ?, ?, 1, ?)
                ON CONFLICT(path) DO UPDATE SET
                    image_hash = excluded.image_hash,
                    size_bytes = excluded.size_bytes,
                    mtime_ns = excluded.mtime_ns,
                    is_primary = 1,
                    seen_at = excluded.seen_at
                """,
                (path, image_hash, size_bytes, mtime_ns, now),
            )
        return RecordResult(image_hash, "new", status)

    def touch_path(self, path: Path | str, size_bytes: int, mtime_ns: int) -> dict[str, Any] | None:
        with self.connection() as conn, conn:
            row = conn.execute("SELECT * FROM paths WHERE path = ?", (str(path),)).fetchone()
            if not row:
                return None
            conn.execute(
                "UPDATE paths SET size_bytes = ?, mtime_ns = ?, seen_at = ? WHERE path = ?",
                (size_bytes, mtime_ns, utc_now(), str(path)),
            )
            poster = conn.execute(
                "SELECT * FROM posters WHERE image_hash = ?", (row["image_hash"],)
            ).fetchone()
        return dict(poster) if poster else None

    # --------------------------------------------------------------- posters

    def poster(self, image_hash: str) -> dict[str, Any] | None:
        with self.connection() as conn:
            row = conn.execute("SELECT * FROM posters WHERE image_hash = ?", (image_hash,)).fetchone()
        return dict(row) if row else None

    def posters(self) -> list[dict[str, Any]]:
        with self.connection() as conn:
            rows = conn.execute("SELECT * FROM posters ORDER BY path").fetchall()
        return [dict(row) for row in rows]

    def processable(
        self,
        *,
        retry_failed: bool = True,
        max_attempts: int = 3,
        limit: int | None = None,
        only_hashes: Sequence[str] | None = None,
    ) -> list[dict[str, Any]]:
        with self.connection() as conn:
            rows = conn.execute("SELECT * FROM posters ORDER BY path").fetchall()
        selected = []
        wanted = set(only_hashes) if only_hashes else None
        for row in rows:
            item = dict(row)
            if wanted is not None and item["image_hash"] not in wanted:
                continue
            if item["status"] in {STATUS_DONE, STATUS_VLM_REVIEW}:
                continue
            if item["status"] == STATUS_FAILED and (not retry_failed or item["attempts"] >= max_attempts):
                continue
            selected.append(item)
            if limit is not None and len(selected) >= limit:
                break
        return selected

    def set_status(self, image_hash: str, status: str, *, error: str | None = None, attempt: bool = False) -> None:
        now = utc_now()
        with self.connection() as conn, conn:
            if error is None:
                if attempt:
                    conn.execute(
                        "UPDATE posters SET status = ?, attempts = attempts + 1, updated_at = ? WHERE image_hash = ?",
                        (status, now, image_hash),
                    )
                else:
                    conn.execute(
                        "UPDATE posters SET status = ?, updated_at = ? WHERE image_hash = ?",
                        (status, now, image_hash),
                    )
            else:
                if attempt:
                    conn.execute(
                        "UPDATE posters SET status = ?, error = ?, attempts = attempts + 1, updated_at = ? "
                        "WHERE image_hash = ?",
                        (status, error, now, image_hash),
                    )
                else:
                    conn.execute(
                        "UPDATE posters SET status = ?, error = ?, updated_at = ? WHERE image_hash = ?",
                        (status, error, now, image_hash),
                    )

    def mark_block(self, image_hash: str, block_index: int, status: str, route: str = "", error: str = "") -> None:
        with self.connection() as conn, conn:
            conn.execute(
                """
                INSERT INTO blocks(image_hash, block_index, status, route, error, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(image_hash, block_index) DO UPDATE SET
                    status = excluded.status, route = excluded.route, error = excluded.error,
                    updated_at = excluded.updated_at
                """,
                (image_hash, block_index, status, route, error, utc_now()),
            )

    def blocks(self, image_hash: str) -> list[dict[str, Any]]:
        with self.connection() as conn:
            rows = conn.execute(
                "SELECT * FROM blocks WHERE image_hash = ? ORDER BY block_index", (image_hash,)
            ).fetchall()
        return [dict(row) for row in rows]

    def counts(self) -> dict[str, int]:
        with self.connection() as conn:
            rows = conn.execute("SELECT status, COUNT(*) AS n FROM posters GROUP BY status").fetchall()
            duplicate_rows = conn.execute("SELECT COUNT(*) AS n FROM paths WHERE is_primary = 0").fetchone()
        counts = {status: 0 for status in PIPELINE_STATUSES}
        counts.update({row["status"]: int(row["n"]) for row in rows})
        counts["duplicate"] = int(duplicate_rows["n"]) if duplicate_rows else 0
        counts["total"] = sum(int(row["n"]) for row in rows)
        return counts

    # ---------------------------------------------------------------- review

    def replace_review_items(self, image_hash: str, items: Iterable[dict[str, Any]]) -> None:
        now = utc_now()
        with self.connection() as conn, conn:
            conn.execute("DELETE FROM review_items WHERE image_hash = ?", (image_hash,))
            for item in items:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO review_items(
                        image_hash, route, direction, seq, reason, detail, ocr_text, vlm_text,
                        source, score, crop_path, status, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        image_hash,
                        str(item.get("route", "")),
                        str(item.get("direction", "")),
                        int(item.get("seq", 0)),
                        str(item["reason"]),
                        str(item.get("detail", "")),
                        str(item.get("ocr_text", "")),
                        str(item.get("vlm_text", "")),
                        str(item.get("source", "ocr")),
                        item.get("score"),
                        str(item.get("crop_path", "")),
                        str(item.get("status", "open")),
                        now,
                        now,
                    ),
                )

    def review_items(self, *, image_hash: str | None = None, statuses: Sequence[str] | None = None) -> list[dict[str, Any]]:
        query = "SELECT * FROM review_items"
        clauses: list[str] = []
        params: list[Any] = []
        if image_hash:
            clauses.append("image_hash = ?")
            params.append(image_hash)
        if statuses:
            clauses.append("status IN (%s)" % ",".join("?" for _ in statuses))
            params.extend(statuses)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY id"
        with self.connection() as conn:
            rows = conn.execute(query, params).fetchall()
        return sorted((dict(row) for row in rows), key=review_sort_key)

    def add_review_item(self, item: Mapping[str, Any]) -> None:
        now = utc_now()
        with self.connection() as conn, conn:
            conn.execute(
                """
                INSERT INTO review_items(
                    image_hash, route, direction, seq, reason, detail, ocr_text, vlm_text,
                    source, score, crop_path, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(image_hash, route, direction, seq, reason) DO UPDATE SET
                    detail = excluded.detail,
                    ocr_text = excluded.ocr_text,
                    source = excluded.source,
                    score = excluded.score,
                    crop_path = excluded.crop_path,
                    status = 'open',
                    updated_at = excluded.updated_at
                """,
                (
                    str(item["image_hash"]),
                    str(item.get("route", "")),
                    str(item.get("direction", "")),
                    int(item.get("seq", 0)),
                    str(item["reason"]),
                    str(item.get("detail", "")),
                    str(item.get("ocr_text", "")),
                    "",
                    str(item.get("source", "ocr")),
                    item.get("score"),
                    str(item.get("crop_path", "")),
                    "open",
                    now,
                    now,
                ),
            )

    def delete_review_items_by_reason(self, reason: str) -> int:
        with self.connection() as conn, conn:
            cursor = conn.execute("DELETE FROM review_items WHERE reason = ?", (reason,))
        return int(cursor.rowcount)

    def set_review_status(self, item_id: int, status: str, *, vlm_text: str | None = None, source: str | None = None) -> None:
        assignments = ["status = ?", "updated_at = ?"]
        params: list[Any] = [status, utc_now()]
        if vlm_text is not None:
            assignments.append("vlm_text = ?")
            params.append(vlm_text)
        if source is not None:
            assignments.append("source = ?")
            params.append(source)
        params.append(item_id)
        with self.connection() as conn, conn:
            conn.execute(f"UPDATE review_items SET {', '.join(assignments)} WHERE id = ?", params)

    def review_counts(self) -> dict[str, int]:
        with self.connection() as conn:
            rows = conn.execute("SELECT status, COUNT(*) AS n FROM review_items GROUP BY status").fetchall()
        return {row["status"]: int(row["n"]) for row in rows}

    def export_review_csv(self, path: Path | str) -> int:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as conn:
            rows = conn.execute(
                """
                SELECT r.id, p.station, p.path AS image_path, r.route, r.direction, r.seq, r.reason,
                       r.detail, r.ocr_text,
                       CASE WHEN o.name IS NOT NULL THEN
                           o.name || CASE WHEN o.note <> '' THEN '（' || o.note || '）' ELSE '' END
                       ELSE '' END AS corrected_text,
                       r.vlm_text, r.source, r.score, r.status, r.crop_path
                FROM review_items r
                JOIN posters p ON p.image_hash = r.image_hash
                LEFT JOIN manual_overrides o
                    ON o.image_hash = r.image_hash AND o.route = r.route
                   AND o.direction = r.direction AND o.seq = r.seq
                ORDER BY r.image_hash, r.route, r.seq, r.id
                """
            ).fetchall()
        with path.open("w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.writer(handle)
            writer.writerow(
                [
                    "id",
                    "站点",
                    "图片",
                    "线路",
                    "方向",
                    "站序",
                    "原因",
                    "详情",
                    "OCR结果",
                    "修正值",
                    "VLM结果",
                    "来源",
                    "置信度",
                    "状态",
                    "裁片",
                ]
            )
            for row in sorted((dict(item) for item in rows), key=review_sort_key):
                writer.writerow(
                    [
                        row["id"],
                        row["station"],
                        row["image_path"],
                        row["route"],
                        row["direction"],
                        row["seq"],
                        row["reason"],
                        row["detail"],
                        row["ocr_text"],
                        row["corrected_text"],
                        row["vlm_text"],
                        row["source"],
                        row["score"],
                        row["status"],
                        row["crop_path"],
                    ]
                )
        return len(rows)

    # ------------------------------------------------------------- overrides

    def set_override(
        self,
        image_hash: str,
        route: str,
        direction: str,
        seq: int,
        name: str,
        note: str = "",
    ) -> None:
        with self.connection() as conn, conn:
            conn.execute(
                """
                INSERT INTO manual_overrides(image_hash, route, direction, seq, name, note, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(image_hash, route, direction, seq) DO UPDATE SET
                    name = excluded.name, note = excluded.note, updated_at = excluded.updated_at
                """,
                (image_hash, route, direction, int(seq), name, note, utc_now()),
            )

    def overrides(self, image_hash: str) -> list[dict[str, Any]]:
        with self.connection() as conn:
            rows = conn.execute(
                "SELECT * FROM manual_overrides WHERE image_hash = ? ORDER BY route, direction, seq",
                (image_hash,),
            ).fetchall()
        return [dict(row) for row in rows]

    # ------------------------------------------------------------------ meta

    def set_meta(self, key: str, value: str) -> None:
        with self.connection() as conn, conn:
            conn.execute(
                "INSERT INTO meta(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

    def get_meta(self, key: str, default: str = "") -> str:
        with self.connection() as conn:
            row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return str(row["value"]) if row else default


def scan_directory(
    store: ManifestStore,
    source_dir: Path | str,
    *,
    station_fallback: str = "",
) -> dict[str, Any]:
    """Scan JPGs into the manifest, hashing only new or changed files."""
    from buspipe.config import poster_direction, poster_station

    source_dir = Path(source_dir)
    if not source_dir.exists():
        raise FileNotFoundError(f"source directory not found: {source_dir}")
    summary = {"discovered": 0, "new": 0, "duplicates": 0, "unchanged": 0, "failed": 0, "paths": []}
    for path in sorted(source_dir.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in {".jpg", ".jpeg"}:
            continue
        summary["discovered"] += 1
        resolved = str(path.resolve())
        stat = path.stat()
        known = store.path_record(resolved)
        if known and known["size_bytes"] == stat.st_size and known["mtime_ns"] == stat.st_mtime_ns:
            poster = store.poster(known["image_hash"])
            if poster:
                summary["unchanged"] += 1
                summary["paths"].append({"path": resolved, "hash": known["image_hash"], "state": "unchanged"})
                continue
        try:
            image_hash = hash_file(path)
        except OSError as exc:
            summary["failed"] += 1
            summary["paths"].append({"path": resolved, "hash": "", "state": "failed", "error": str(exc)})
            continue
        width = height = None
        error = ""
        try:
            from PIL import Image

            with Image.open(path) as image:
                width, height = image.size
        except Exception as exc:  # corrupt or unsupported image
            error = f"unreadable image: {exc}"
        stem = path.stem
        station = poster_station(stem, station_fallback)
        direction = poster_direction(stem)
        result = store.record_image(
            image_hash=image_hash,
            path=path,
            size_bytes=stat.st_size,
            mtime_ns=stat.st_mtime_ns,
            width=width,
            height=height,
            station=station,
            direction=direction,
            error=error,
        )
        if result.state == "new":
            summary["new"] += 1
        elif result.state == "duplicate":
            summary["duplicates"] += 1
        else:
            summary["unchanged"] += 1
        if error and result.state == "new":
            summary["failed"] += 1
        summary["paths"].append({"path": resolved, "hash": image_hash, "state": result.state, "error": error})
    return summary
