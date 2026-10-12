import { basename } from 'node:path'

export interface DeriveDefaultTeamInput {
  team?: string
  project_dir?: string
}

export function deriveDefaultTeam(input: DeriveDefaultTeamInput): string {
  const explicitTeam = input.team?.trim()
  if (explicitTeam) return explicitTeam

  if (input.project_dir !== undefined) {
    const projectTeam = basename(input.project_dir).trim().toLowerCase()
    if (projectTeam) return projectTeam
  }

  return 'default'
}
