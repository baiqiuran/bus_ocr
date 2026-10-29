"""Local-only OCR workbench. Run: python -m buspipe.web."""
from __future__ import annotations

import argparse
import csv
import io
import json
import os
import secrets
import sqlite3
import subprocess
import sys
import threading
import uuid
from contextlib import closing
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent.parent
STATIC = Path(__file__).with_name("web_static")
ACTIVE = {"scanning", "running"}


def now():
    return datetime.now(timezone.utc).isoformat()


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def read_records(path):
    rows = read_json(path)
    if not isinstance(rows, list):
        raise ValueError("station_routes.json 必须是记录数组。")
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("线路记录必须是 JSON 对象。")
        for key in ("station", "direction", "station_key", "route", "first_stop", "last_stop", "poster"):
            if not isinstance(row.get(key, ""), str):
                raise ValueError(f"线路记录的 {key} 必须是字符串。")
        if type(row.get("needs_review", False)) is not bool or type(row.get("block_index", 0)) is not int:
            raise ValueError("复核标记必须是布尔值，板块编号必须是整数。")
    return rows


def manifest_rows(path, table):
    if table not in {"posters", "review_items"} or not Path(path).is_file():
        return []
    with closing(sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True, timeout=5)) as db:
        db.row_factory = sqlite3.Row
        return [dict(row) for row in db.execute(f"SELECT * FROM {table}")]


