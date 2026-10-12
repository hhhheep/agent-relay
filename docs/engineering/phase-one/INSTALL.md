# Install, update and extend the P1 candidate

Status: P1 engineering candidate. Current isolated Windows/Linux archive installation and Harness M1–M4 passed, including exact old/current/old activation, configuration/state preservation and rollback. Native App identity/read and Writer remote validation also passed. Remote CI passed on the stated repair commit; formal activation is separate. See [current status](STATUS.md). No live installation was switched.

## Requirements and source

Windows x64 client: Python 3.13, Node 24, Git, npm and pnpm 11.25. Linux x64 Worker candidate: Python 3.13, Git, OpenSSH, bash, tmux; systemd user support when selecting systemd_transient jobs; external Codex CLI where worker dispatch is used. The exact Python pins have passed isolated Linux x64 / Python 3.13.16 installation and pip check, including a fresh no-cache installation. Lifecycle/XATS requires Node 24 and a native better-sqlite3 build matching OS/arch/ABI. Linux x64 / Node 24.19.0 frozen installation, native rebuild and memory SQLite check now pass. Do not copy Windows native modules to Linux.

Clone the public repository and checkout the reviewed engineering commit, and keep it separate from research state. Source baseline bf8db034ee0e72967296b916968dfe2460e35cbe is historical provenance, not a supported installed release.

From any current directory (replace checkout with your checkout path):

```powershell
python checkout/scripts/release/install_dependencies.py --role windows-client
python checkout/scripts/release/install_dependencies.py --role windows-client --with-xats
```

The second command adds selected XATS dependencies without running install scripts. Build its native SQLite dependency with the upstream build tooling for your exact Node ABI, then run `node checkout/scripts/extraction/build-local.mjs`. No service is started by this build. The installer installs into the checkout venv and component node_modules only. `--python-only`, `--node-only`, and `--venv` allow explicit subsets; Python dependencies use public PyPI, isolated pip and exact pins. They do not use a research venv or personal provider credentials.

Linux worker candidate:

```sh
python3 checkout/scripts/release/install_dependencies.py --role linux-worker
checkout/.venv/bin/python checkout/plugins/solution-agent-harness/scripts/server.py --check
```

For optional Node components on Linux, run the following from the checkout root with Node 24 and pnpm 11.25 on PATH. Keep registry caches and state local to the test checkout when rehearsing a fresh installation:

```sh
npm ci --prefix packages/report-provider --ignore-scripts --no-audit --no-fund
npm ci --prefix components/lifecycle --ignore-scripts --no-audit --no-fund
npm ci --prefix components/goal-routing-integration --ignore-scripts --no-audit --no-fund
pnpm --dir plugins/research-report-writer install --frozen-lockfile --ignore-scripts
pnpm --dir components/xats-routing-lab/upstream install --frozen-lockfile --ignore-scripts
npm rebuild --prefix components/xats-routing-lab/upstream better-sqlite3
npm --prefix components/lifecycle run build
node scripts/extraction/build-local.mjs
```

The Python-only Linux Worker installer does not install these optional Node components. The Windows installer includes goal-routing dependencies alongside Lifecycle. Run Lifecycle/goal tests from `components/lifecycle`, with `PYTHONPATH` set to the absolute checkout root and the checkout's isolated `.venv` installed. All Python-backed fixtures choose the platform's venv executable.

Worker bootstrap packaging is available via `python checkout/scripts/release/build_candidate.py`; outputs are ignored `dist/p1-candidate/`. Build on the Windows producer after installing lifecycle build dependencies. `research-report-writer.zip` is a standalone plugin distribution, bundling the shared Provider and SDK with dependency licenses. It runs without adjacent source packages/node_modules; use this generated artifact for plugin cache installation, rather than copying only the raw Writer source folder. Run `node scripts/release/check_writer_plugin.mjs` for real stdio tool-surface verification with no credentials. The candidate builder runs publication and provenance gates, invokes the existing bootstrap builder, and checks archive bytes. Client source package retains the compact repo layout, including core, shared provider, lifecycle and selected upstream source. It also carries the generated `plugins/solution-agent-harness/release_manifest.json` paired to the Worker; retain it when configuring the client plugin. Dependencies are installed again on the destination; local venv/node_modules are excluded. `candidate.json` carries commit/dirty flag, identity, artifact hashes and honest pending targets. A dirty build is a development candidate only.

