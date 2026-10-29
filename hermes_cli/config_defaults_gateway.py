"""Pure gateway configuration defaults, imported by config_defaults."""

GATEWAY_DEFAULTS = {  # Gateway settings (messaging platforms: Telegram, Discord, Slack, ...).
    "topic_campaigns": {"enabled": False, "state_path": "", "groups": [],
                        "check_interval_seconds": 60, "review_interval_seconds": 300,
                        "report_interval_seconds": 900, "stalled_after_seconds": 600},
    "unmanaged_idle_exit_seconds": 600,  # client-auto-started gateway only; 0 disables
    # Seconds a SIGTERM-interrupted agent unwinds before teardown (short: service stop budgets).
    "signal_interrupt_grace_timeout": 1,
    "service_install_choice": None,  # null | install | decline; never authorizes ordinary launch installation.
    # Durable delivery-obligation ledger: final responses are recorded in state.db around the
    # platform send; a gateway that died between finalize and platform ACK redelivers on next
    # boot (ambiguous cases carry a "recovered reply — may be a duplicate" marker;
    # at-least-once). Disable to lose in-flight final responses on crash/restart.
    "delivery_ledger": True,
    # Seconds to wait for one platform to connect at startup/reconnect; raise on "discord
    # connect timed out" loops (many slash commands to sync). 0/negative = wait forever. Bridged
    # to HERMES_GATEWAY_PLATFORM_CONNECT_TIMEOUT, which wins if set explicitly.
    # Seconds the gateway waits for a single messaging platform to finish connecting during startup (and
    # on reconnect). Discord in particular can blow past the old fixed 30s when an account has many
    # slash commands to sync (#19776: 90-173 skills → ~28-31s sync). Raise this if your gateway hits
    # "discord connect timed out" / "Timeout waiting for connection to Discord" restart loops. ``0`` or
    # negative disables the timeout entirely (wait indefinitely).
    "platform_connect_timeout": 30,
    # Event-loop liveness watchdog: a daemon thread probes the asyncio loop; after consecutive
    # missed probes it dumps all-thread stacks and hard-exits with the service-restart code so
    # systemd/launchd revives the process instead of leaving a wedged-but-alive zombie.
    # Set to false to disable. See #69089.
    "loop_watchdog": True,
    # Watchdog tuning (defaults mirror gateway/shutdown_watchdog.py): probe_interval = seconds
    # between probes; probe_timeout = seconds before an unprocessed probe counts as a miss;
    # max_strikes = consecutive misses before hard-exit 75 (~90-120s of sustained loop block at
    # the defaults).
    "loop_watchdog_probe_interval_s": 30.0,
    "loop_watchdog_probe_timeout_s": 10.0,
    "loop_watchdog_max_strikes": 3,
    # Allow all users without allowlists (security opt-in).
    "allow_all_users": False,
    # Bot-to-bot loop guard: admitted bot messages per conversation before a cooldown.
    "bot_loop_guard": {"enabled": True, "max_events": 20, "window_seconds": 300, "cooldown_seconds": 600},
    # Startup-liveness watchdog: stdlib-only daemon thread armed at process entry that
    # hard-exits 75 if the loop isn't live within the deadline. Armed before config loads, so
    # run_gateway() bridges these to HERMES_STARTUP_WATCHDOG / HERMES_STARTUP_WATCHDOG_TIMEOUT_S
    # and re-arms the live handle; explicit env wins.
    "startup_watchdog": True,
    "startup_watchdog_timeout_seconds": 300,
    # Keep writing the legacy ~/.hermes/sessions/sessions.json mirror of the routing index
    # (primary copy: state.db gateway_routing table). True for external tooling and downgrade
    # safety; False stops producing the file.
    "write_sessions_json": True,
    # One gateway for every profile on this host: the DEFAULT profile's gateway also connects
    # each named profile's bots (their own .env / config.yaml, per-profile secret scope) and
    # stamps the profile into session keys. This is the ONLY supported topology — there is no
    # `false` opt-out any more: an explicit `false` still parses (it is the runtime mode flag
    # every scoped code path reads) but is warned about and IGNORED for process topology, and
    # `hermes gateway migrate --multiplex` folds any per-profile fleet that is left.
    # An UNSET key is a request, not a verdict: at boot the gateway runs the migration
    # preflight and stays standalone (logging why) while a secondary still runs its own
    # gateway or a blocker exists, then converges once that is resolved.
    # TWO things DO change on a host that had pinned `false`, and neither is a process:
    #   • INGRESS — `/p/<profile>/` on the default listener goes 404 -> served
    #     (gateway/api_server.py::_resolve_request_profile, gateway/webhook.py). A host that
    #     opted out GAINS that HTTP surface; it is authenticated exactly like the default
    #     profile's, but it is new reachable surface, so audit any reverse proxy that assumed
    #     /p/ was dead.
    #   • SECRET SCOPE — eager multi-profile activation no longer consults the flag
    #     (tui_gateway/launch_profile_policy.py), so a host with a leftover servable profile
    #     dir flips eager=false/reads-open -> eager=true/fail-closed: an UNSCOPED `get_secret`
    #     now raises UnscopedSecretError, and a key that lives ONLY in the unit's
    #     `Environment=` (no .env) disappears from file-built scopes. A genuinely
    #     single-profile host never activates and is byte-identical.
    # Two profiles configuring the same bot token cannot be served together — the duplicate
    # adapter is parked; `hermes profile create --clone` therefore leaves messaging channels
    # behind unless --clone-channels is passed.
    "multiplex_profiles": True,
    # May `hermes update` fold this install onto a multiplexed default gateway by itself?
    # True (the default) keeps today's behaviour: a multi-profile install whose secondaries run
    # their own gateways is migrated automatically after an update when nothing blocks it.
    # Set to False to choose WHEN you converge, not whether: the fold is left to you to run by
    # hand (it is not an opt-out from the one-gateway-per-host model, which has none). Only the
    # AUTOMATIC path reads this: `hermes gateway migrate --multiplex` is explicit and proceeds.
    "auto_multiplex_migration": True,
    # Route inbound chats of the default profile's bots to another profile
    # (gateway/profile_routing.py): [{profile, platform, chat_id|user_id|guild_id|...}].
    # Most-specific match wins; only read by the multiplexing default gateway.
    "profile_routes": [],
    # Scale-to-zero idle TIMEOUT only. When an instance is opted in via the NAS "Labs" toggle
    # (HERMES_SCALE_TO_ZERO env stamp) AND messaging is relay-only/absent AND a wakeUrl is
    # registered, the relay transport goes dormant so the platform (e.g. Fly autostop) can
    # suspend the machine; it wakes on the wakeUrl poke. Enablement is the Labs toggle, never a
    # config key. 0/negative = default.
    "scale_to_zero": {"idle_timeout_minutes": 2},
    # Auto-resume restart-loop breaker. A supervisor-revived gateway auto-resumes the
    # SIGTERM-interrupted session; if that turn keeps triggering the kill, boots no more than
    # `max_gap_seconds` apart (floored by `window_seconds`) chain, and after `max_restarts`
    # auto-resume is SKIPPED for that boot (inbound messages still served). Gap-based chaining
    # also catches SLOW ~150s crash cycles. max_restarts=0 disables.
    "restart_loop_guard": {"max_restarts": 3, "window_seconds": 60, "max_gap_seconds": 300},
    # Respawn-storm circuit breaker (complements restart_loop_guard): counts (re)starts in a
    # sliding window and sleeps an exponential backoff before booting so a crash-looping
    # supervisor can't hammer the process. max_starts <= 0 disables. Env escape hatches:
    # HERMES_GATEWAY_MAX_STARTS / HERMES_GATEWAY_START_WINDOW_S.
    "respawn_storm": {"max_starts": 5, "window_seconds": 120},
    # Prefix user messages IN THE MODEL'S CONTEXT with a timestamp (e.g. "[Tue 2026-04-28
    # 13:40:53 CEST]") for temporal awareness. Persisted transcripts stay clean (timestamp is
    # message metadata regardless), so enabling later surfaces past send-times too.
    "message_timestamps": {"enabled": False},
    # Max bytes of inbound image/audio/video the gateway buffers into RAM and caches to disk.
    # Media is read fully into memory first, so unbounded uploads (Discord Nitro: 500 MB) or
    # huge remote URLs can OOM-kill constrained deployments. Enforced in
    # gateway/platforms/base.py for every adapter. 0 = no cap. Default 128 MiB.
    "max_inbound_media_bytes": 134217728,
    # Let adapters read HTTP_PROXY/HTTPS_PROXY/NO_PROXY/SSL_CERT_FILE from the environment and
    # auto-detect generic/macOS system proxies. False when the gateway inherits a proxy it must
    # not use (e.g. a scheduled task picking up a Clash/V2Ray HTTP_PROXY -> "Cannot connect to
    # host 127.0.0.1:7890"). Per-platform vars (DISCORD_PROXY, TELEGRAM_PROXY, ...) are still
    # honored.
    "trust_env": True,
    # Media delivery. False: any emitted file path is delivered natively unless under the
    # credential/system denylist (/etc, /proc, ~/.ssh, ~/.aws, ~/.hermes/.env, auth.json). True:
    # files must be under the Hermes cache, media_delivery_allow_dirs, or fresher than
    # trust_recent_files_seconds — recommended for public-facing gateways so prompt injection
    # can't exfiltrate host secrets. Bridged to HERMES_MEDIA_DELIVERY_STRICT.
    "strict": False,
    # Extra roots (project/scratch dirs, mounted shares) from which bare file paths may be
    # uploaded; the Hermes cache is always trusted. List of absolute paths or one
    # os.pathsep-separated string; tildes expanded. Bridged to HERMES_MEDIA_ALLOW_DIRS. Honored
    # in both modes.
    "media_delivery_allow_dirs": [],
    # Trust files whose mtime is within trust_recent_files_seconds even outside the cache/
    # allowlist (e.g. `pandoc -o /tmp/report.pdf`); system paths stay blocked. False =
    # pure-allowlist mode. Bridged to HERMES_MEDIA_TRUST_RECENT_FILES. Only consulted when
    # strict is true.
    "trust_recent_files": True,
    # Recency window in seconds; 600 covers a multi-tool turn. Bridged to
    # HERMES_MEDIA_TRUST_RECENT_SECONDS. Only consulted when strict is true.
    "trust_recent_files_seconds": 600,
    "api_server": {  # OpenAI-compatible API server platform (gateway/platforms/api_server.py).
        # Max concurrent agent runs. Requests to /v1/chat/completions, /v1/responses, and
        # /v1/runs beyond this get HTTP 429 + Retry-After, bounding CPU/memory/LLM-quota
        # exhaustion from a request flood. 0 = no cap.
        "max_concurrent_runs": 10,
        # Cap (chars) on each tool output and tool-call argument string in the stored
        # /v1/responses conversation history used for previous_response_id chaining. The
        # stored history is cumulative, so a few large tool outputs can make one
        # response_store.db write several hundred KB. 0 = store tool outputs verbatim
        # (default: the capped text is what the model is replayed on the next turn).
        "history_tool_output_max_chars": 0,
    },
}