class Workbench:
    def __init__(self, data_dir):
        self.data = Path(data_dir).resolve()
        self.data.mkdir(parents=True, exist_ok=True)
        self.database = self.data / "workbench.sqlite"
        self.lock = threading.Lock()
        self.process = None
        self.stopping = False
        with self.connect() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL, source TEXT NOT NULL,
                    output TEXT NOT NULL UNIQUE, manifest TEXT NOT NULL,
                    status TEXT NOT NULL, created TEXT NOT NULL, updated TEXT NOT NULL,
                    error TEXT NOT NULL DEFAULT '', imported INTEGER NOT NULL DEFAULT 0,
                    report TEXT NOT NULL DEFAULT '{}');
                CREATE TABLE IF NOT EXISTS records (
                    id INTEGER PRIMARY KEY, job_id TEXT NOT NULL REFERENCES jobs(id),
                    station TEXT, direction TEXT, station_key TEXT, route TEXT,
                    first_stop TEXT, last_stop TEXT, route_direction TEXT,
                    needs_review INTEGER NOT NULL, poster TEXT, block_index INTEGER,
                    image_hash TEXT, image_path TEXT, payload TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS records_job ON records(job_id);
                CREATE INDEX IF NOT EXISTS records_station ON records(station, route);
            """)
            db.execute("UPDATE jobs SET status='interrupted', error=? WHERE status IN ('running','scanning')",
                       ("服务曾中断；可继续处理，已完成图片会跳过。",))

    def connect(self):
        # Context manager closes as well as commits; one connection per request.
        from contextlib import contextmanager

        @contextmanager
        def connection():
            with closing(sqlite3.connect(self.database, timeout=30)) as db:
                db.row_factory = sqlite3.Row
                db.execute("PRAGMA foreign_keys=ON")
                with db:
                    yield db
        return connection()

    def job(self, job_id):
        with self.connect() as db:
            row = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if row is None:
            raise ValueError("找不到这个批次，请刷新页面。")
        return dict(row)

    def update(self, job_id, **values):
        values["updated"] = now()
        with self.connect() as db:
            db.execute("UPDATE jobs SET " + ",".join(f"{key}=?" for key in values) + " WHERE id=?",
                       (*values.values(), job_id))

    def create(self, source):
        path = Path(source).expanduser().resolve()
        if not path.is_dir():
            raise ValueError("目录不存在或不可访问，请填写本机图片目录的完整路径。")
        with self.lock:
            self.ensure_idle()
            job_id = uuid.uuid4().hex
            output = self.data / "batches" / job_id
            output.mkdir(parents=True)
            with self.connect() as db:
                db.execute("INSERT INTO jobs(id,name,source,output,manifest,status,created,updated) VALUES(?,?,?,?,?,?,?,?)",
                           (job_id, path.name, str(path), str(output), str(output / ".batch" / "manifest.sqlite"), "scanning", now(), now()))
            threading.Thread(target=self.run, args=(job_id, True, None), daemon=True).start()
        return {"id": job_id}

    def ensure_idle(self):
        with self.connect() as db:
            active = db.execute("SELECT id FROM jobs WHERE status IN ('running','scanning')").fetchone()
        if active or self.stopping:
            raise ValueError("已有批次正在处理，请等待当前批次结束。")

    def start(self, job_id, sample=False):
        with self.lock:
            self.ensure_idle()
            job = self.job(job_id)
            if job["imported"]:
                raise ValueError("历史批次为只读导入；重新识别请新建批次。")
            self.update(job_id, status="running", error="")
            threading.Thread(target=self.run, args=(job_id, False, 3 if sample else None), daemon=True).start()
        return {"id": job_id}

    def run(self, job_id, dry_run, limit):
        job = self.job(job_id)
        command = [sys.executable, "-m", "buspipe", "run", job["source"], "--out", job["output"],
                   "--extract-mode", "endpoints", "--workers", "1", "--threads", "4", "--no-vlm"]
        if dry_run:
            command.append("--dry-run")
        if limit:
            command += ["--limit", str(limit)]
        log = Path(job["output"]) / "process.log"
        try:
            with log.open("w", encoding="utf-8") as handle:
                with self.lock:
                    if self.stopping:
                        self.update(job_id, status="interrupted")
                        return
                    process = subprocess.Popen(command, cwd=ROOT, stdout=handle, stderr=subprocess.STDOUT,
                                               env={**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"},
                                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                    self.process = process
                code = process.wait()
                with self.lock:
                    self.process = None
            if self.stopping:
                self.update(job_id, status="interrupted", error="服务已停止，可继续批次。")
            elif code:
                self.update(job_id, status="failed", error=log.read_text(encoding="utf-8")[-6000:])
            elif dry_run:
                text = log.read_text(encoding="utf-8")
                summary = json.loads(text)
                self.update(job_id, status="ready", report=json.dumps(summary, ensure_ascii=False))
            else:
                self.ingest(job_id)
        except Exception as exc:
            self.update(job_id, status="failed", error=str(exc))

    def import_output(self, directory):
        output = Path(directory).expanduser().resolve()
        report = read_json(output / "batch_report.json")
        if report.get("extract_mode") != "endpoints":
            raise ValueError("仅支持 endpoints 精简模式结果，请选择对应的输出目录。")
        read_records(output / "station_routes.json")
        with self.lock:
            with self.connect() as db:
                old = db.execute("SELECT id FROM jobs WHERE output=?", (str(output),)).fetchone()
                if old:
                    return {"id": old["id"]}
                job_id = uuid.uuid4().hex
                manifest = output / ".batch" / "manifest.sqlite"
                reported = Path(report.get("manifest_path", ""))
                if not manifest.is_file() and reported.is_file():
                    manifest = reported.resolve()
                db.execute("INSERT INTO jobs(id,name,source,output,manifest,status,created,updated,imported) VALUES(?,?,?,?,?,?,?,?,1)",
                           (job_id, output.name, report.get("source_dir", ""), str(output), str(manifest), "importing", now(), now()))
            try:
                self.ingest(job_id)
            except Exception:
                with self.connect() as db:
                    db.execute("DELETE FROM jobs WHERE id=?", (job_id,))
                raise
        return {"id": job_id}

    def ingest(self, job_id):
        job = self.job(job_id)
        output = Path(job["output"])
        report = read_json(output / "batch_report.json")
        if report.get("extract_mode") != "endpoints":
            raise ValueError("批次输出模式不一致，未写入结果。")
        rows = read_records(output / "station_routes.json")
        posters = manifest_rows(job["manifest"], "posters")
        index = {}
        for poster in posters:
            index.setdefault(Path(poster["path"]).stem, []).append(poster)
        counts = report.get("counts", {})
        status = "partial" if counts.get("failed", 0) or counts.get("incomplete", 0) else "completed"
        with self.connect() as db:
            db.execute("DELETE FROM records WHERE job_id=?", (job_id,))
            for row in rows:
                sources = index.get(row.get("poster", ""), [])
                source = sources[0] if len(sources) == 1 else {}
                first, last = row.get("first_stop", ""), row.get("last_stop", "")
                review = bool(row.get("needs_review")) or not all((row.get("station"), row.get("route"), first, last, row.get("direction"))) or row.get("direction") == "未知"
                db.execute("""INSERT INTO records(job_id,station,direction,station_key,route,first_stop,last_stop,
                    route_direction,needs_review,poster,block_index,image_hash,image_path,payload)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                           (job_id, row.get("station", ""), row.get("direction", ""), row.get("station_key", ""), row.get("route", ""),
                            first, last, f"{first}→{last}" if first and last else "", int(review), row.get("poster", ""), row.get("block_index", 0),
                            source.get("image_hash", ""), source.get("path", ""), json.dumps(row, ensure_ascii=False)))
            db.execute("UPDATE jobs SET status=?,report=?,updated=?,error='' WHERE id=?",
                       (status, json.dumps(report, ensure_ascii=False), now(), job_id))

    def overview(self):
        with self.connect() as db:
            jobs = [dict(row) for row in db.execute("SELECT * FROM jobs ORDER BY created DESC")]
            stats = dict(db.execute("SELECT COUNT(*) records, COUNT(DISTINCT NULLIF(station_key,'')) stations, COUNT(DISTINCT NULLIF(route,'')) routes, COALESCE(SUM(needs_review),0) review FROM records").fetchone())
        for job in jobs:
            job["report"] = json.loads(job["report"])
        return {"jobs": jobs, "stats": stats, "database": str(self.database)}

    def detail(self, job_id):
        job = self.job(job_id)
        job["report"] = json.loads(job["report"])
        posters = manifest_rows(job["manifest"], "posters")
        counts = {"total": len(posters), "done": 0, "failed": 0, "skipped": 0}
        for poster in posters:
            counts[poster["status"]] = counts.get(poster["status"], 0) + 1
        counts["incomplete"] = counts["total"] - sum(counts[key] for key in ("done", "failed", "skipped"))
        job["counts"] = counts if posters else job["report"].get("counts", counts)
        job["posters"] = posters
        job["reviews"] = manifest_rows(job["manifest"], "review_items")
        return job

    def records(self, query):
        terms, args = [], []
        for key, field in (("job", "job_id"), ("direction", "direction")):
            if query.get(key):
                terms.append(f"r.{field}=?")
                args.append(query[key])
        if query.get("review") == "1":
            terms.append("r.needs_review=1")
        if query.get("q"):
            terms.append("(r.station LIKE ? ESCAPE '\\' OR r.route LIKE ? ESCAPE '\\' OR r.poster LIKE ? ESCAPE '\\')")
            word = query["q"].replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            args.extend([f"%{word}%"] * 3)
        where = " WHERE " + " AND ".join(terms) if terms else ""
        page = max(1, int(query.get("page", "1")))
        with self.connect() as db:
            total = db.execute("SELECT COUNT(*) FROM records r" + where, args).fetchone()[0]
            rows = [dict(row) for row in db.execute("SELECT r.*,j.name batch_name FROM records r JOIN jobs j ON j.id=r.job_id" + where + " ORDER BY r.id LIMIT 50 OFFSET ?", (*args, (page-1)*50))]
        return {"rows": rows, "total": total, "page": page, "pages": max(1, (total+49)//50)}

    def record(self, record_id):
        with self.connect() as db:
            row = db.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        if row is None:
            raise ValueError("找不到这条记录。")
        record = dict(row)
        job = self.job(record["job_id"])
        record["reviews"] = [item for item in manifest_rows(job["manifest"], "review_items")
                             if item["image_hash"] == record["image_hash"] and item["route"] in {"", record["route"]}]
        record["output"] = job["output"]
        return record

    def close(self):
        with self.lock:
            self.stopping = True
            process = self.process
        if process and process.poll() is None:
            process.terminate()
            process.wait(timeout=15)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def send(self, data, mime="application/json; charset=utf-8", status=200, disposition=None):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8") if mime.startswith("application/json") else data
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'; img-src 'self'; style-src 'self'; script-src 'self'; frame-ancestors 'none'")
        if disposition:
            self.send_header("Content-Disposition", disposition)
        self.end_headers()
        self.wfile.write(body)

    def valid_host(self):
        return self.headers.get("Host") in {f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}"}

    def do_GET(self):
        if not self.valid_host():
            return self.send({"error": "仅允许本机访问"}, status=403)
        try:
            url = urlparse(self.path)
            query = {k: v[0] for k, v in parse_qs(url.query).items()}
            path = url.path
            app = self.server.app
            if path == "/api/session":
                return self.send({"token": self.server.token})
            if path == "/api/jobs":
                return self.send(app.overview())
            if path.startswith("/api/jobs/"):
                return self.send(app.detail(path.rsplit("/", 1)[1]))
            if path == "/api/records":
                return self.send(app.records(query))
            if path.startswith("/api/records/"):
                return self.send(app.record(int(path.rsplit("/", 1)[1])))
            if path.startswith("/api/image/"):
                record = app.record(int(path.rsplit("/", 1)[1]))
                image = Path(record["image_path"])
                if image.is_file() and image.suffix.lower() in {".jpg", ".jpeg"}:
                    return self.send(image.read_bytes(), "image/jpeg")
                return self.send({"error": "来源图片不可用或来源同名不唯一"}, status=404)
            if path.startswith("/api/export/"):
                job_id = path.rsplit("/", 1)[1]
                app.job(job_id)
                with app.connect() as db:
                    rows = [dict(row) for row in db.execute("SELECT station,direction,station_key,route,first_stop,last_stop,route_direction,needs_review,poster,block_index,image_hash,image_path FROM records WHERE job_id=? ORDER BY id", (job_id,))]
                fields = ["station", "direction", "station_key", "route", "first_stop", "last_stop", "route_direction", "needs_review", "poster", "block_index", "image_hash", "image_path"]
                stream = io.StringIO(newline="")
                writer = csv.DictWriter(stream, fieldnames=fields)
                writer.writeheader()
                for row in rows:
                    writer.writerow({k: "'" + v if isinstance(v, str) and v.startswith(("=", "+", "-", "@", "\t", "\r")) else v for k, v in row.items()})
                return self.send(stream.getvalue().encode("utf-8-sig"), "text/csv; charset=utf-8", disposition='attachment; filename="station_routes.csv"')
            files = {"/": (STATIC / "index.html", "text/html; charset=utf-8"),
                     "/app.js": (STATIC / "app.js", "text/javascript; charset=utf-8"),
                     "/app.css": (STATIC / "app.css", "text/css; charset=utf-8"),
                     "/tokens.css": (ROOT / "tokens.css", "text/css; charset=utf-8")}
            if path in files:
                file, mime = files[path]
                return self.send(file.read_bytes(), mime)
            self.send({"error": "页面不存在"}, status=404)
        except (ValueError, OSError, sqlite3.Error) as exc:
            self.send({"error": str(exc)}, status=400)

    def do_POST(self):
        origin = self.headers.get("Origin")
        allowed = {f"http://127.0.0.1:{self.server.server_port}", f"http://localhost:{self.server.server_port}"}
        if not self.valid_host() or (origin and origin not in allowed) or not secrets.compare_digest(self.headers.get("X-Workbench-Token", ""), self.server.token):
            return self.send({"error": "会话失效，请刷新页面。"}, status=403)
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if not 0 < size < 16384:
                raise ValueError("请求大小不正确。")
            payload = json.loads(self.rfile.read(size))
            if not isinstance(payload, dict):
                raise ValueError("请求必须是 JSON 对象。")
            if self.path in {"/api/jobs", "/api/import"}:
                source = payload.get("path")
                if not isinstance(source, str) or not source.strip():
                    raise ValueError("请填写目录路径。")
                action = self.server.app.create if self.path == "/api/jobs" else self.server.app.import_output
                return self.send(action(source.strip()))
            if self.path == "/api/start":
                return self.send(self.server.app.start(str(payload.get("id", "")), payload.get("sample") is True))
            self.send({"error": "接口不存在"}, status=404)
        except (ValueError, OSError, sqlite3.Error) as exc:
            self.send({"error": str(exc)}, status=400)


def make_server(app, port=8765):
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.app = app
    server.token = secrets.token_urlsafe(32)
    return server


def main():
    parser = argparse.ArgumentParser(description="站牌 OCR 本地工作台")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--data-dir", type=Path, default=ROOT / "output" / "workbench")
    parser.add_argument("--import-output", type=Path, help="启动前导入已有 endpoints 输出目录（只读）")
    args = parser.parse_args()
    # Bind before recovery: a second launch on the same port must not reset a running job.
    server = make_server(None, args.port)
    app = Workbench(args.data_dir)
    server.app = app
    try:
        if args.import_output:
            app.import_output(args.import_output)
        print(f"Bus OCR: http://127.0.0.1:{server.server_port}\nSQLite: {app.database}", flush=True)
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        app.close()
        server.server_close()


if __name__ == "__main__":
    main()