For a fresh isolated Worker directory use the bundled existing `install_bootstrap_release.py --archive harness-worker.zip --expected-sha256 <verified SHA> --target-root <new-directory>`; inspect the dry run, then repeat with `--apply`. It refuses nonempty targets. The candidate also delivers `install_overlay.py` separately. For existing managed overlays use `scripts/harness/install_overlay.py` dry-run/plan hash handling; never use force overwrite. Follow the existing ownership/conflict gates.

## External configuration

Copy `config/harness_remote_deploy_profiles.example.json` to a private per-machine location and supply your own values. Export `SOLUTION_AGENT_PROFILES_FILE` to that file and `SOLUTION_AGENT_PROFILE_ID` to the chosen entry. Both terminal service and Codex worker resolve the same file. The legacy checkout config path remains a fallback. The example is not an authorized SSH destination and is never auto-activated. MCP declaration passes through these variable names, without secret values. Keep the Python package root accessible as `SOLUTION_AGENT_ROOT` where discovery cannot locate it.

Use the existing workplace init and identity/preflight helpers only against an explicitly selected project. Client and Worker `.solution-agent/workplace.id` must be identical canonical UTF-8 bytes. Preserve protocol 0.6 exact protocol_version/release_id/payload_sha256/mcp_schema_sha256 equality and workplace checks. Unknown or mismatched peers must not submit jobs or apply artifacts.

Writer: supply `REPORT_WRITER_ALLOWED_ROOTS` plus Bailian environment values or `REPORT_WRITER_ENV_FILE`. Environment values win over file values; fixed model/preset remains unchanged. No API key is shipped. Start the Writer with an absolute path to `plugins/research-report-writer/server.mjs`, or install the plugin with its normal plugin-relative cwd. Harness also supports absolute script invocation from arbitrary cwd. Background configuration is explicit: copy config.example.json to private config, supply vault/state/rules/credential references, then use `node tools/research-background/cli.mjs --config <private-config> status`. Relative config paths resolve beside that config. Provider module is optional; direct shared provider is the default; an existing explicit legacy helper override still works. Do not register startup or schedules without deployment authorization.

## Optional Lifecycle / XATS connection check

After target-native Node dependencies and build are complete, copy `components/lifecycle/manager.example.json` and `components/lifecycle/xats-service.example.json` to your own private configuration directory. Replace every example path, name, team and port. Use an independent state directory and XATS database. Generate a private random XATS token file; provide the same value through `HARNESS_XATS_TOKEN` to the Manager. The URL must include the existing MCP endpoint `/mcp`, not just the server origin.

`appServerPath` must point to the actual installed official native-tools server described below. Run the Manager only inside a genuine Codex execution context supplying `CODEX_THREAD_ID` and `CODEX_APP_TOOLS_PIPE_PATH`; do not copy another chat's identity or pipe into config. `resume.installedConfig` is a private MCP JSON declaration with `mcpServers.solution_agent_harness.command`, `args` and `cwd`, pointing to your installed paired client launcher and selected control/profile. Keep `resume.contexts` empty for a connection-only check. The example does not register routes or submit work.

From the installed `components/lifecycle` directory, in your explicitly selected test environment:

```powershell
node --import tsx production-xats-service-20261008.ts <private-xats-config.json>
```

Keep that loopback service running and invoke this command from a genuine Codex execution context, with the private token available:

```powershell
node --import tsx manager-cli.ts <private-manager-config.json> --once
```

