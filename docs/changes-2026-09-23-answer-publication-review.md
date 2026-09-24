# Answer publication review recovery

## Scope

Fix the general publication pipeline, without changing task-consumption limits,
replaying tools, migrating old tasks, or treating a successful execution as proof
of a scientifically correct explanation.

## Changes

- File references are distinguished from ordinary slash notation using complete
  observed identities, filesystem syntax, file suffixes and explicit file context.
  Scientific tokens and units have no dataset-specific allowlist. Actual private
  paths, unobserved file references, traversal and ambiguous aliases still cannot
  be published as verified references. Extensionless slash notation without file
  cues is display text only; it grants no filesystem access.
- Citation-correction root/indices failures receive one structural retry using
  the exact same frozen evidence and host-supplied expected indices. Accepted
  paragraphs cannot be reopened, and no correction can authorize execution.
- Technical failures remain `unavailable`; rejection requires a concrete failed
  source/file check. Withdrawing all paragraphs means the answer is incomplete,
  not scientifically rejected and not successful. Public wording distinguishes
  reference validation from scientific rejection.
- If citation repair or safe publication rendering changes a candidate, its old
  verdict is invalidated. A separate tool-disabled check reviews the exact final
  paragraphs against the same evidence. It cannot return replacement text or
  tools. One frozen-payload schema recovery is allowed; negative or uncertain
  scientific judgments do not trigger repeated requests for a passing verdict.
- Historical explanations explicitly select `historical_explanation` and cite
  only host-admitted, version-frozen prior results from the matching session/input
  scope. They do not establish new computations, current file availability or new
  deliveries. Failed/pending results and raw old tool observations remain excluded.

## Regression coverage

New cases cover generic slash notation, unknown/private file references, citation
protocol errors and transport failures, fully withdrawn answers, changed published
text and cancellation, native tool-call rejection, history identity/version
tampering, reused step IDs across plans, and sequential follow-up explanations.
Tests use synthetic observations and model-response fixtures, not user datasets.

Validation on this change: backend 8,190 passed / 31 skipped; frontend 2,745
passed, type checking and production build passed; sandbox 5,926 passed / 87
skipped; Compose configuration and patch whitespace checks passed. The sandbox
suite used the existing image in a disposable test container with test-only
dependencies and read-only source/fixture mounts; production dependencies were
not changed.

## Operational boundaries

Use the existing Compose stack via `./run.sh`; keep the configured loopback-only
port 7001. No old events or user datasets are rewritten. A live model verification
requires separately authorized synthetic input; deterministic regression tests do
not establish that every future model judgment is correct. Real missing evidence
and substantive scientific errors must still remain visible.
