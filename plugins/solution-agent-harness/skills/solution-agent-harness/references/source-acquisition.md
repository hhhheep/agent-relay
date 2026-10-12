# Source acquisition

Use this reference only when the active taskbook, lock, execution spec, or user
names a repository or release needed by the work.

Resolve the URL, requested ref or release, required paths, destination,
submodules, and Git LFS needs from the materials already supplied. Do not ask the
user to repeat information that is already explicit.

Check the established source roots before downloading. If a checkout already
exists, verify its origin, resolved commit, dirty state, and required files.
Reuse a suitable checkout without mutation. Do not reset, clean, or update a
dirty checkout merely to make it look standard.

If the source is absent and the source plus destination are unambiguous, acquire
it and perform routine setup through a Harness-owned child terminal or an existing project
workflow. A user-provided public GitHub URL authorizes ordinary inspection,
clone or fetch, and task-required setup. Use a stage only when acquisition needs
a reusable receipt.

Record whether the source was reused or acquired, its original URL, resolved
version, destination, and integrity evidence. Ask for help only for a concrete
blocker such as authentication, gated terms, an ambiguous destination, an
incompatible existing checkout, unavailable required assets, or insufficient
storage.
