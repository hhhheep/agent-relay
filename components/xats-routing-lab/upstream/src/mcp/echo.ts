import { z } from 'zod'

export const echoSchema = { msg: z.string() }

export async function echoHandler(args: { msg: string }): Promise<{ content: Array<{ type: 'text'; text: string }> }> {
  const out = { msg: args.msg, echoed_at: new Date().toISOString() }
  return { content: [{ type: 'text', text: JSON.stringify(out) }] }
}
