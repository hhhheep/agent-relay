export interface ContractDiff {
  added_fields: Array<{ path: string; type_summary: string }>
  removed_fields: Array<{ path: string; type_summary: string }>
  changed_fields: Array<{
    path: string
    from: { type?: string; required?: boolean; enum?: unknown[]; raw: unknown }
    to:   { type?: string; required?: boolean; enum?: unknown[]; raw: unknown }
  }>
  breaking: boolean
}

type Schema = Record<string, unknown> & {
  type?: string
  properties?: Record<string, Schema>
  required?: string[]
  enum?: unknown[]
}

function typeSummary(s: Schema | undefined): string {
  if (!s) return 'unknown'
  if (typeof s.type === 'string') return s.type
  return 'unknown'
}

function isRequired(parent: Schema, key: string): boolean {
  return Array.isArray(parent.required) && parent.required.includes(key)
}

function walk(
  fromParent: Schema, toParent: Schema, basePath: string,
  added: ContractDiff['added_fields'], removed: ContractDiff['removed_fields'], changed: ContractDiff['changed_fields']
): void {
  const fp = fromParent.properties ?? {}
  const tp = toParent.properties ?? {}
  const keys = new Set<string>([...Object.keys(fp), ...Object.keys(tp)])
  for (const key of keys) {
    const path = `${basePath}/properties/${key}`
    const fromChild = fp[key]
    const toChild = tp[key]
    if (fromChild && !toChild) {
      removed.push({ path, type_summary: typeSummary(fromChild) })
      continue
    }
    if (!fromChild && toChild) {
      added.push({ path, type_summary: typeSummary(toChild) })
      continue
    }
    if (fromChild && toChild) {
      const fromType = typeof fromChild.type === 'string' ? fromChild.type : undefined
      const toType = typeof toChild.type === 'string' ? toChild.type : undefined
      const fromReq = isRequired(fromParent, key)
      const toReq = isRequired(toParent, key)
      const typeDiff = fromType !== toType
      const reqDiff = fromReq !== toReq
      if (typeDiff || reqDiff) {
        changed.push({
          path,
          from: { type: fromType, required: fromReq, enum: fromChild.enum, raw: fromChild },
          to:   { type: toType,   required: toReq,   enum: toChild.enum,   raw: toChild }
        })
      }
      if (toChild.type === 'object' || fromChild.type === 'object') {
        walk(fromChild, toChild, path, added, removed, changed)
      }
    }
  }
}

export function diffSchema(from: Schema, to: Schema): ContractDiff {
  const added: ContractDiff['added_fields'] = []
  const removed: ContractDiff['removed_fields'] = []
  const changed: ContractDiff['changed_fields'] = []
  walk(from, to, '', added, removed, changed)
  const breaking =
    removed.length > 0 ||
    changed.some(c => c.from.required === false && c.to.required === true) ||
    changed.some(c => !!c.from.type && !!c.to.type && c.from.type !== c.to.type)
  return { added_fields: added, removed_fields: removed, changed_fields: changed, breaking }
}