Inspect `<stateRoot>/status.json`: an active tick must have `state=running`, `appAvailability=online`, `transport=connected`, `resumeState=ready` and `pending=0`. The final single-tick status has `lifecycle=stopped` and `transport=disconnected`; it retains the preceding connection fields. `app_rebootstrap_required`, `offline` or `harness_reconnect_required` means the connection check failed. Stop the separately launched test XATS service with Ctrl+C when finished. This manual check installs no startup entry or schedule and does not prove native message delivery or paid report generation.

## Update and rollback without changing state format

1. Identify exact installed and loaded releases, workplace, session/binding ownership and active command IDs. Stop if identity or state ownership is unknown. A source checkout SHA is insufficient evidence of loaded identity.
2. Retain the supported old artifact and verify its hash. If unavailable, mark the update/rollback test NOT AVAILABLE. Do not substitute the extraction baseline and claim supported deployment compatibility.
3. Back up configuration and obtain an SQLite-consistent backup of state (use SQLite backup API, including WAL semantics). Retain original IDs, receipts/CAS/leases. Do not copy just a live main DB file or overwrite the live DB.
4. Stage the candidate in a new directory, install target-native dependencies and run checks there. Use the existing managed installer dry-run/hash-bound plan if a managed overlay is explicitly selected. Conflicts stop activation. Change only the explicitly selected binding under actual host authorization.
5. Reload the selected session, verify actual runtime identity and original command lookup. If submission was uncertain, lookup original ID; do not resend. Active commands keep their execution release.
6. Roll back by selecting the retained old code/binding only after confirming it can read the retained current state. Preserve configuration/state and receipts. Partial endpoint updates remain partial until both exact identities are verified. If backward-read is unverified, stop rollback; no destructive state conversion is included.

Local installer and synthetic-state tests are engineering evidence only. They cannot replace M1 new/old, M2 new/new, M3 old/new and M4 old/old on actual isolated Windows × Linux deployments.

## Extension owners

New Harness MCP tools belong to existing mcp_server.py and their canonical manifests/contracts; keep namespace and strict validation. Writer report schemas/prompts and remote business adapter stay in Writer. Provider transport/config/preset belongs to packages/report-provider; both Writer and Background are actual consumers. Add meaningful fixture coverage there and at the consumers. Background rules and queue remain its existing service owner; rules are project-local. Lifecycle and goal integration retain their current direct imports. XATS upstream is pinned, with its existing license/provenance; do not introduce a plugin bus or duplicate state stores.

Verification: `python scripts/release/provenance.py`; publication gate; relevant Python suite; Writer, Background and shared consumer Node tests; lifecycle/goal integration build/tests. Regenerate source-changes after reviewed authority changes with `python scripts/release/provenance.py --write`. Never edit historical source-map hashes.


## Original install artifact rehearsal

The original local-to-remote Worker install package is a valid local test input; a separate isolation package is unnecessary. Preferred identified Harness start: `harness-0.6-cdf4c9cbb9e48fb2`, original ZIP SHA256 `067da359bf8ee832fc03a0c4a81e8da8cf90a9fd58c6eb3da22d5b0d505f8d1d`, 320 verified payload files, paired Client manifest retained privately. It has source-root identity rather than a Git commit. This does not cover the entire Writer/Background/Lifecycle deployment or establish current loaded remote state.

Run `python scripts/release/rehearse_harness_update.py --old-archive <original-install.zip> --old-sha256 <verified-original-hash> --python <isolated-python> --workdir <checkout>/.verification/<new-rehearsal-directory>`. This explicit local operation installs in a new repo-private directory, generates a reviewed exact-hash forward update and reverse artifact, checks old/candidate/old identities, consistent synthetic SQLite backup, original command lookup and unchanged config/receipt hashes. Original package bytes remain immutable. Archived historical profiles remain inert to preserve identity hashes; the rehearsal never invokes a profile loader, SSH or credential-file reader. Results remain ignored; derived artifacts carrying historical private settings are never published. It is local Harness component evidence, not M4.

