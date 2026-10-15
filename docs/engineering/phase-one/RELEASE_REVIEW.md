# P1 delivery review

Final engineering review is complete for the scope in [DELIVERY.md](DELIVERY.md): S1–S5, current bd matrix, clean install/build and production connection checks. Source and package publication retain their actual commit/hash/CI identities. Live cutover is a separate decision.

## What a recipient receives

The unique repository source, exact Python/Node inputs, client source ZIP, Worker ZIP, standalone Writer ZIP and existing bootstrap/overlay installers. `candidate.json` records the producer commit, dirty flag, exact artifact hashes, runtime/contract identity and scoped prior acceptance. Router helpers and XATS use their existing builders and target-native dependencies as documented in [INSTALL.md](INSTALL.md). Private machine configuration, old baseline artifacts and raw execution state are not published.

## Review before publication

| Gate | Current evidence / required action |
| --- | --- |
| Unique source / historical provenance | Immutable extraction map and 27 explicitly changed extraction files; run `python scripts/release/provenance.py`. |
| Publication boundary | Run `python scripts/extraction/check-publication.py` before staging/building; review Git candidates, not the developer directory. |
| Supported install targets | Windows x64 clean same-host archive install and Linux x64 fixed isolated install; source/lock/native rebuild evidence in [STATUS.md](STATUS.md). |
| Old baseline and rollback | Exact supported original Harness ZIP retained privately; M4 old/current/old identities, 13 preserved hashes, SQLite-consistent backup and original command lookup passed. No arbitrary old-version support. |
| Native host / remote adapters | Real HarnessPort, production NativeAppPort identity/read and Writer own SSH validation passed. Native message sends and paid drafts remain unverified business actions. |
| CI coverage | Candidate workflow now includes core/legacy/install, Token Guard, real shared-Provider consumers, target-native SQLite/XATS, 69 Lifecycle + 8 goal tests, Router build and standalone Writer check. Local commands have receipts; [GitHub Actions run 38225690945](https://github.com/hhhheep/agent-relay/actions/runs/38225690945) passed on published commit `004fe29031f6b7f9b0e5807f20050e2806f39367`. |
| Rebuild and handoff | Build from a clean reviewed commit; follow private configuration and output ownership in INSTALL. Hash and dry-run the installer before applying only to the selected target. |

CI is a reproducibility check of the declared Windows target; it does not replace Linux or M1–M4 external receipts. No global plugin/cache/hooks, live service, research DB or queue is part of the proposed publication.

## Concrete proposed next actions

1. Publication and clean-host CI are complete for commit `004fe29031f6b7f9b0e5807f20050e2806f39367` on `codex/phase-one-restructure`. The repository is public at the user’s request. Follow-up documentation changes require their own published commit and workflow result. Do not push main, merge, tag or upload private acceptance packages; retain exact commit/run identities and fix fresh-run failures in the source owner.
2. If real native message delivery is part of the requested final support claim, select one explicitly authorized test chat and fixed engineering message. Use production routing/native tools, verify the exact returned destination and actual response; never treat a tool ACK as a completed answer. Prepare/review this before sending; do not infer permission from native read access.
3. Paid report generation and live installation remain separate requested use-cases. No paid call, production service activation, hooks replacement or research dispatch is proposed by this review. Their absence is not a reason to hide the completed isolation results.

## Stop and recovery conditions

Wrong workplace/release/schema, unknown state ownership, overlay conflicts, dirty/untraceable artifacts or failed required tests stop the relevant action. Query original command IDs after uncertainty; do not resubmit to make a receipt green. Roll back code/binding only using the verified retained baseline and preserve state/receipts. Do not narrow the system claim to whichever component happens to pass.

The next delivery decision must be based on the concrete source diff, generated artifact hashes and CI evidence, rather than the existence of this document. Current evidence and known exclusions remain in [STATUS.md](STATUS.md).

## Current review input after runtime repair

Candidate repair commit `d0481c629085a7b09bc4483f82bfa3ba1040788c` has green [CI run 38228079618](https://github.com/hhhheep/agent-relay/actions/runs/38228079618), fresh Windows/Linux Python archive checks, Linux 194-test regression, current bd M1–M4 and a real default-adapter production Manager connection tick. The [workplace record](WORKPLACE_ACCEPTANCE.md) distinguishes prior runtime evidence and retained failed acceptance configurations. Two private-configuration examples now document actual Manager/XATS entrypoints without enabling services. Final review must audit the rebuilt clean-commit archives, source provenance and S1–S5 handoff before changing the candidate delivery claim. Main received only its separately authorized README update, not candidate code.
