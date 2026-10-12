# Safety contract

The guard is successful only when it reduces model-facing output without changing command behavior or concealing evidence needed for the next decision.

## Invariants

1. Execute the supplied argument vector directly. Never reinterpret it through a shell.
2. Capture stdout and stderr byte-for-byte in separate files before summarization.
3. Record SHA-256 and byte counts for both streams. `verify` must reproduce them.
4. If valid UTF-8 text fits the configured inline budget, return it completely. Preserve non-UTF-8 bytes in raw files and mark the stream so replacement-decoded text is never mistaken for exact binary evidence.
5. If content is omitted, set `lossy: true`, report omitted bytes, and provide exact recovery coordinates.
6. Exit code, timeout state, and launch errors are never inferred from text.
7. Signals are deterministic hints. They are not a semantic summary and cannot authorize a scientific conclusion.
8. Failure evidence gets a larger inline budget, but a nonzero exit still requires exact traceback or error retrieval before repair.
9. Never delete or overwrite prior run directories. Each run receives a unique identifier.
10. Never log environment variables or unredacted secret-looking command arguments.

## Safe expansion

Add a new compression rule only with fixtures demonstrating both reduction and retention of the lines required to make the correct decision. Prefer format-preserving transformations and explicit projections over free-form summarization. A parser failure must fall back to the generic recoverable receipt, not an empty or guessed result.

Automatic hooks remain disabled until an A/B replay shows that critical numeric values, failure causes, file identities, and go/no-go decisions match the raw-output baseline.

## Suggested acceptance gate

- All raw hashes verify.
- All injected failure and warning lines remain discoverable.
- All expected research metrics are recoverable by literal query or line range.
- Small valid UTF-8 text, including line endings, is exact in `inline_output`; arbitrary bytes remain exact in the raw file and hash.
- No command is executed twice merely because compression omitted context.
- Model-facing output decreases on the representative replay corpus.
