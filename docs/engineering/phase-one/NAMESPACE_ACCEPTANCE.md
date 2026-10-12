# Path-preserving Linux Harness acceptance

The original supported package includes an absolute deployment profile in its release hash. Changing that profile produces a different release. Moving its files alone fails `profile_root_mismatch`. The acceptance helper preserves both the payload and the paths seen by the process, while the actual files live in an explicitly authorized isolated directory.

This method passed on the declared Linux x64 workplace with Python 3.13.16, bubblewrap, cgroup v2 and a systemd user manager supporting `Delegate=yes`. It does not require root or change the current installed Worker. Other hosts need their own capability check. Missing namespaces or delegation are real environment blockers; do not silently substitute the host user bus.

## Filesystem and execution boundaries

`scripts/release/run_linux_namespace.py` reads a private JSON file. The selected release's original profile supplies the historical runtime, run and workplace paths. The helper maps an isolated release read-only at the original runtime path, and maps private state/run/workplace directories read-write at the corresponding paths. Isolated Python and venv inputs are read-only. OS libraries, minimal account files, private `/proc`, devices and temporary files support execution. Host HOME, research directories, credentials, network and host D-Bus are unavailable.

A transient user unit delegates only its own cgroup. Inside the namespace a private systemd user manager starts its own user bus and supervises the unchanged Harness `systemd_transient` backend. The manager exits when the acceptance client closes, and the outer transient unit owns process cleanup. Use completed test commands; this short-lived environment is unsuitable for production or active-job continuity tests.

Use one active acceptance connection per private configuration/runtime directory. Give concurrent tests separate namespace configurations and runtime directories so their private user buses cannot collide.

## Private configuration

Create all selected directories strictly beneath the authorized test root. Use an isolated supported old installation and target-native dependencies. Example values below are synthetic:

```json
{
  "sandbox_root": "/work/example-relay-test",
  "release_root": "/work/example-relay-test/installed",
  "python_root": "/work/example-relay-test/python",
  "venv_root": "/work/example-relay-test/worker-venv",
  "workplace_root": "/work/example-relay-test/workplace",
  "run_root": "/work/example-relay-test/runs",
  "runtime_root": "/work/example-relay-test/runtime",
  "state_root": "/work/example-relay-test/state",
  "profile_id": "example_worker"
}
```

`profile_id` must match an actual key in the retained old package's private profile. The original runtime/run/workplace paths must be absolute and disjoint. Escaping the sandbox, overlapping historical paths or a symlink state mountpoint fails before execution. The helper creates an empty state mountpoint in the isolated installed tree; payload files remain unchanged. Keep this configuration and historical profiles outside Git.

The original launcher expects `<original-runtime>/.venv/bin/python`. Preserve that interpreter path with a private installed-tree `.venv` reference to the isolated Linux venv. Keep the Python interpreter's original installation path accessible through `python_root`; never copy a Windows venv. The namespace maps the declared venv and Python paths after the private workplace mount so they remain accessible.

## Reproduce the sequence

1. Verify the exact original ZIP hash and paired client manifest. Fresh-install the immutable old package into a new isolated directory with the existing bootstrap installer.
2. Create a synthetic workplace identity and copy identical canonical bytes to the isolated Windows client. Prepare private run/state directories, explicit client control declarations, synthetic history and an SQLite-consistent backup.
3. Check namespace support and private systemd supervision with the helper, supplying an explicit command after `--`. Its private JSON file is the only host-specific input.
4. Use `scripts/release/check_harness_remote.py` with the ordinary explicit client inputs plus `--namespace-config`, `--namespace-helper` and `--remote-python`. It reuses the selected release's own route and expected protocol/workplace identity. These options affect acceptance transport only.
5. Run the old/old stdio probe, retaining its original command ID and receipt. Snapshot original profile, workplace identity, history, consistent backup and immutable command-result hashes.
6. Produce a forward release using the existing builder and a review mapping each differing original file hash to the exact candidate source hash. The existing local `rehearse_harness_update.py` can prepare and verify private forward/reverse artifacts. These artifacts may contain historical private profiles: retain them privately and transfer only to the explicitly authorized isolated target.
7. Run the existing overlay installer against the isolated installed tree. Inspect conflicts and dry-run plan; apply only with that exact `plan_sha256`. No force overwrite is used. Switch the isolated client selection to the paired candidate plugin/manifest, then verify actual loaded identity.
8. Recover the old command by its exact receipt ID. Submit a separate fixed candidate acceptance command, record its execution release, and verify all original configuration/state/receipt hashes.
9. Apply the reviewed reverse artifact, again binding apply to the exact dry-run hash. Select the retained old client and verify the original loaded server identity.
10. Recover both the original and candidate-release completed commands without resubmission. Verify history rows, backup, original hashes and immutable original ZIP again. Check that no acceptance transient units remain.

Optional `--artifact-source` and `--artifact-target` run real store/inspect/materialize plan and CAS commit. Source content must be exactly `agent-relay-artifact-acceptance` followed by LF. Prepare that synthetic input under the isolated runtime's private state mount; use a new target each time. The checker reads the plan's diff before committing, retains its lease/plan IDs, and checks both receipt and actual target hashes. It does not broaden artifact scope or overwrite existing targets.

## Measured result and scope

The exact original/candidate/original Harness sequence, strict pairing, actual systemd commands, forward/reverse overlay plans, original state/configuration preservation, consistent backup and backward reads passed. M2/M3 and rolled-back M4 artifact round trips also passed. Evidence and limitations: [WORKPLACE_ACCEPTANCE.md](WORKPLACE_ACCEPTANCE.md).

This removes the original-root blocker. It proves isolated Harness installation/update/rollback on the tested endpoints. It does not by itself verify every other component, another physical Windows device, live cutover, active-command handoff or a full P1 release.
