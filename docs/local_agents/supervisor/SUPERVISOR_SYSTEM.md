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
5. Run only named, hard-coded check profiles. Never execute a command supplied
   by a plan or model.
6. GPT-OSS must return an unambiguous approval. Missing or malformed verdicts
   are rejections.
7. An atom gets at most three implementation attempts. Preserve concrete gate
   feedback in the next prompt. At the limit, persist `BLOCKED`.
8. Persist every transition atomically. Unknown schemas, states, paths or tool
   output fail closed.
9. Never run checkout, restore, reset, clean, merge, push or deployment.
10. The terminal success state is `AWAIT_CODEX_REVIEW`, never “deployed” or
    “merged”. The checkpoint is an artifact, not approval.
11. Bridge home/config and executor root/config/state are run-scoped. Never
    fall back to the global bridge or the main checkout for an isolated run.
