# Workplace acceptance follow-up — 2026-10-12

Status: partial P1 acceptance. User explicitly authorized installation packages and isolated workplace installation/testing/update/rollback. Existing installed plugins, global control profiles and live Worker installation were not switched. Private paths, original deployment configuration and raw receipts remain outside Git.

## Legacy regression repair

The original nine failures were reproducibility defects in tests relying on intentionally excluded research checkout files. Three remote tests lacked synthetic conda profiles and required bootstrap input files; six continuation tests copied a planner policy only if it happened to exist in the checkout. The remote suite now uses a temporary workspace with synthetic inputs and the actual core source. The continuation suite supplies an explicit bounded-probe policy. All previous behavior assertions are retained. Runtime missing-policy/missing-bootstrap behavior still fails closed. Two legacy suites plus workflow-profile regressions: **55 passed**. This does not provision production research policies.

## Actual target and source identities

- Client: same Windows x64 host, new isolated source/venv; Python 3.13.12, Node 24.19.0. Python pins installed from public PyPI with cache disabled. Node locks installed using a new npm cache and pnpm store; fresh consumer tests: 23 passed.
- Worker: actual Linux x64 workplace, kernel 6.8, isolated directory. Python 3.13.16 obtained through uv 0.13.0 (downloaded wheel checked against its PyPI SHA256). Exact Linux Worker pins install from public PyPI, `pip check` passes. No system interpreter, service or current Worker installation was replaced.
- Existing release: `harness-0.6-cdf4c9cbb9e48fb2`, source root `cdf4c9cbb9e48fb29f6c924795f2aa56d3675b96ab842b7033c67c173520da5d`.
- Candidate release: `harness-0.6-4e69022edc545f89`, source root `4e69022edc545f89b24c206c9a260e9be719752088901f00deeb937e521b27b2`.
- Both retain MCP schema `0e4f66158a663c674a761cf70253cb6cda09b34167c99e75643892ae6556b4b4` and the original eleven tools.

## Matrix evidence and limits

| Row | Actual evidence | Full-row status |
| --- | --- | --- |
| M1 | New isolated Windows client with original paired Client manifest connects through release-owned launcher to existing Linux Worker; matching loaded identity, correct cwd, supervised command, receipt hash and original-ID lookup pass | PARTIAL: same physical Windows device and Harness component coverage |
| M2 | Fresh isolated Windows dependencies and fresh Linux Worker installation; independent workplace identity; real stdio list/dispatch/result/hash/original-ID lookup pass | PARTIAL: Harness component coverage, remaining whole-system coverage pending |
| M3 | Existing isolated client profile, original receipt and synthetic SQLite history preserved by hash; add a new explicit binding, connect to candidate Worker, verify new target and original-ID lookup | PARTIAL: isolated configuration/history fixture; no live-client cutover |
| M4 | Exact old ZIP activates in a private namespace; original/candidate/original stdio activation, hash-reviewed update/rollback, command recovery and configuration/state/receipt preservation pass | PARTIAL whole-system; isolated Harness update/rollback PASSED |

Private local evidence is under `.verification/workplace-matrix/`. Successful M1 stdout receipt SHA256: `c736ea7cc85e797120ed4ac888ba23f9adc427ed4514a2a755e3d5d4abc90ff0`. M2/M3 isolated target stdout receipt SHA256: `59831b1d9e32dce1aa6c090579c89070919dadbfbf94ed80e1a21dfe02ddf4ff`. Receipts record original command IDs and exact execution releases. Commands print cwd and a fixed acceptance marker; no research job or paid model is invoked.

The M4 error is `profile_root_mismatch`: the original configured runtime root differs from the isolated installation root. Editing that old profile changes the old source identity because its allowlist hashes the profile. We do not loosen `verify_pair`, alter the immutable archive, or treat a relocated/reissued package as the original old release. That initial blocker is now resolved by the genuine private namespace documented below.

After the M3 switch, the old binding also recovered the original M1 command without resubmission. A deliberate old-Client/new-Worker identity mismatch was rejected during server startup by the unchanged protocol gate, before any acceptance command was submitted. The candidate manifest carries prior component acceptance only while the built release ID and source-root hash still equal the tested candidate; overall status remains `P1_CANDIDATE_NOT_VERIFIED`.

