// Test-only real CLI producer -> production hook stdout contract. No App calls.
import {writeFileSync} from 'node:fs';
import {contextRolloverHookOutput} from '../../components/lifecycle/context-rollover.ts';
import {rolloverChatId} from '../../components/lifecycle/controller-authority.ts';
let input='';for await(const chunk of process.stdin){input+=chunk;if(Buffer.byteLength(input)>16384)throw Error('oversize hook event');}
const event=JSON.parse(input);
if(event.hook_event_name!=='PreCompact'||event.trigger!=='auto'||!rolloverChatId(event.session_id)||!rolloverChatId(event.turn_id))throw Error('invalid actual product event');
writeFileSync(process.argv[2],JSON.stringify(event));
console.log(JSON.stringify(contextRolloverHookOutput('requested')));
