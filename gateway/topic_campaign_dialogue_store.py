"""Durable input intent and human-sourced requirement amendments while paused."""
import json


def migrate_dialogue(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS campaign_inputs (
        campaign_key TEXT NOT NULL, message_id TEXT NOT NULL, mode TEXT NOT NULL,
        control_epoch INTEGER NOT NULL, targets TEXT NOT NULL, delivery TEXT NOT NULL,
        actor TEXT NOT NULL, text TEXT NOT NULL, PRIMARY KEY(campaign_key,message_id))''')
    conn.execute('''CREATE TABLE IF NOT EXISTS campaign_amendments (
        id INTEGER PRIMARY KEY, campaign_key TEXT NOT NULL, message_id TEXT NOT NULL,
        profile TEXT NOT NULL, control_epoch INTEGER NOT NULL, changes TEXT NOT NULL,
        created_at REAL NOT NULL, applied_at REAL, previous TEXT,
        UNIQUE(campaign_key,message_id,profile))''')


def remember_input(conn, item, message_id, *, is_bot, targets, profile, source, text, new):
    # Legacy messages and duplicate inputs must never gain a new execution mode.
    if not new:
        return
    mode = 'bot' if is_bot else 'paused_dialogue' if item['status'] == 'paused' else 'task'
    conn.execute('INSERT OR IGNORE INTO campaign_inputs VALUES (?,?,?,?,?,?,?,?)',
                 (item['key'], message_id, mode, item.get('control_epoch', 0),
                  json.dumps(targets or [item['owner_profile']]),
                  'forward' if not targets and profile != item['owner_profile'] else 'native',
                  source.get('user_id', ''), text))


def apply_amendments(conn, item, now):
    rows = conn.execute('SELECT a.* FROM campaign_amendments a JOIN campaign_message_bindings b '
                        'ON a.campaign_key=b.campaign_key AND a.message_id=b.message_id '
                        'WHERE a.campaign_key=? AND a.applied_at IS NULL ORDER BY b.first_received_at,a.id',
                        (item['key'],)).fetchall()
    for row in rows:
        changes = json.loads(row['changes'])
        previous = {key: item[key] for key in changes}
        item.update(changes)
        conn.execute('UPDATE campaign_amendments SET applied_at=?,previous=? WHERE id=?',
                     (now, json.dumps(previous, ensure_ascii=False), row['id']))


class DialogueStore:
    def input_intent(self, key, message_id):
        with self.connection() as conn:
            row = conn.execute('SELECT * FROM campaign_inputs WHERE campaign_key=? AND message_id=?',
                               (key, message_id)).fetchone()
            return {**dict(row), 'targets': json.loads(row['targets'])} if row else None

    def amendments(self, key):
        with self.connection() as conn:
            return [{**dict(row), 'changes': json.loads(row['changes']),
                     'previous': json.loads(row['previous']) if row['previous'] else None}
                    for row in conn.execute('SELECT * FROM campaign_amendments WHERE campaign_key=? ORDER BY id',
                                            (key,))]

    def amend(self, key, profile, message_id, changes, now):
        if not changes or set(changes) - {'goal', 'done_when', 'next_action'}:
            raise ValueError('only goal, done_when and next_action may be amended')
        if any(not isinstance(v, str) or not v.strip() or len(v) > 12000 for v in changes.values()):
            raise ValueError('requirement changes must be nonempty text up to 12000 characters')
        with self.transaction() as conn:
            item = self._read(conn, key)
            origin = conn.execute('SELECT * FROM campaign_inputs WHERE campaign_key=? AND message_id=?',
                                  (key, message_id)).fetchone()
            if (not item or item['status'] != 'paused' or not origin or origin['mode'] != 'paused_dialogue'
                    or origin['control_epoch'] != item.get('control_epoch', 0)
                    or profile not in json.loads(origin['targets'])):
                raise ValueError('amendment requires this paused epoch and its addressed human message')
            payload = json.dumps(changes, ensure_ascii=False, sort_keys=True)
            prior = conn.execute('SELECT changes FROM campaign_amendments '
                                 'WHERE campaign_key=? AND message_id=? AND profile=?',
                                 (key, message_id, profile)).fetchone()
            if prior and prior['changes'] != payload:
                raise ValueError('this message already recorded a different amendment; ask for a new instruction')
            conn.execute('''INSERT OR IGNORE INTO campaign_amendments
                (campaign_key,message_id,profile,control_epoch,changes,created_at) VALUES (?,?,?,?,?,?)''',
                         (key, message_id, profile, origin['control_epoch'], payload, now))
            return dict(status='paused', applied=False, changes=changes, source_message_id=message_id)
