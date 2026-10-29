"""Topic Campaign orchestration over existing gateway turns and delivery adapters.

Inactive by default. The clock runs independently of completion events and of model output.
Every restored route carries its own transport owner; a missing bot never borrows another bot.
"""
import asyncio
from contextlib import closing
import logging
import json
import time

from gateway.topic_campaign_config import CampaignPolicy, topic_key
from gateway.topic_campaign_prompt import instructions, report, reply_anchor
from gateway.topic_campaign_store import ACTIVE, WORK_DONE, CampaignStore

logger = logging.getLogger(__name__)
_CONTROLS = {"暂停campaign": "pause", "取消campaign": "cancel", "继续campaign": "resume"}


def task_text(event):
    """Feishu prepends a display-only native-mention hint before the user's actual body."""
    if event.native_task_text is not None:
        return event.native_task_text
    text = event.text or ""
    if event.mentioned_user_ids and text.startswith("[Mentioned: "):
        _, separator, body = text.partition("]\n\n")
        if separator:
            return body
    return text


def service_for(runner):
    if "_topic_campaign_service" not in runner.__dict__:
        policy = CampaignPolicy.parse(getattr(runner.config, "topic_campaigns", {}))
        runner._topic_campaign_service = TopicCampaignService(runner, policy) if policy else None
    return runner._topic_campaign_service


def defer_campaign_processing_marker(runner, event):
    """Collection alone is not an agent turn; show activity only after admission."""
    service = service_for(runner) if runner is not None else None
    return bool(service and service.policy.group_for(event.source))


