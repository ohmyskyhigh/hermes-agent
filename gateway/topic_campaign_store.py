"""Durable topic ownership, work records, supervision leases and delivery checkpoints.

One shared SQLite file is explicitly configured for the participating gateways. Writes use
IMMEDIATE transactions; a gateway restart leaves overdue work due and expires claimed leases.
"""
from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
import uuid

ACTIVE = {"running", "blocked"}
TERMINAL = {"completed", "cancelled"}
WORK_DONE = {"done", "cancelled"}


from gateway.topic_campaign_milestones import MilestoneStore, migrate, project
from gateway.topic_campaign_dialogue_store import DialogueStore, migrate_dialogue, remember_input, apply_amendments


class CampaignStore(MilestoneStore, DialogueStore):
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS campaigns (
                    key TEXT PRIMARY KEY, data TEXT NOT NULL,
                    status TEXT NOT NULL, next_check REAL NOT NULL, next_report REAL NOT NULL,
                    lease_until REAL NOT NULL DEFAULT 0, lease_token TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS campaign_events (
                    id INTEGER PRIMARY KEY, campaign TEXT NOT NULL, dedup TEXT NOT NULL,
                    kind TEXT NOT NULL, profile TEXT NOT NULL, text TEXT NOT NULL, at REAL NOT NULL,
                    UNIQUE(campaign, dedup)
                );
                CREATE INDEX IF NOT EXISTS campaign_due ON campaigns(status,next_check,next_report);
            """)

        with self.transaction() as conn:
            migrate(conn)
            migrate_dialogue(conn)

    @contextmanager
    def connection(self):
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()

    @contextmanager
    def transaction(self):
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
                conn.commit()
            except BaseException:
                conn.rollback()
                raise

    @staticmethod
    def _read(conn, key):
        row = conn.execute("SELECT data FROM campaigns WHERE key=?", (key,)).fetchone()
        return project(conn, json.loads(row[0])) if row else None

    @staticmethod
    def _save(conn, item):
        conn.execute("""UPDATE campaigns SET data=?, status=?, next_check=?, next_report=? WHERE key=?""",
                     (json.dumps({k: v for k, v in item.items() if k not in {"topic_key", "milestone_no", "previous_campaign_key", "trigger_message_id"}}, ensure_ascii=False), item["status"], item["next_check_at"],
                      item["next_report_at"], item["key"]))

    def get(self, key):
        with self.connection() as conn:
            return self._read(conn, key)

    def list(self):
        with self.connection() as conn:
            return [project(conn, json.loads(r[0])) for r in conn.execute("SELECT data FROM campaigns ORDER BY key")]

    def ingest(self, *, key, owner, profile, source, transport_profile, message_id, text,
               root_message_id, is_bot, targets, now, review_interval, report_interval):
        """Capture each incoming message once, plus each bot's separately authorized route."""
        with self.transaction() as conn:
            return self._ingest(conn, key=key, owner=owner, profile=profile, source=source,
                transport_profile=transport_profile, message_id=message_id, text=text,
                root_message_id=root_message_id, is_bot=is_bot, targets=targets, now=now,
                review_interval=review_interval, report_interval=report_interval)

    def _ingest(self, conn, *, key, owner, profile, source, transport_profile, message_id, text,
                root_message_id, is_bot, targets, now, review_interval, report_interval):
        item = self._read(conn, key)
        if item is None:
            if is_bot:
                return None
            item = dict(key=key, goal=text, done_when="", owner_profile=owner,
                        participating_profiles=[owner], status="running", progress="尚未完成首次检查",
                        next_action="负责人确认目标、完成标准并开始执行", blocker="", evidence="",
                        routes={}, work={}, root_message_id=root_message_id or message_id,
                        created_at=now, updated_at=now, last_progress_at=now, last_reconciled_at=0,
                        last_reported_at=0, next_check_at=now, next_review_at=now,
                        next_report_at=now + report_interval, review_interval=review_interval,
                        report_interval=report_interval, wake_pending_until=0, last_error="",
                        revision=0, final_report_pending=False)
            conn.execute("INSERT INTO campaigns(key,data,status,next_check,next_report) VALUES (?,?,?,?,?)",
                         (key, "{}", "running", now, item["next_report_at"]))
        conn.execute("INSERT OR IGNORE INTO campaign_milestones VALUES (?,?,?,?,?)",
                     (key, key, 1, None, message_id))
        conn.execute("INSERT OR IGNORE INTO campaign_message_bindings VALUES (?,?,?,?,?,?)",
                     (project(conn, item).get("topic_key", key), message_id, key, "bound", None, now))
        if item["status"] in TERMINAL:
            return project(conn, item)
        # A bot-authored handoff must never replace the human source used for scheduled wakes.
        if not is_bot:
            item["routes"][profile] = dict(source=source, transport_profile=transport_profile)
        new = conn.execute("""INSERT OR IGNORE INTO campaign_events
            (campaign,dedup,kind,profile,text,at) VALUES (?,?,?,?,?,?)""",
            (key, "message:" + message_id, "bot" if is_bot else "human", profile, text, now)).rowcount
        remember_input(conn, item, message_id, is_bot=is_bot, targets=targets, profile=profile,
                       source=source, text=text, new=new)
        if new:
            item["next_review_at"] = min(item["next_review_at"], now)
            item["next_check_at"] = min(item["next_check_at"], now)
            item["updated_at"] = now
            item["revision"] += 1
        original = conn.execute('SELECT mode FROM campaign_inputs WHERE campaign_key=? AND message_id=?',
                                (key, message_id)).fetchone()
        if item["status"] in ACTIVE and (not original or original['mode'] != 'paused_dialogue'):
            for target in targets:
                if target not in item["participating_profiles"]:
                    item["participating_profiles"].append(target)
                if target != item["owner_profile"]:
                    work_id = f"message:{message_id}:{target}"
                    item["work"].setdefault(work_id, dict(id=work_id, profile=target,
                        status="pending", summary=text, updated_at=now, kind="request"))
        self._save(conn, item)
        return project(conn, item)

    def events(self, key, limit=12):
        with self.connection() as conn:
            return [dict(r) for r in conn.execute("""SELECT kind,profile,text,at FROM campaign_events
                WHERE campaign=? ORDER BY id DESC LIMIT ?""", (key, limit))][::-1]

    def update(self, key, profile, changes, now):
        allowed = {"goal", "done_when", "progress", "next_action", "blocker", "evidence", "status"}
        if set(changes) - allowed:
            raise ValueError("unsupported campaign fields")
        with self.transaction() as conn:
            item = self._read(conn, key)
            if item is None or item["owner_profile"] != profile:
                raise ValueError("only the campaign owner can update the campaign")
            if item["status"] not in ACTIVE:
                raise ValueError("campaign is stopped; only a human resume may reopen it")
            updated = {**item, **changes}
            if updated["status"] not in ACTIVE | {"completed"}:
                raise ValueError("owner may set running, blocked or completed")
            if updated["status"] == "blocked" and not updated["blocker"].strip():
                raise ValueError("blocked campaigns require a concrete blocker")
            if updated["status"] == "running" and not updated["next_action"].strip():
                raise ValueError("running campaigns require a next action")
            if updated["status"] == "completed":
                if not updated["done_when"].strip() or not updated["evidence"].strip():
                    raise ValueError("completion requires acceptance criteria and evidence")
                if any(w["status"] not in WORK_DONE for w in item["work"].values()):
                    raise ValueError("unfinished work prevents campaign completion")
                updated["final_report_pending"] = True
                updated["next_report_at"] = now
            if updated["progress"] != item["progress"]:
                updated["last_progress_at"] = now
            updated.update(updated_at=now, last_reconciled_at=now, wake_pending_until=0,
                           next_review_at=now + item["review_interval"], revision=item["revision"] + 1)
            self._save(conn, updated)
            conn.execute("""INSERT INTO campaign_events(campaign,dedup,kind,profile,text,at)
                VALUES (?,?,?,?,?,?)""", (key, uuid.uuid4().hex, "review", profile,
                                         json.dumps(changes, ensure_ascii=False), now))
            return updated

    def work_update(self, key, profile, work_id, changes, now):
        if set(changes) - {"status", "summary", "kind", "task_id", "board", "profile"}:
            raise ValueError("unsupported work fields")
        with self.transaction() as conn:
            item = self._read(conn, key)
            if item is None or item["status"] not in ACTIVE:
                raise ValueError("campaign is not active")
            work = item["work"].get(work_id)
            if work is None:
                if profile != item["owner_profile"]:
                    raise ValueError("only the owner may register work")
                work = dict(id=work_id, profile=changes.get("profile", profile), status="pending", kind="request")
            if profile not in {item["owner_profile"], work["profile"]}:
                raise ValueError("work belongs to another profile")
            if profile != item["owner_profile"] and "profile" in changes:
                raise ValueError("only the owner may reassign work")
            work = {**work, **changes, "updated_at": now}
            if work["status"] not in {"pending", "running", "blocked", "done", "cancelled"}:
                raise ValueError("invalid work status")
            if work["status"] in {"done", "blocked"} and not work.get("summary", "").strip():
                raise ValueError("completed/blocked work requires a result or reason")
            if work.get("kind") == "kanban" and not work.get("task_id"):
                raise ValueError("kanban work requires task_id")
            item["work"][work_id] = work
            if work["profile"] not in item["participating_profiles"]:
                item["participating_profiles"].append(work["profile"])
            item.update(updated_at=now, last_progress_at=now, next_review_at=now, next_check_at=now,
                        revision=item["revision"] + 1)
            self._save(conn, item)
            return work

    def control(self, key, action, now):
        with self.transaction() as conn:
            return self._control(conn, key, action, now)

    def _control(self, conn, key, action, now):
        item = self._read(conn, key)
        if item is None:
            return None
        if item["status"] in TERMINAL:
            raise ValueError("completed/cancelled campaigns are closed; mention an Agent with a new task in the same topic")
        if action == "resume":
            apply_amendments(conn, item, now)
        item["control_epoch"] = item.get("control_epoch", 0) + 1
        item["status"] = {"pause": "paused", "cancel": "cancelled", "resume": "running"}[action]
        item.update(next_check_at=now, next_review_at=now, wake_pending_until=0,
                    next_report_at=now, revision=item["revision"] + 1,
                    final_report_pending=action == "cancel")
        self._save(conn, item)
        return item

    def claim_due(self, now, eligible, lease_seconds=60):
        """Only the gateway owning a saved route claims a row; another host can retry after expiry."""
        with self.transaction() as conn:
            rows = conn.execute("""SELECT data FROM campaigns WHERE lease_until<=?
                AND (next_check<=? OR next_report<=?) AND status!='paused' ORDER BY next_check""",
                (now, now, now)).fetchall()
            for row in rows:
                item = project(conn, json.loads(row[0]))
                if item["status"] not in ACTIVE and not item["final_report_pending"]:
                    continue
                if not eligible(item):
                    continue
                token = uuid.uuid4().hex
                conn.execute("UPDATE campaigns SET lease_until=?,lease_token=? WHERE key=?",
                             (now + lease_seconds, token, item["key"]))
                return item, token
        return None

    def finish_tick(self, key, token, updates, expected_revision=None, retry_after=0):
        # Merge timer observations, never overwrite a concurrent user pause or owner completion.
        with self.transaction() as conn:
            row = conn.execute("SELECT lease_token FROM campaigns WHERE key=?", (key,)).fetchone()
            if row is None or row[0] != token:
                return False
            item = self._read(conn, key)
            if expected_revision is None or item["revision"] == expected_revision:
                item.update(updates)
            self._save(conn, item)
            conn.execute("UPDATE campaigns SET lease_until=?,lease_token='' WHERE key=?", (retry_after, key))
            return True
