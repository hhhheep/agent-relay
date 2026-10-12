# Example profile routing

Use the local control plane at `C:\Users\example-user\Desktop\ai-tools`. Read
`remote-workplaces\connection-policy.yaml`, then only the profile selected by
the user or task.

- Example 91 / FAN: `remote-workplaces\example_worker_a_fan.yaml`, wrapper
  `tools\example-worker-a.cmd`, SSH alias `EXAMPLE_WORKER_A`.
- Example 93: `remote-workplaces\example_worker_c.yaml`, wrapper `tools\example-worker-c.cmd`, SSH
  alias `EXAMPLE_WORKER_C`. Lifecycle work remains blocked while its declared remote
  root is a placeholder; identity and GPU probes are still valid.

Use the profile terminal for normal remote work and the stage lifecycle for
stable receipt-producing work. The wrapper is for profile information,
bootstrap, and recovery. For profile information, read-only diagnosis, or
authorized maintenance, an unavailable surface may be replaced by another
surface bound to the same declared profile. Worker submission and recovery stay
on the managed lifecycle: preserve the original command ID, establish the launch
state, and do not switch to SSH dispatch or a new work unit to bypass failure.
Do not invent an unmanaged host or ask for key contents.

Routine wrapper probes are `info`, `identity`, and `gpu`. They do not authorize
work outside the user's task.