## Packaging and diagnostic findings

The client source ZIP originally omitted `plugins/solution-agent-harness/release_manifest.json`, required by its existing remote launcher. The builder now generates this from the Worker builder's exact runtime identity plus Worker archive provenance. Archive payload digest and runtime source digest have distinct meanings and are not substituted for one another.

The acceptance client preserves Windows standard environment variables used by OpenSSH, including its global configuration location, while excluding provider credentials. It selects the SDK's documented initialize handshake for the unchanged Harness MCP surface; protocol 0.6 release checks still run unchanged. Remote profile key casing must match the actual deployed key. Supervised systemd jobs have their own PATH, so the probe uses the confirmed Linux `python3`, rather than assuming `python` is available. Failed probes and diagnostic attempts are preserved; uncertain results were queried by their original IDs before any new command.

Final affected legacy/workflow/bootstrap/installation suite: **59 passed**. Fresh client Node consumer suite: **23 passed**. Publication check: 979 Git candidate files, no findings. Immutable source map unchanged; 19 extracted files now have explicit engineering change provenance. Prior 130-pass core/Guard and 77-pass lifecycle evidence remains separate from this follow-up.

## Resolved original-root blocker

Following explicit user approval for the helpers and clean engineering test package, bubblewrap/user namespaces and user-systemd cgroup delegation were verified. `scripts/release/run_linux_namespace.py` maps the unchanged isolated release onto its historical runtime path, with private workplace/run/state mounts. Mount, PID, network and cgroup namespaces isolate execution. No host HOME, research filesystem or host D-Bus socket is mounted. A transient Delegate=yes user unit owns cleanup; a private systemd manager supervises actual Harness jobs. Root permission and persistent service configuration changes were unnecessary. Reproduction method: [NAMESPACE_ACCEPTANCE.md](NAMESPACE_ACCEPTANCE.md).

Both clients and servers passed strict protocol/workplace checks and exposed eleven canonical tools. The existing overlay installer applied an exact original-hash-reviewed forward plan and an exact-current-hash reverse plan. Each apply was bound to its dry-run plan hash. The Windows acceptance client selected the matched retained plugin for each server. Actual loaded sequence: `harness-0.6-cdf4c9cbb9e48fb2` → `harness-0.6-4e69022edc545f89` → `harness-0.6-cdf4c9cbb9e48fb2`.

The candidate recovered the original old command ID. The rolled-back old release recovered both the old command and a candidate-release completed command, preserving execution-release IDs and stdout hash `c736ea7cc85e797120ed4ac888ba23f9adc427ed4514a2a755e3d5d4abc90ff0`, without resubmission. Seven original configuration/workplace/SQLite backup/command-result files retained exact hashes through update and rollback. SQLite rows and consistent backup were verified; the prior M3 client profile, receipt and SQLite history also retained their recorded hashes. Original archive bytes remained unchanged.

Real artifact store, inspect, diff review, create-only materialize plan and CAS commit passed for M2, M3 and rolled-back M4. Synthetic source, receipt and target all hash to `d948f658bbb3595257ea920fb9ee6ae27c7c60047f08a401fb8e53500a5d8afd`. Eight cross-end receipts and original-ID links were checked together under ignored `.verification/namespace-m4/`. Linux core/legacy/install/namespace suite: **153 passed**, no skips. Windows namespace boundary tests: **3 passed**. This clears the path blocker and verifies isolated Harness update/rollback; wider whole-system and independent-device coverage remain separate.

Retained diagnostics: mount ordering initially hid the isolated interpreter; a separate dbus-run-session bus could not activate the private manager; the read-only release needed an existing state mountpoint before mounting private state. All were repaired in the helper. One recovery used an incorrect ID and returned not_found without submitting a command; corrected recovery reads the exact original receipt ID. Windows sandbox tempfile restrictions initially failed the boundary tests; unchanged assertions pass in the authorized normal local environment. Failed attempts are not counted as acceptance.

Linux Token Guard suite also passed: **37 passed** (190 Linux tests across the two suites). Final checks confirmed all seven original hashes and the immutable old ZIP remained unchanged, with no acceptance transient units left running.