## Current isolated acceptance boundary

M1–M4 have passed real Windows/Linux Harness isolation checks for the current tested runtime, including clean archive installation, exact pairing, additive binding, original command recovery, artifact CAS and old/current/old rollback. Windows clean installation uses a new directory on the same host; M3/M4 use isolated configuration/state copies. The namespace method preserves the immutable old package's original paths without changing its identity; see [NAMESPACE_ACCEPTANCE.md](NAMESPACE_ACCEPTANCE.md). Production native App identity/read and Writer remote validation also passed; native sends and paid drafts were not invoked. Current evidence and remaining checks: [STATUS.md](STATUS.md), [WORKPLACE_ACCEPTANCE.md](WORKPLACE_ACCEPTANCE.md).

## Native Codex App entry

Lifecycle's private `appServerPath` refers to the genuine installed native-tools MCP server. On the tested Windows Codex package it is `<Codex-install>/app/resources/plugins/openai-bundled/plugins/codex-app-tools/server.mjs`; adjacent `.mcp.json` and `scripts/launch_codex_app_tools_mcp.cmd` declare official launch requirements. Read the installed package location (for example, `Get-AppxPackage *Codex*`) and verify the exact file. Layouts can change; do not copy an old machine's absolute path or ship the host server in Agent Relay.

NativeAppPort requires genuine current `CODEX_THREAD_ID` and host-supplied `CODEX_APP_TOOLS_PIPE_PATH`; it checks caller identity through `read_thread` before binding. A new App must supply a new trusted context. Do not discover another pipe, substitute a target thread as caller or use transport-owner locks. The separate `codex app-server` JSON-RPC interface is not this MCP endpoint. Host configuration/hooks remain private and outside this isolated installation.

Writer remote roots must be beneath its own deployment workspace (`report_writer.py` parents[2]). Set private `REPORT_WRITER_REMOTE_ALIAS`, `REPORT_WRITER_REMOTE_SCRIPT` and `REPORT_WRITER_REMOTE_ROOTS` accordingly. Install the remote script from the same source; run `remote_writer_status`, then `validate_remote_evidence_packet` with an allowed synthetic packet and verify SHA256. Validation sends no credential and calls no model. Harness command receipts do not prove this separate Writer route.

## Build output ownership and deployment order

| Output | Build command | Use and checks |
| --- | --- | --- |
| Worker / client / standalone Writer ZIPs | `python scripts/release/build_candidate.py` | `dist/p1-candidate/candidate.json` identifies exact artifact hashes and matched Harness identity. |
| Router helpers | `npm --prefix components/lifecycle run build` | `components/lifecycle/router-package-latest.json` points to the generated `packages/router-*` directory and manifest hash. It includes dependency licenses and source hashes. Keep it separate from Harness runtime; never overlay its helper-only Python namespace. |
| XATS native/service build | `node scripts/extraction/build-local.mjs` after target-native rebuild | `.verification/build/xats-service.mjs` and metafile; SQLite memory smoke is part of the command. The external upstream native module remains a target dependency. Build does not start a service. |
| Background | Source client package, `node tools/research-background/cli.mjs --config <private-config> status` | Uses its own vault/state/rules and shared Provider. Windows launcher remains a separate opt-in host entry; no startup registration is implicit. |
| Token Guard | Client source package's `plugins/research-token-guard/` | Existing five Python CLI entrypoints and skill; preserve their receipt/artifact ownership rather than introducing another MCP server. |

Install host prerequisites, then target dependencies. Configure private workplace identity and exact Harness pairing first. Configure XATS, Router state and the genuine native App entry only for the selected isolated instance; use absolute private configuration paths. Finally configure Writer roots/Provider and Background state. Check each entry before connecting its direct consumer. Keep all runtime state and credentials outside source and retain exact old artifacts before updating bindings. Generated Router scope still denotes a candidate requiring declared services; it does not automatically activate installed hooks.
