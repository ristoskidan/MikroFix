# Supervisor invariants

1. Only an owner/Codex-approved plan defines the objective, atoms and file
   scope. The supervisor never invents or changes business rules.
2. Before every Qwen attempt, require the short lease belonging to this exact
   run, worktree, plan hash and allowed-files hash. If it is unavailable or
   mismatched, persist `NEEDS_WRITE_LEASE` and stop.
3. Compare the real workspace snapshot from immediately before dispatch with
   the snapshot after completion. Claims in an agent response are not proof.
4. A changed path outside `allowed_files` blocks the run immediately. Never
   conceal, revert or delete it automatically.
5. Run only named, hard-coded check profiles: syntax_changed_python, local_agent_tests,
   desktop_unit_tests, central_api_tests. Never execute a command supplied
   by a plan or model.
6. GPT-OSS must return an unambiguous approval. Missing or malformed verdicts
   are rejections. Plan must contain required fields: objective, constraints,
   context_links (canon/code/tests), acceptance_criteria. Additional properties fail closed.
7. An atom gets at most three implementation attempts. Preserve concrete gate
   feedback in the next prompt. At the limit, persist `BLOCKED`.
8. Persist every transition atomically. Unknown schemas, states, paths or tool
   output fail closed.
9. Never run checkout, restore, reset, clean, merge, push or deployment.
10. The terminal success state is `AWAIT_CODEX_REVIEW`, never “deployed” or
    “merged”. The checkpoint is an artifact, not approval.
11. Bridge home/config and executor root/config/state are run-scoped. Never
    fall back to the global bridge or the main checkout for an isolated run.
12. Invoke the versioned bridge and reviewer only from absolute trusted-source
    paths. They and every loaded system prompt/rule are protected control-plane
    files and cannot appear in an implementation plan.
13. A queued/running job that exceeds its bounded wait stops in `BLOCKED`.
    Never dispatch a replacement while the earlier worker may still mutate the
    worktree.
14. Start, stop and lease grant share one run-scoped lifecycle lock. Start the
    executor, verify private token and exact health/identity, then start bridge.
    Stop in reverse order and revoke the lease.
15. Existing transient units are trusted only after matching immutable cwd,
    environment identity and command. Logs are private non-symlink regular
    files prepared before systemd-run.
16. Lease grant requires `READY` workers and a human GUI decision. Resume
    requires both `READY` and the exact bound lease; failed preconditions must
    not modify supervisor state.
17. Roles are exact: Codex — vodja projekta in končni pregled; GPT-OSS —
    lokalni vodja izvedbe; Qwen — lokalni izvajalec; Bridge — nadzorni most.
    Codex does not create paid Codex subagents unless the owner explicitly asks.
    The local lead may use supervised local workers or sequential atoms, never
    uncontrolled parallelism.
18. Missing, empty, and wrong-type plan fields are distinct failures. Keep the
    schema, validator, fixtures, prompts, and tests aligned; reject unknown
    plan, atom, and context keys, duplicate links, and unsafe paths.
19. Before READY, inspect `project_diff`, inspect the final diff, and run the
    named check profile. READY must include the exact command, actual test
    count, PASS result, and remaining risks. Never claim a test passed without
    real output. Do not leave duplicate functions or dead code; inspect a tool
    schema and correct its parameters instead of repeating an invalid call.
20. If no owner/Codex input is needed, adapt and continue. Do not apologize or
    emit a generic inability message. A real block uses exactly: `BLOCKED`,
    `Naloga`, `Opravljeno`, `Natančen vzrok`, `Dokaz`, `Poskušeni koraki`,
    `Potrebujem`, and `Naslednji korak`.
21. Bind `CODEX_THREAD_ID` only at run initialization. After terminal state is
    durably stored, enqueue a private event and make one at-most-once Codex wake
    with the job ID, display name, state, safe evidence path/hash, test result,
    risks, and concrete blocked need. Never guess a recent thread or send raw
    model output, full diffs, or secrets. Without a bound thread, retain the
    event and show its path. A claim intentionally prevents automatic retry;
    a crash after claim creation but before queue acceptance can require manual
    review of the durable event.
