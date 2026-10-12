import { defineConfig } from 'tsup'
export default defineConfig({
  entry: {
    cli: 'src/cli.ts',
    'channel-cli': 'plugins/cross-agent-teams-channel/src/cli.ts'
  },
  format: ['esm'],
  target: 'node20',
  clean: true,
  sourcemap: true,
  dts: true
})
