# MikroFIX local supervisor

Supervisor is the fail-closed coordinator above the existing Qwen bridge. It
turns an owner-approved JSON plan into bounded implementation attempts, local
checks, a GPT-OSS review and an immutable candidate patch for Codex review.

The trusted control checkout and the agent-owned candidate repository may be
different paths. Pass `--repository-root /path/to/agent/repository` before the
subcommand. Trusted bridge, reviewer, executor controller and Python
environments still come from the protected checkout containing this script;
only the detached candidate worktree comes from the agent repository.

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
python3 tools/mikrofix_supervisor.py \
  --state-dir /home/mikrofix/MikroFIX_Agent_Workspace \
  --repository-root /home/mikrofix/MikroFIX_Agent_Workspace/repository \
  init /path/to/plan.json
python3 tools/mikrofix_supervisor.py workers start
python3 tools/mikrofix_supervisor.py workers status
python3 tools/mikrofix_supervisor.py grant-lease --minutes 15
python3 tools/mikrofix_supervisor.py resume
python3 tools/mikrofix_supervisor.py workers stop
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

The bridge process is the versioned `tools/mikrofix_bridge.py` from the trusted
source checkout, invoked by absolute path. The executor module and GPT-OSS
reviewer are likewise loaded from the trusted source checkout; none of these
control-plane programs is imported from the agent-editable worktree. The
executor's data root remains the isolated candidate worktree.

`workers start` manages two transient `systemd --user` units. It starts the
executor first, waits for a private non-symlink token and `/health` identifying
the exact worktree, and only then starts the bridge monitor. Existing units are
accepted only if their working directory, environment identity and command
match the immutable metadata. Partial or foreign state is stopped and rebuilt
in the safe order. Startup failure rolls both units back and revokes any lease.

Worker lifecycle and grants share a run-scoped lock, so concurrent commands
cannot create duplicate units or race a grant against shutdown. Logs are
pre-created as private regular files (`0600`) under `runs/<run-id>/logs/` before
systemd receives their paths.

`workers status` is read-only and reports `READY`, `PARTIAL`, `STOPPED` or
`UNHEALTHY`, plus unit identity, health and lease fields. `workers stop` is
idempotent, stops bridge before executor and always revokes the lease.

`grant-lease` works only while workers are `READY`. It invokes the trusted
control helper with the complete run/worktree/hash binding and intentionally
never supplies `--yes`; the owner must approve the graphical prompt. `resume`
(with `run` retained as an alias) requires both `READY` workers and the exact
active lease. If either precondition fails, supervisor state remains byte-for-
byte unchanged. Never edit the state JSON manually. `BLOCKED` requires
Codex/owner inspection.

Plans cannot place supervisor, bridge, executor, reviewer, their tests, or the
loaded agent prompts/rules in `allowed_files`. In supervised executor mode the
generic `system_command` and mutating `desktop_action` endpoints are disabled,
even when a valid lease exists.

Queued/running Qwen jobs have a bounded wait (`job_timeout_seconds`, default 30
minutes, allowed 30–7200 seconds). A timed-out active job moves to `BLOCKED`
instead of dispatching a potentially concurrent replacement. Inspect and stop
the workers before starting any later attempt.

Run the focused tests with:

```bash
python3 -m unittest -v tests.test_mikrofix_supervisor
```

The example systemd unit is intentionally not installed automatically. A
service must not be enabled until the owner has reviewed this first version.

## Safety boundary

Isolation and lifecycle management are real: bridge job storage, executor
root/state/config, transient units, logs, allowed-file enforcement and lease
identity are run-scoped. This still does **not** make the system fully
autonomous: the lease requires an owner-approved GUI prompt and the final state
remains `AWAIT_CODEX_REVIEW`. Nothing automatically merges the candidate patch
into the main checkout. All plan fields must be validated by `validate_plan`
which rejects missing required fields, empty constraints/acceptance-criteria or
context-link sections, unsafe or duplicate paths, and unknown properties.

The originating `CODEX_THREAD_ID` is only used when explicitly supplied for a
run; the bridge and supervisor never guess the newest thread. In the owner-only
default no thread is supplied, so completion remains a durable local event and
does not consume Codex usage. If a thread is explicitly bound, one at-most-once
wake is attempted with the absolute Codex binary. The bounded wake message contains
only the job ID, display name, mapped state, and event path; safe evidence
paths, hashes, test status, risks, and a concrete blocked need remain in the
private event. Raw model output and full diffs are never placed on the command
line. If no thread was captured, the event path is shown for manual review.
The durable claim prevents replay after restart, but also means a crash after
claim creation and before queue acceptance is not retried automatically.

## Open WebUI front door

`tools/mikrofix_openwebui_gateway.py` exposes an OpenAI-compatible front door.
It exposes every enabled pre-approval role exactly once, independent of the
installed model inventory. Open WebUI therefore shows a single
`Pogovorni lokalni vodja` entry for `conversation_lead`, not one duplicate per
LLM. `/llms` reads the live Ollama inventory and `/llm <tag>` chooses the
LLM before the first ordinary message creates a task.

Ordinary messages create or continue a conversation locked to the selected
role and previously chosen model. The role determines the prompt and contract;
the model runtime registry determines only technical context/thinking settings.
The assistant
response contains the immutable `MF-LOCAL-...` id, which the gateway reads from
subsequent chat history. A silent role or model change in the same conversation
fails closed. Owner slash commands are parsed by trusted Python code before
routing and never reach a language model. `/prepare-plan` asks the selected
conversation model for the structured first proposal, then invokes the fixed
validator and deterministic manager. Roles after `/approve-plan` remain
preconfigured in the supervisor flow.

The gateway listens only on the Docker host bridge address, requires a bearer
token stored outside the repository and intentionally exposes no generic file,
shell or model-selection endpoint. Install the example user unit, then add an
OpenAI-compatible Open WebUI connection with base URL
`http://172.17.0.1:8767/v1` and the private gateway token.
