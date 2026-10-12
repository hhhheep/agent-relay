# Harness maintenance feedback

This publication copy contains technical maintenance topics only. Historical local chat/field receipts are not part of the source release; they are not evidence of current deployment success.

- Keep workplace identity, command identity, task ownership, leases, artifact hashes and publication provenance stable across updates.
- Route concurrent work through declared workspaces and writable roots; terminal separation alone does not isolate files or external resources.
- Bound metadata collection and output retrieval; preserve exact raw streams through existing receipts rather than returning full logs.
- Exercise reconnect, cancellation, partial publication and interrupted update recovery with synthetic fixtures before live cutover.
- Track current failures and feature maturity in the extraction verification record. Record version, affected contract and regression evidence for future fixes.
