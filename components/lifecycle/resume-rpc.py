
# Independent extraction: resolve the sole project Python source, never a research checkout.
import sys as _extraction_sys
from pathlib import Path as _ExtractionPath
_extraction_sys.path.insert(0, str(_ExtractionPath(__file__).resolve().parents[2]))

"""Durable continuation boundary; transport submission remains in the manager."""
import json
import sys
from solution_agent.harness.help_resume import ResumeLedger


def main():
    request=json.loads(sys.stdin.buffer.read(1024*1024+1))
    ledger=ResumeLedger(request['ledger'])
    try:
        action=request['action']
        if action=='plan':
            result={'id':ledger.plan(request['binding'],request['question'],**request['context'])}
        elif action=='inspect':result=ledger.inspect(request['id'])
        elif action=='claim':result=ledger.claim(request['id'],request['receipt'])
        elif action=='reconcile':result=ledger.reconcile(request['id'],request['receipt'])
        else:raise ValueError('invalid continuation action')
        print(json.dumps(result,ensure_ascii=True))
    finally:ledger.close()


if __name__=='__main__':main()
