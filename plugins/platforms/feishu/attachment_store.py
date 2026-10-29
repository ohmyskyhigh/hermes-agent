"""Durable, profile-local Feishu attachment references, separate from downloads."""
from contextlib import closing, contextmanager
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import stat

_RESULT_FIELDS = frozenset(("state", "path", "mime_type", "size_bytes", "sha256",
                            "attempts", "error_kind", "error_code", "retryable"))
_ERROR_KINDS = frozenset(("", "permission", "not_found", "too_large", "size_limit", "busy", "network",
                         "filesystem", "internal", "timeout", "authentication", "redirect", "http",
                         "protocol", "cache_invalid", "download_failed", "sdk_unavailable",
                         "invalid_input", "media_mismatch", "api"))
_ERROR_CODES = frozenset(("", "timeout", "connection", "tls", "deadline", "queue_timeout",
                         "content_encoding", "length_mismatch", "image_cover", "range_mismatch",
                         "etag_mismatch", "auth_response_too_large", "auth_response_invalid",
                         "unsupported_app_type"))


def _validate_result(result):
    if (type(result) is not dict or result.keys() - _RESULT_FIELDS
            or result.get("state") not in ("pending", "ready", "failed")):
        raise ValueError("attachment result: invalid fields")
    for key, value in result.items():
        if key in ("size_bytes", "attempts"):
            valid = type(value) is int and 0 <= value <= 2 ** 63 - 1
        elif key == "retryable":
            valid = type(value) is bool
        else:
            valid = type(value) is str and len(value) <= 4096 and all(c.isprintable() for c in value)
        if not valid:
            raise ValueError("attachment result: invalid value")
    if result.get("error_kind", "") not in _ERROR_KINDS:
        raise ValueError("attachment result: unknown error classification")
    code = result.get("error_code", "")
    if code not in _ERROR_CODES and not re.fullmatch(r"[0-9]{1,12}", code):
        raise ValueError("attachment result: invalid error code")
    if result.get("sha256") and not re.fullmatch(r"[0-9a-f]{64}", result["sha256"]):
        raise ValueError("attachment result: invalid digest")
    if result.get("mime_type") and not re.fullmatch(r"[a-zA-Z0-9!#$&^_.+-]{1,63}/[a-zA-Z0-9!#$&^_.+-]{1,127}", result["mime_type"]):
        raise ValueError("attachment result: invalid media type")
    clean = dict(result)
    if clean["state"] != "ready" and "path" in clean:
        clean["path"] = ""
    return clean


def _is_link(path):
    try:
        info = path.lstat()
        return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & 0x400)
    except FileNotFoundError:
        return False


class AttachmentStore:
    """Synchronous ledger. Each operation closes its connection for worker safety."""

    def __init__(self, home: Path):
        self.home = Path(home).resolve()
        self.cache_dir = self.home / "cache"
        if _is_link(self.cache_dir):
            raise ValueError("attachment cache must not be a link")
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.cache_dir / "feishu-attachments.sqlite3"
        with self._connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS attachments (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                attachment_id TEXT NOT NULL UNIQUE,
                app_id TEXT NOT NULL, chat_id TEXT NOT NULL, thread_id TEXT NOT NULL,
                ref_json TEXT NOT NULL, result_json TEXT
            )""")

    @contextmanager
    def _connect(self):
        candidates = (self.cache_dir, self.path, *(self.path.with_name(self.path.name + suffix)
                       for suffix in ("-journal", "-wal", "-shm")))
        if any(_is_link(path) for path in candidates):
            raise ValueError("attachment cache must not contain ledger links")
        with closing(sqlite3.connect(self.path, timeout=10)) as db, db:
            yield db

    @staticmethod
    def _id(ref):
        scope = [ref[key] for key in (
            "platform", "app_id", "chat_id", "thread_id", "message_id", "file_key")]
        return hashlib.sha256(json.dumps(scope, separators=(",", ":")).encode()).hexdigest()

    def save(self, ref: dict, result: dict | None = None) -> str:
        from gateway.attachment_refs import validate_attachment_refs

        ref = validate_attachment_refs([ref])[0]
        replace = result is not None
        result = _validate_result(result) if replace else {"state": ref["state"]}
        attachment_id = self._id(ref)
        with self._connect() as db:
            db.execute("""INSERT INTO attachments
                (attachment_id, app_id, chat_id, thread_id, ref_json, result_json)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(attachment_id) DO UPDATE SET
                ref_json=excluded.ref_json, result_json=excluded.result_json
                WHERE ?""",
                (attachment_id, ref["app_id"], ref["chat_id"], ref["thread_id"],
                 json.dumps(ref), json.dumps(result), replace))
        return attachment_id

    def _cache_valid(self, result):
        try:
            path = Path(result["path"])
            if not path.is_relative_to(self.cache_dir):
                return False
            if any(_is_link(part) for part in (path, *path.parents) if part != self.home):
                return False
            resolved = path.resolve(strict=True)
            if (not path.is_absolute() or not resolved.is_relative_to(self.cache_dir)
                    or not resolved.is_file() or resolved.stat().st_size != result["size_bytes"]):
                return False
            digest = hashlib.sha256()
            with resolved.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
            return digest.hexdigest() == result["sha256"]
        except (OSError, KeyError, TypeError, ValueError):
            return False

    def _record(self, row, scope):
        from gateway.attachment_refs import validate_attachment_refs

        try:
            ref = validate_attachment_refs([json.loads(row[1])])[0]
            result = _validate_result(json.loads(row[2]))
        except (ValueError, TypeError):
            return None
        if self._id(ref) != row[0] or tuple(ref[k] for k in ("app_id", "chat_id", "thread_id")) != scope:
            return None
        if result.get("state") == "ready" and not self._cache_valid(result):
            result = {"state": "failed", "path": "", "error_kind": "cache_invalid", "retryable": True}
        return {"attachment_id": row[0], "ref": ref, "result": result}

    def list(self, app_id, chat_id, thread_id="", limit=10) -> list[dict]:
        limit = max(1, min(20, int(limit)))
        with self._connect() as db:
            rows = db.execute("""SELECT attachment_id, ref_json, result_json FROM attachments
                WHERE app_id=? AND chat_id=? AND thread_id=?
                ORDER BY sequence DESC LIMIT ?""", (app_id, chat_id, thread_id, limit)).fetchall()
        return [record for row in rows if (record := self._record(row, (app_id, chat_id, thread_id))) is not None]

    def get(self, id, app_id, chat_id, thread_id="") -> dict | None:
        with self._connect() as db:
            row = db.execute("""SELECT attachment_id, ref_json, result_json FROM attachments
                WHERE attachment_id=? AND app_id=? AND chat_id=? AND thread_id=?""",
                (id, app_id, chat_id, thread_id)).fetchone()
        return self._record(row, (app_id, chat_id, thread_id)) if row else None