class TopicCampaignService:
    def __init__(self, runner, policy):
        self.runner, self.policy = runner, policy
        self.store = None

    def _store(self):
        if self.store is None:
            self.store = CampaignStore(self.policy.state_path)
        return self.store

    async def db(self, method, *args, **kwargs):
        return await asyncio.to_thread(lambda: getattr(self._store(), method)(*args, **kwargs))

    async def intake(self, event, source):
        group = self.policy.group_for(source)
        if group is None or event.internal:
            return False, None
        from gateway.session_identity import identity_of
        identity = identity_of(source)
        profile = identity.runtime_profile if identity else source.profile or self.runner._active_profile_name()
        if profile not in group["profiles"]:
            return False, None
        key, now = topic_key(source), time.time()
        body = task_text(event)
        control = _CONTROLS.get("".join(body.lower().split()))
        if event.get_command() in {"stop", "new", "reset"}:
            control = "pause"
        if control and not source.is_bot:
            notice = await self.control(key, control, now, event.message_id or source.message_id or "",
                                        actor=source.user_id or source.user_id_alt or "", profile=profile, text=body)
            if not event.get_command():
                return True, notice
        if event.get_command():
            return False, None
        mentions = (group.get("mentions") or {}).get(profile, {})
        targets = list(dict.fromkeys(mentions[i] for i in (event.mentioned_user_ids or []) if i in mentions))
        if not targets and event.native_mention_self:
            targets = [profile]
        prefix = group.get("creation_prefix", "")
        if prefix and not body.lstrip().startswith(prefix) and not await self.db("current", key):
            return profile == group["collector_profile"] and profile not in targets, None
        native_targets = list(targets)
        unresolved = bool((event.mentioned_user_ids or event.native_mentions_present) and not native_targets)
        owner = targets[0] if targets else group["default_owner"]

        item = await self.db("ingest_topic", topic_key=key,
            unresolved=unresolved,
            native_task_text=event.native_task_text, native_targets=native_targets,
            parent_message_id=event.reply_to_message_id,
            unmapped_mentions=bool(event.mentioned_user_ids and not native_targets),
            default_owner=group["default_owner"], owner=owner, profile=profile, source=source.to_dict(),
            transport_profile=identity.transport_profile if identity else profile,
            message_id=event.message_id or source.message_id or "", text=body,
            root_message_id=event.topic_root_message_id, is_bot=source.is_bot, targets=targets, now=now,
            review_interval=self.policy.review_interval, report_interval=self.policy.report_interval)
        if unresolved:
            notice = ("Campaign 原生 @收件人尚未解析；不会转交默认负责人。请核对原生 @目标或提及映射。"
                      if item is None and profile == group["collector_profile"] else None)
            return True, notice
        if item is None:
            return True, None
        from gateway.topic_campaign_dialogue import forward_dialogue, input_intent, turn_mode
        intent = await input_intent(self, item, event)
        if intent and intent['mode'] == 'paused_dialogue' and intent['delivery'] == 'forward':
            return True, await forward_dialogue(self, item, event, source, intent)
        if await turn_mode(self, item, event, source) is None:
            return True, None
        recipient = profile in targets if targets else profile == item["owner_profile"]
        if not recipient:
            return True, None
        return False, None

    async def control(self, key, action, now, message_id, *, actor="", profile="", text=""):
        try:
            item = await self.db("control_topic", key, message_id, action, now,
                                 actor=actor, profile=profile, text=text)
        except ValueError:
            return "该 Campaign 已关闭；在本话题原生 @Agent 并附新任务正文可启动下一阶段。"
        if item is None:
            return "本话题尚未建立 Campaign。"
        if item["status"] in {"completed", "cancelled"} and not item.get("_control_applied", False):
            return "该 Campaign 已关闭；在本话题原生 @Agent 并附新任务正文可启动下一阶段。"
        if not item.get("_control_applied", True):
            return f"此控制消息已处理，不重复执行；原阶段当前状态：{item['status']}。"
        return {"pause": "Campaign 已暂停自动监督和后续续派；已经运行的工作不会被强制终止。",
                "cancel": "Campaign 已取消自动监督；已经运行的工作不会被强制终止。",
                "resume": "Campaign 已恢复；暂停期间已登记的要求修改已应用，负责人将按当前要求继续检查和推进。"}[action]

    def route(self, item, profile=None):
        from gateway.session import SessionSource
        from gateway.session_identity import restore_identity
        route = item["routes"].get(profile or item["owner_profile"])
        if not route:
            return None, None
        primary = getattr(self.runner, "_primary_profile_name", None) or self.runner._active_profile_name()
        if not self.runner.config.multiplex_profiles and route["transport_profile"] != primary:
            return None, None
        source = SessionSource.from_dict(route["source"])
        restore_identity(source, runner=self.runner, transport_profile=route["transport_profile"])
        adapter = self.runner._delivery_adapter_for(source)
        return source, adapter

    def eligible(self, item):
        _, adapter = self.route(item)
        if adapter is not None:
            return True
        if item["owner_profile"] in item["routes"]:
            return False  # Another gateway owns that route; do not consume its review deadline.
        # The collector can report that the owner has not received the task, but cannot wake as it.
        for group in self.policy.groups:
            if item["key"].startswith(group["platform"] + ":" + group["chat_id"] + ":"):
                return self.route(item, group["collector_profile"])[1] is not None
        return False

    async def health(self, item, now, source):
        key = self.runner._session_key_for_source(source) if source else ""
        busy = bool(key and self.runner._is_session_running(key))
        workers = await asyncio.to_thread(self._worker_health, item, now)
        return dict(owner_busy=busy, stalled=now - item["last_progress_at"] >= self.policy.stalled_after,
                    workers=workers)

    def _worker_health(self, item, now):
        import sqlite3
        from pathlib import Path
        workers = []
        for work in item["work"].values():
            observed = dict(id=work["id"], profile=work["profile"], status=work["status"],
                            stale=work["status"] not in WORK_DONE and now - work["updated_at"] >= self.policy.stalled_after)
            if work.get("kind") == "kanban":
                # Resolve the shared Kanban file from the explicit campaign policy, never another
                # profile's ambient environment. Default board lives alongside the campaign DB.
                board = work.get("board", "default")
                if not board.replace("-", "").replace("_", "").isalnum():
                    observed["error"] = "invalid board name"
                else:
                    base = self.policy.state_path.parent
                    path = base / "kanban.db" if board == "default" else base / "kanban" / "boards" / board / "kanban.db"
                    try:
                        with closing(sqlite3.connect(Path(path).as_uri() + "?mode=ro", uri=True, timeout=2)) as conn:
                            row = conn.execute("SELECT status,last_heartbeat_at,result FROM tasks WHERE id=?",
                                               (work["task_id"],)).fetchone()
                        if row:
                            observed.update(status=row[0], last_heartbeat_at=row[1], result=row[2],
                                            stale=row[0] not in {"done", "completed", "cancelled", "failed"}
                                            and (not row[1] or now - row[1] >= self.policy.stalled_after))
                        else:
                            observed["error"] = "task not found"
                    except sqlite3.Error as exc:
                        observed["error"] = str(exc)
            workers.append(observed)
        return workers

    async def tick(self, now=None):
        now = time.time() if now is None else now
        # Eligibility touches adapter/profile ownership and must run on the gateway loop.
        rows = await self.db("list")
        eligible = {item["key"] for item in rows if self.eligible(item)}
        claimed = await self.db("claim_due", now, lambda item: item["key"] in eligible)
        if claimed is None:
            return False
        item, token = claimed
        updates = {"next_check_at": now + self.policy.check_interval, "last_error": ""}
        try:
            await self._tick_claimed(item, updates, now)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # health: allow BLE001 -- durable scheduler records and retries failed delivery
            updates["last_error"] = str(exc)[:500]
            logger.warning("Campaign supervision failed for %s: %s", item["key"], exc, exc_info=True)
        finally:
            retry_after = now + self.policy.check_interval if updates.get("last_error") else 0
            await self.db("finish_tick", item["key"], token, updates, item["revision"], retry_after)
        return True

    async def _tick_claimed(self, item, updates, now):
        from gateway.run import _async_profile_runtime_scope
        source, adapter = self.route(item)
        health = await self.health(item, now, source)
        current = await self.db("get", item["key"])
        if current["status"] == "paused":
            return
        item = current
        observations = health["workers"]
        if observations != item.get("worker_observations"):
            updates["worker_observations"] = observations
            item = {**item, "next_review_at": min(item["next_review_at"], now)}
        if item["status"] in ACTIVE and now >= item["next_review_at"] and now >= item["wake_pending_until"]:
            if source is None or adapter is None:
                updates["last_error"] = "负责人尚无可用话题路由，等待其接单或恢复连接"
            elif not health["owner_busy"] and not getattr(self.runner, "_external_drain_active", False):
                async with _async_profile_runtime_scope(self.runner._resolve_profile_home_for_source(source)):
                    from agent.estop import check_paused
                    if not check_paused("campaign", logger):
                        try:
                            await self._wake(item, source, adapter, health)
                            updates.update(wake_pending_until=now + self.policy.review_interval, last_error="")
                        except Exception as exc:  # health: allow BLE001 -- a refused wake must not suppress the scheduled report
                            updates["last_error"] = str(exc)[:500]
                            logger.warning("Campaign wake failed for %s: %s", item["key"], exc)
        if now >= item["next_report_at"] or item["final_report_pending"]:
            if adapter is None:
                group = self.policy.group_for(source) if source else next(g for g in self.policy.groups
                    if item["key"].startswith(g["platform"] + ":" + g["chat_id"] + ":"))
                source, adapter = self.route(item, group["collector_profile"])
            if source is None or adapter is None:
                raise RuntimeError("no authorized campaign report route")
            async with _async_profile_runtime_scope(self.runner._resolve_profile_home_for_source(source)):
                result = await adapter.send(source.chat_id, report({**item, **updates}, health, now),
                                            reply_to=reply_anchor(item),
                                            metadata={"thread_id": source.thread_id})
            if not result or getattr(result, "success", False) is not True:
                raise RuntimeError(f"campaign report delivery failed: {getattr(result, 'error', 'no receipt')}")
            updates.update(last_reported_at=now, next_report_at=now + self.policy.report_interval,
                           final_report_pending=False)

    async def history_summary(self, item):
        rows = await self.db("history", item["topic_key"])
        previous = [row for row in rows if row["milestone_no"] < item["milestone_no"]][-3:]
        summary = []
        for row in reversed(previous):
            entry = {k: row[k] for k in ("key", "milestone_no", "owner_profile", "status")}
            entry.update(goal=row["goal"][:150], evidence=row["evidence"][:200])
            candidate = [entry, *summary]
            if len(json.dumps(candidate, ensure_ascii=False)) <= 2000:
                summary = candidate
        return summary

    async def _wake(self, item, source, adapter, health):
        from gateway.wake import deliver_wake
        text = instructions(item, item["owner_profile"], self.policy.state_path,
                            await self.db("events", item["key"]), health=health, history=await self.history_summary(item))
        await deliver_wake(adapter, text=text, source=source, topic_campaign_key=item["key"],
                           topic_campaign_epoch=item.get('control_epoch', 0))


