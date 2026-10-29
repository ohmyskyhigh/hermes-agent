"""Opt-in, chat-scoped campaign policy. No profile credentials live here."""
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class CampaignPolicy:
    state_path: Path
    groups: tuple[dict, ...]
    check_interval: int = 60
    review_interval: int = 300
    report_interval: int = 900
    stalled_after: int = 600

    @classmethod
    def parse(cls, raw):
        if not isinstance(raw, dict) or raw.get("enabled") is not True:
            return None
        path = Path(raw.get("state_path") or "")
        if not path.is_absolute():
            raise ValueError("gateway.topic_campaigns.state_path must be an absolute shared DB path")
        groups = raw.get("groups") or []
        seen = set()
        for group in groups:
            profiles = group.get("profiles") or []
            identity = (group.get("platform"), group.get("chat_id"))
            if (not all(identity) or identity in seen or not profiles
                    or group.get("default_owner") not in profiles
                    or group.get("collector_profile") not in profiles):
                raise ValueError("campaign groups need unique platform/chat, profiles, collector and default owner")
            seen.add(identity)
            if not isinstance(group.get("creation_prefix", ""), str):
                raise ValueError("campaign creation_prefix must be a string")
            for mapping in (group.get("mentions") or {}).values():
                if not isinstance(mapping, dict) or any(p not in profiles for p in mapping.values()):
                    raise ValueError("campaign mention mappings must target a participating profile")
        if not groups:
            raise ValueError("enabled topic campaigns need at least one group")
        intervals = [int(raw.get(key, default)) for key, default in (
            ("check_interval_seconds", 60), ("review_interval_seconds", 300),
            ("report_interval_seconds", 900), ("stalled_after_seconds", 600))]
        if any(value < 1 for value in intervals):
            raise ValueError("campaign intervals must be positive")
        return cls(path, tuple(groups), *intervals)

    def group_for(self, source):
        if not source.thread_id or source.chat_type == "dm":
            return None
        return next((g for g in self.groups
                     if g["platform"] == source.platform.value and g["chat_id"] == source.chat_id), None)


def topic_key(source):
    return f"{source.platform.value}:{source.chat_id}:{source.thread_id}"
