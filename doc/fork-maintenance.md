# Maintaining this customized fork

`ohmyskyhigh/hermes-agent:main` is the tested release branch for our installations.
There is no permanent `custom/main` branch. Official commits enter this branch only
after integration, conflict resolution and regression testing in a development checkout.

## Development and release

Use `origin` for this fork and `upstream` for `NousResearch/hermes-agent` in the
development checkout. Start a temporary integration branch from `origin/main`,
fetch the official `main`, and merge it. Preserve the customization ancestry.
Resolve moved behavior in the current upstream modules instead of restoring old
monolithic files. Open a PR targeting this fork's `main`; merge only the verified
tree, using a merge commit. Never force-push the release branch.

The initial customization families are image patch-limit recovery, MCP OAuth
authorization-server discovery and issuer binding, and Feishu conversational
reply policy/native handoffs. Final reply policy runs in
`gateway/run_turn_persistence.py` after persistence, before voice/media delivery.
The generic `gateway_reply_policy` hook remains optional; external plugins own
classification. Frozen compatibility fixtures are tests, not installed plugins.

## Runtime configuration

Runtime checkouts have only `origin`, set to
`https://github.com/ohmyskyhigh/hermes-agent.git`, and track `main` -> `origin/main`.
Do not add an official `upstream` remote to a runtime checkout: the stock updater
can fetch it even when the skip-prompt marker exists.

Select `hermes update --set-channel main` for the installation. Create
`.skip_upstream_prompt` in each default/named Hermes home from which updates can
run. This marker suppresses adding upstream; it does not disable an existing one.
An explicit `--branch main` also selects the fork branch. Do not select official
release channels for this customized deployment.

## Update procedure

1. Take a restorable Git bundle and preserve launchers, Git configuration, plugin
   sources and all profile configurations. Snapshot live SQLite databases with
   the SQLite backup API, not by copying only their main files. Verify integrity
   and archive checksums. Keep credentials and backups outside this repository.
2. Check `git remote -v`, then fetch only `origin`'s `main`. Stop on fetch failure;
   never substitute official upstream or an official release archive.
3. Require an empty `git status --porcelain` and a successful
   `git merge-base --is-ancestor HEAD origin/main`. Stop if dirty, diverged or
   unverifiable. The general Hermes updater can autostash or rescue/reset a
   divergence, so this operator preflight is required; it is not an automatic
   refusal promised by the stock updater.
4. Run `hermes update --plan`, then `hermes update --branch main --yes` to use
   the supported dependency, pause/drain and restart lifecycle. Do not replace
   that lifecycle with a bare Git pull. `--no-backup` is appropriate only when
   the independently verified backup for this update is already complete.
5. Verify the clean checkout, branch/upstream and HEAD against the published
   fork commit. Check `hermes update --check --branch main`, `hermes --version`,
   diagnostics and each previously running service's actual code SHA.

If a failure occurs before local activation, keep the old runtime. For a failed
activation, preserve any new state, stop the affected processes through Hermes,
then restore the matching code/runtime/launcher and compatible data. Never copy
an old state snapshot over sessions created after the update without preserving them.

## Validation

Use `scripts/run_tests.sh` with a PM-built test environment. Cover image recovery,
MCP OAuth discovery/issuer binding, reply policy, busy/queued routing, native
Feishu mentions, proxy/TTS, plugin loading across homes, and origin-only update
selection. Run `python scripts/check`. Test installed external-plugin copies in
temporary homes without live message sends. Compare unrelated failures against
the untouched upstream baseline and record them separately from regressions.
