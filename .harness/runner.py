#!/usr/bin/env python3
"""Project Harness runner. Reads commands; never installs dependencies or edits code."""
from __future__ import annotations
import argparse, json, subprocess, sys, time
from pathlib import Path
import yaml
ROOT=Path(__file__).resolve().parents[1]
MANIFEST=ROOT/'.harness/manifest.yaml'

def main():
 p=argparse.ArgumentParser(); p.add_argument('profile',choices=['fast','medium','slow']); p.add_argument('--output',required=True); a=p.parse_args()
 if not MANIFEST.exists(): print('NO_OP: missing manifest'); return 0
 m=yaml.safe_load(MANIFEST.read_text()); names=m['profiles'].get(a.profile,[])
 if not names:
  result={'profile':a.profile,'command':'NOT_APPLICABLE','exit_code':0,'duration_ms':0,'status':'NOT_APPLICABLE','artifact':None}
  Path(a.output).write_text(json.dumps(result,indent=2)); return 0
 timeout=m['timeouts_seconds'][a.profile]; results=[]
 for name in names:
  cmd=m['commands'].get(name)
  if not cmd:
   results.append({'profile':a.profile,'command':str(name),'exit_code':-1,'duration_ms':0,'status':'BLOCKED','artifact':None}); continue
  if not isinstance(cmd, list):
   results.append({'profile':a.profile,'command':str(cmd),'exit_code':-1,'duration_ms':0,'status':'BLOCKED','artifact':None}); continue
  start=time.monotonic(); log=Path(a.output).with_suffix('.'+name+'.log')
  try:
   r=subprocess.run(cmd,cwd=ROOT,text=True,capture_output=True,timeout=timeout)
   log.write_text(r.stdout+'\n'+r.stderr)
   results.append({'profile':a.profile,'command':cmd,'exit_code':r.returncode,'duration_ms':int((time.monotonic()-start)*1000),'status':'PASS' if r.returncode==0 else 'FAIL','artifact':str(log)})
  except subprocess.TimeoutExpired as e:
   def _text(value):
    if value is None: return ''
    return value.decode(errors='replace') if isinstance(value, bytes) else str(value)
   log.write_text(_text(e.stdout)+_text(e.stderr))
   results.append({'profile':a.profile,'command':cmd,'exit_code':124,'duration_ms':int((time.monotonic()-start)*1000),'status':'FAIL','artifact':str(log)})
 Path(a.output).write_text(json.dumps(results[0] if len(results)==1 else results,indent=2,ensure_ascii=False))
 for x in results: print(x['status'],x['command'],'rc='+str(x['exit_code']),'ms='+str(x['duration_ms']))
 return 0 if all(x['status'] in ('PASS','NOT_APPLICABLE') for x in results) else 1
if __name__=='__main__': sys.exit(main())
