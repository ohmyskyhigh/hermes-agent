# Fork validation before merging local customizations

The `fork-validation.yml` workflow validates a fixed pull-request head on standard
GitHub-hosted Linux, Windows and macOS runners. It does not deploy Hermes, merge upstream,
publish releases, sign artifacts, or load production credentials.

The required test selection is `scripts/ci/fork_required_tests.json`. Its initial
PR4 policy is also saved outside the candidate checkout under the G-drive
maintenance candidate as `merge-policy-pr4.json`. The maintainer must compare that
external manifest hash and the workflow before accepting evidence; a candidate's
own modified checklist is not approval to reduce coverage.

The regression selection includes the local customization contracts, update and
channel policy tests, configuration migrations, backup/restore regression, Windows
service identity and CUA driver interface tests. The canonical `scripts/check`
and `scripts/run_tests.sh` remain the verification entry points. PM builds the
test dependencies with the Feishu SDK, messaging/webhook dependencies and Telegram
adapter installed. Incomplete optional dependencies are not a passing result.

The native Windows installer lane reuses the existing real installation/update
journeys with `strict_acceptance=1`, including their crash-cell and teardown
evidence checks. The JavaScript lane reuses the existing full workspace checks
because the source snapshot also changes locked npm dependencies. Their Fork
runner selection uses the standard hosted runner; upstream retains its existing
Blacksmith runner selection.

`Fork required validation` succeeds only if every prerequisite job completed
with `success`. Queued, missing, cancelled, timed-out, skipped and failed jobs do
not satisfy it. Per-test platform skips remain in the evidence: a Windows-only
case needs actual Windows execution, a macOS-only case needs macOS execution,
and a file-symlink privilege skip on Windows
requires execution of the same contract on Linux. Other skips require review.
This selection is not the entire repository test suite.

Before merging, require strict checks on `main`, confirm its baseline is unchanged,
bind the merge request to the tested full head SHA, and compare the resulting tree
with the tested tree. Keep the source and validation receipts in maintenance
storage. Production installation requires its own target-SHA guard, backup,
recovery and runtime acceptance gates; this workflow does not complete those gates.

Two internal helpers now live with the topics they implement:
`gateway.platforms.base_thread_metadata.thread_metadata_for_source` owns adapter
thread routing metadata, and
`hermes_cli.gateway_service_identity.home_owns_bare_service_name` owns bare service
home selection. Their callers use those modules directly.
