"""Atomic milestone indexing, legacy migration and immutable message ownership."""
import json

PROJECTION = ('topic_key', 'milestone_no', 'previous_campaign_key', 'trigger_message_id')


def project(conn, item):
    row = conn.execute('SELECT topic_key,milestone_no,previous_key,trigger_message_id '
                       'FROM campaign_milestones WHERE campaign_key=?', (item['key'],)).fetchone()
    return {**item, **dict(zip(PROJECTION, row))} if row else item


def migrate(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS campaign_milestones (
        campaign_key TEXT PRIMARY KEY REFERENCES campaigns(key), topic_key TEXT NOT NULL,
        milestone_no INTEGER NOT NULL CHECK(milestone_no>=1), previous_key TEXT,
        trigger_message_id TEXT, UNIQUE(topic_key,milestone_no))''')
    conn.execute('''CREATE TABLE IF NOT EXISTS campaign_message_bindings (
        topic_key TEXT NOT NULL, message_id TEXT NOT NULL, campaign_key TEXT,
        disposition TEXT NOT NULL, control_action TEXT, first_received_at REAL NOT NULL,
        PRIMARY KEY(topic_key,message_id))''')
    rows = conn.execute('SELECT key,data FROM campaigns WHERE key NOT IN '
                        '(SELECT campaign_key FROM campaign_milestones)').fetchall()
    for key, data in rows:
        item = json.loads(data)
        routes = item.get('routes', {}).values()
        expected = {f"{r['source']['platform']}:{r['source']['chat_id']}:{r['source'].get('thread_id')}"
                    for r in routes}
        if expected != {key}:
            raise ValueError(f'legacy campaign route does not match key: {key}')
        conn.execute('INSERT INTO campaign_milestones VALUES (?,?,?,?,?)', (key, key, 1, None, None))
    conn.execute('''INSERT OR IGNORE INTO campaign_message_bindings
        SELECT m.topic_key,substr(e.dedup,9),e.campaign,'bound',NULL,e.at
        FROM campaign_events e JOIN campaign_milestones m ON m.campaign_key=e.campaign
        WHERE e.dedup LIKE 'message:%' AND length(e.dedup)>8''')


def _current(store, conn, topic):
    row = conn.execute('SELECT campaign_key FROM campaign_milestones WHERE topic_key=? '
                       'ORDER BY milestone_no DESC LIMIT 1', (topic,)).fetchone()
    return store._read(conn, row[0]) if row else None


def _binding(conn, topic, message):
    row = conn.execute('SELECT * FROM campaign_message_bindings WHERE topic_key=? AND message_id=?',
                       (topic, message)).fetchone()
    return dict(row) if row else None


def _remember(conn, topic, message, key, disposition, now, action=None):
    conn.execute('''INSERT INTO campaign_message_bindings VALUES (?,?,?,?,?,?)
        ON CONFLICT(topic_key,message_id) DO UPDATE SET campaign_key=excluded.campaign_key,
        disposition=excluded.disposition,control_action=excluded.control_action''',
                 (topic, message, key, disposition, action, now))


class MilestoneStore:
    def current(self, topic_key):
        with self.connection() as conn:
            return _current(self, conn, topic_key)

    def history(self, topic_key):
        with self.connection() as conn:
            keys = conn.execute('SELECT campaign_key FROM campaign_milestones WHERE topic_key=? '
                                'ORDER BY milestone_no', (topic_key,)).fetchall()
            return [self._read(conn, key[0]) for key in keys]

    def binding(self, topic_key, message_id):
        with self.connection() as conn:
            return _binding(conn, topic_key, message_id)

    def ingest_topic(self, *, topic_key, native_task_text=None, native_targets=None,
                     unresolved=False, allow_initial=True, parent_message_id=None,
                     unmapped_mentions=False, default_owner=None, **values):
        from gateway.topic_campaign_store import TERMINAL
        message = values['message_id']
        if not isinstance(message, str) or not message.strip():
            raise ValueError('campaign intake requires a stable native message ID')
        with self.transaction() as conn:
            binding = _binding(conn, topic_key, message)
            if binding and binding['disposition'] != 'unresolved':
                if binding['disposition'] != 'bound':
                    return None
                item = self._read(conn, binding['campaign_key'])
                if item['status'] in TERMINAL:
                    return item
                return self._ingest(conn, key=item['key'], **values)
            item = _current(self, conn, topic_key)
            if unmapped_mentions:
                values['targets'] = []
                if item is None:
                    values['owner'] = default_owner
                elif item['status'] in TERMINAL:
                    unresolved = True
            pinned = self._read(conn, binding['campaign_key']) if binding and binding['campaign_key'] else None
            if unresolved:
                initial_key = pinned['key'] if pinned else item['key'] if item and item['status'] not in TERMINAL else None
                _remember(conn, topic_key, message, initial_key, 'unresolved', values['now'])
                return None
            if pinned:
                _remember(conn, topic_key, message, pinned['key'], 'bound', values['now'])
                return self._ingest(conn, key=pinned['key'], **values)
            creating = item is None or item['status'] in TERMINAL
            if creating:
                qualified = (not values['is_bot'] and (allow_initial if item is None else
                             bool(native_targets and isinstance(native_task_text, str) and native_task_text.strip())))
                if not qualified:
                    _remember(conn, topic_key, message, None, 'ignored', values['now'])
                    return None
                number = item['milestone_no'] + 1 if item else 1
                key = f'{topic_key}:milestone:{number}' if number > 1 else topic_key
                if item:
                    values['root_message_id'] = item['root_message_id']
                    values['owner'] = native_targets[0]
                    values['text'] = native_task_text.strip()
                previous = item['key'] if item else None
                # Index first within the same transaction; no connection sees a partial stage.
                conn.execute('INSERT INTO campaign_milestones VALUES (?,?,?,?,?)',
                             (key, topic_key, number, previous, message))
            else:
                key = item['key']
            # Unassociated bot output must not acquire a later milestone's ownership.
            parent = _binding(conn, topic_key, parent_message_id) if parent_message_id else None
            if (values['is_bot'] and item and item['milestone_no'] > 1
                    and not (parent and parent['disposition'] == 'bound' and parent['campaign_key'] == key)):
                _remember(conn, topic_key, message, None, 'ignored', values['now'])
                return None
            _remember(conn, topic_key, message, key, 'bound', values['now'])
            return self._ingest(conn, key=key, **values)

    def control_topic(self, topic_key, message_id, action, now, *, actor='', profile='', text=''):
        if not message_id:
            raise ValueError('campaign control requires a stable native message ID')
        with self.transaction() as conn:
            binding = _binding(conn, topic_key, message_id)
            if binding and binding['disposition'] != 'unresolved':
                item = self._read(conn, binding['campaign_key']) if binding['campaign_key'] else None
                return {**item, '_control_applied': False} if item else None
            item = _current(self, conn, topic_key)
            _remember(conn, topic_key, message_id, item['key'] if item else None, 'control', now, action)
            if item and item['status'] in {'completed', 'cancelled'}:
                return {**item, '_control_applied': False}
            updated = self._control(conn, item['key'], action, now) if item else None
            if updated:
                receipt = dict(action=action, message_id=message_id, actor=actor, profile=profile,
                               text=text, at=now, control_epoch=updated['control_epoch'])
                updated['last_control'] = receipt
                self._save(conn, updated)
                conn.execute('INSERT INTO campaign_events(campaign,dedup,kind,profile,text,at) VALUES (?,?,?,?,?,?)',
                             (updated['key'], 'control:' + message_id, 'human_control', profile,
                              json.dumps(receipt, ensure_ascii=False), now))
            return {**updated, '_control_applied': True} if updated else None