async def pending_reply_or_campaign(runner, event, source, key, is_internal):
    """Preserve approval/clarification replies before adding campaign turn context."""
    # An exact lifecycle command is never a clarification answer or an execution approval.
    if not is_internal and not source.is_bot and ''.join(task_text(event).lower().split()) in _CONTROLS:
        service = service_for(runner)
        if service and service.policy.group_for(source):
            return await observe_campaign_message(runner, event, source)
    reply = None if is_internal else await runner._hm_pending_reply_intercepts(event, source, key)
    if reply is not None:
        return True, reply
    return await observe_campaign_message(runner, event, source)


async def observe_campaign_message(runner, event, source):
    from gateway.session_ingress import executing_admission
    service = service_for(runner)
    if service is None:
        return False, None
    if executing_admission.get() or event.internal:
        if service.policy.group_for(source):
            from gateway.topic_campaign_binding import resolve_campaign
            from gateway.topic_campaign_dialogue import turn_mode
            item, scoped = await resolve_campaign(service, event, source)
            if scoped and await turn_mode(service, item, event, source) is None:
                return True, None
        return False, None
    return await service.intake(event, source)


async def prepare_campaign_turn(runner, event, source):
    """Enrich only after admission, keeping redelivery fingerprints and queued input immutable."""
    service = service_for(runner)
    if service is None or not service.policy.group_for(source):
        return
    from gateway.topic_campaign_binding import resolve_campaign
    from gateway.topic_campaign_dialogue import dialogue_prompt, input_intent, tool_handler, turn_mode
    item, scoped = await resolve_campaign(service, event, source)
    mode = await turn_mode(service, item, event, source) if scoped else None
    if scoped and mode is None:
        return False
    if item is None:
        return True
    if item:
        event.reply_anchor_override = reply_anchor(item)
    from gateway.session_identity import identity_of
    identity = identity_of(source)
    profile = identity.runtime_profile if identity else source.profile or runner._active_profile_name()
    if profile not in service.policy.group_for(source)["profiles"]:
        return
    if mode == 'paused_dialogue':
        event.text = await dialogue_prompt(service, item, event, source, profile)
        event._campaign_dialogue_handler = tool_handler(service._store(), item, profile,
            await input_intent(service, item, event), service.policy.state_path)
    else:
        incoming = "" if event.internal else event.text
        event.text = instructions(item, profile, service.policy.state_path,
                                  await service.db("events", item["key"]), incoming=incoming,
                                  health=await service.health(item, time.time(), source),
                                  history=await service.history_summary(item))
    adapter = runner._intake_adapter_for(source)
    if adapter is not None:
        event._campaign_turn_admitted = True
        await adapter.on_processing_start(event)
    return True


async def execute_campaign_turn(runner, event, source, key, generation):
    """Revalidate immediately before model execution, inside the claimed session slot."""
    if await prepare_campaign_turn(runner, event, source) is False:
        return None
    from agent.tool_execution_scope import constrained_tools
    with constrained_tools(getattr(event, '_campaign_dialogue_handler', None)):
        return await runner._handle_message_with_agent(event, source, key, generation)


async def campaign_watcher(runner):
    service = service_for(runner)
    if service is None:
        return
    while runner._running:
        # Bounded burst lets all due topics proceed without one busy topic monopolizing the tick.
        for _ in range(50):
            if not await service.tick():
                break
        await asyncio.sleep(min(service.policy.check_interval, 15))
