# Extracted project scope
This project is an independent source baseline, not an active installation.
Keep public solution_agent/CLI/MCP identities and behavior. No Nx/package restructuring yet.
Never modify original research folders, installed plugins, hooks, credentials, live DBs or scheduled tasks.
Use only this project's isolated dependencies and synthetic fixtures for checks.
No GitHub upload, plugin registration or service deployment without a later task requesting it.
Source extraction provenance: docs/extraction/source-map.json. Exact local origins are excluded in .local-extraction/.
Architecture and update plans under docs/planning are future work, not completed capabilities.

Publication privacy: never commit or package credentials, per-machine account/host/path bindings, research content, live state or raw local receipts. Use environment variable names, relative paths and synthetic fixtures. Run scripts/extraction/check-publication.py before staging or packaging; package from Git candidate files only. Public Python/MCP/plugin identities are preserved when renaming the project.
