# MikroFIX local supervisor

Supervisor is the fail-closed coordinator above the existing Qwen bridge. It
turns an owner-approved JSON plan into bounded implementation attempts, local
checks, a GPT-OSS review and an immutable candidate patch for Codex review.

It never merges, pushes, deploys, changes business rules, or bypasses the
executor write lease. Every initialized run gets a detached Git worktree under
`~/.local/state/mikrofix-supervisor/worktrees/<run-id>`. The main checkout is
not the executor root. Each atom is additionally guarded by a before/after
workspace snapshot and any change outside `allowed_files` blocks the run.

## State flow

```text
PLANNED -> DISPATCHED_TO_QWEN -> DIFF_VERIFICATION -> LOCAL_TESTS
        -> GPT_OSS_REVIEW -> (next atom or CHECKPOINT) -> AWAIT_CODEX_REVIEW

PLANNED -> NEEDS_WRITE_LEASE -> PLANNED
any failed gate -> PLANNED (maximum three attempts) or BLOCKED
out-of-scope mutation -> BLOCKED immediately
```

`CHECKPOINT` creates `candidate.patch` and `manifest.json` below
`~/.local/state/mikrofix-supervisor/artifacts/<run-id>/`. It does not create a
Git commit. The manifest records review/test evidence and explicitly leaves
merge, push and deployment to a human-reviewed workflow.

## Use

Create a plan from `PLAN_SCHEMA.json`, then:

```bash
python3 tools/mikrofix_supervisor.py init /path/to/plan.json
python3 tools/mikrofix_supervisor.py status
python3 tools/mikrofix_supervisor.py run
```

Initialization creates, but does not launch:

- the isolated worktree;
- an immutable normalized `plan.json` and `allowed-files.json`;
- a per-run bridge home and `config.json` using the bridge's actual keys
  (`ollama_url`, `model`, `executor_url`, `token_file`, `project`,
  `system_prompt`, `num_ctx`, `max_steps`);
- executor environment metadata binding its root, config directory, state
  directory, allowed-files manifest, run id and both manifest hashes;
- the exact per-run lease path.

Review `runs/<run-id>/environment.json`, then launch the listed processes with
their listed environments in this order:

1. start the **executor** first; its command uses
   `source_root/.venv-server/bin/python` and creates the per-run API token;
2. only after the token exists, start the **bridge monitor**, which reads that
   token through its per-run `token_file` setting.

They must run as separate per-run processes; the global bridge
monitor/executor are not used for an isolated run. Initialization fails closed
if the server virtual-environment interpreter is absent or not executable.

The supervisor deliberately does not mint its own lease. Until an
owner-controlled helper creates a short lease containing the exact `run_id`,
`project_root`, `plan_sha256` and `allowed_files_sha256` at the listed lease
path, it persists `NEEDS_WRITE_LEASE` and exits safely. After such a lease is
granted, start `run` again. Never edit the supervisor state JSON manually.
`BLOCKED` requires Codex/owner inspection.

Run the focused tests with:

```bash
python3 -m unittest -v tests.test_mikrofix_supervisor
```

The example systemd unit is intentionally not installed automatically. A
service must not be enabled until the owner has reviewed this first version.

## Safety boundary

Isolation preparation is real: bridge job storage, executor root/state/config,
allowed-file enforcement and lease identity are run-scoped. This still does
**not** make the system fully autonomous. Process launch and the short
run-specific lease remain owner-controlled, and the final state remains
`AWAIT_CODEX_REVIEW`. Nothing automatically merges the candidate patch into
the main checkout.
