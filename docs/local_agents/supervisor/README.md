# MikroFIX local supervisor

Supervisor is the fail-closed coordinator above the local executor bridge. It
turns an owner-approved JSON plan into bounded implementation attempts, local
checks, an independent local review and an immutable candidate patch for Codex
review. Role contracts are model-independent and are defined in
`docs/local_agents/PRAVILNIK_VERIGE.md`.

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

`DISPATCHED_TO_QWEN` and `GPT_OSS_REVIEW` are legacy serialized state labels.
They remain unchanged so old runs can still be read; they now mean the
model-independent `executor` and `local_reviewer` stages respectively and do
not select a model.

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
source checkout, invoked by absolute path. The executor module and local
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

Queued/running executor jobs have a bounded wait (`job_timeout_seconds`, default 30
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
LLM before the first ordinary message creates a task. The same command accepts
a model-specific preset and a bounded allowlist of runtime values, for example:

```text
/llm qwen3:4b-thinking-2507-q8_0 preset=deep
/llm qwen3:4b-thinking-2507-q8_0 num_ctx=32768 num_predict=12288 temperature=0.6
```

`/llm-settings` shows the effective preselection or locked task settings, while
`/llm-reset` clears only an unlocked preselection. The gateway signs the
preselection embedded in chat history. Unknown, duplicated, malformed or
out-of-range values fail closed, and editing the marker invalidates its
signature. Only `num_ctx`, `num_predict`, `temperature`, `top_k`, `top_p`,
`min_p`, `repeat_penalty`, `seed` and `think` are owner-selectable. Hardware and
contract controls such as GPU layers, threads, batching, stop sequences and
output format remain manager-owned.

Only an explicit `/research` envelope creates a new local task. It contains
separate owner-request and Codex-research-prompt sections. Ordinary messages
may continue an existing conversation locked to the selected role and model,
but cannot create an unrelated task. The role determines the prompt and contract;
the model runtime registry determines technical defaults and safe per-model
caps. The first ordinary message expands the signed selection into the
immutable profile snapshot. Effective generation options participate in the
snapshot hash and are copied into every bridge turn; later chat controls cannot
silently change them.
The assistant response contains the immutable `MF-LOCAL-...` id, which the gateway reads from
subsequent chat history. A silent role or model change in the same conversation
fails closed. Owner slash commands are parsed by trusted Python code before
routing. The research session exposes only the `reply` contract and read-only
tools. Its final report and tool evidence are stored as immutable hashed
manifests. `/prepare-plan` starts a separate tool-less `plan_preparer` session
from the stored owner request, Codex prompt, report and evidence manifests. It
stores and renders the exact plan revision but does not invoke the validator.
The owner may discuss changes and repeat `/prepare-plan`; `/plan-dispatch`
alone sends the latest displayed revision to the fixed validator and
deterministic manager. Roles after `/approve-plan` remain preconfigured in the
supervisor flow.

Autonomous downstream roles currently use separately bounded defaults on the
local 12 GiB GPU. The plan validator uses `num_ctx=32768`, `num_predict=4096` and
`think=low`; the executor uses `num_ctx=32768`, `num_predict=8192` and
`think=medium`; the independent local reviewer uses `num_ctx=32768`,
`num_predict=3072` and `think=medium`. Sampling remains at the model's native
defaults (`temperature=1`) because gpt-oss is a reasoning model; role prompts,
reasoning effort and strict output contracts provide focus. These values are
copied into each new run's immutable bridge configuration, so changing the
template never mutates an already dispatched run.

At `/prepare-plan`, the manager freezes an immutable, hashed `PlanIntent` with
stable turn ids, the root owner request, research-manifest hashes and Git
checkpoint. The separate `plan_preparer` must cite owner turn ids for the
objective and later decisions, while
explanatory questions are marked discussion-only. The manager derives the
validator task from those cited owner turns instead of trusting model-authored
prose. Empty plan sections, missing root provenance and source
overlap fail closed after deterministic normalization: the owner root is always
restored, lead turns cannot authorize execution, and otherwise unclassified
owner turns default to discussion-only. A
successful candidate is cached by the intent hash. Repeating `/prepare-plan`
without a substantive conversation change returns the same revision; a later
owner correction requires a new revision. `/plan-dispatch` fails closed if the
conversation changed after the displayed revision.

Before execution, `/reject-plan [reason]` (alias `/revise-plan`) preserves the
validated child and rejection record, clears it as an approvable candidate, and
reopens the same conversation. A fresh `/prepare-plan` is required before the
next dispatch.

`/replace-goal <nov cilj>` is the explicit escape hatch for the uncommon case
where the owner really wants to replace, rather than discuss, the original
goal. It is handled by trusted code without an LLM, appended to the audit trail
and becomes the new PlanIntent root. Earlier owner turns are then superseded and
cannot be cited as objective or decision sources.

Open WebUI keeps the streamed request open for the two asynchronous owner
checkpoints. `/plan-dispatch` returns in the originating conversation only
after the validator has produced `READY_FOR_OWNER_LEASE`, together with the
exact candidate id and `/approve-plan`. `/approve-plan` returns only after the
supervised executor and local reviewer reach `READY_FOR_CODEX_REVIEW` or
`BLOCKED`, together with the next owner action. Ten-second empty stream deltas
keep the HTTP connection alive without inventing progress. The standalone
local manager UI remains asynchronous because it polls the same durable state.
If an Open WebUI tab disconnects, the durable job and desktop terminal
notification remain authoritative and `/status` retrieves the checkpoint.

During a streamed conversation, the gateway forwards the `thinking` text that
Ollama actually returned as OpenAI-compatible `reasoning_content`. Open WebUI
can therefore render it in its separate collapsible reasoning block. Progress
appears after each completed model step, together with bounded tool-call and
tool-result labels; the bridge deliberately remains non-streaming within an
individual Ollama call so its existing supervised tool loop is unchanged.
Request content and full tool results are not copied into the reasoning stream.
This display is diagnostic only: the append-only bridge trace remains the
authoritative evidence of model and tool activity.

The gateway listens only on the Docker host bridge address, requires a bearer
token stored outside the repository and intentionally exposes no generic file,
shell or model-selection endpoint. Install the example user unit, then add an
OpenAI-compatible Open WebUI connection with base URL
`http://172.17.0.1:8767/v1` and the private gateway token.
