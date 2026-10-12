# Extraction build environment

Verified locally: Windows x64, Python 3.13.12, Node 24.19.0, esbuild 0.27.7, TypeScript loader tsx 4.21.0, better-sqlite3 12.9.0.

`requirements-extraction-windows.txt` records the exact Python distribution closure; metadata/file hashes are recorded separately. Third-party files were reconstructed offline, without installing into or reusing the original venv. The ordinary venv still relies on a base Python installation for its standard library. Venvs/native binaries are platform-local generated environments, not portable source.

The writer keeps its original pnpm lock. XATS upstream pnpm lock is retained as upstream provenance; its copied package.json direct runtime dependencies were pinned to the observed installed versions. It is not yet a regenerated authoritative lock for the extraction tree. Runtime package IDs/versions and dependency edges are in node-dependencies.json. Lifecycle's metadata declares its formerly implicit SDK/esbuild/tsx dependencies. Future fresh installs must resolve/verify component locks and native modules; this was not claimed tested online.

The source project does not require an OpenAI/Bailian key to build or run synthetic checks. No credential file is provided. Live operations require explicit external project profiles and later deployment authorization.
