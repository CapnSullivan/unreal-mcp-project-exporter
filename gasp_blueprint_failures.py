#!/usr/bin/env python3
"""Offline error report for existing GASP_PROJECT_EXPORT blueprints/*.json. No Unreal / MCP calls."""
import argparse
import csv
import json
from pathlib import Path
from collections import Counter


def collect(output):
    folder=Path(output)/'blueprints'
    if not folder.is_dir():
        raise FileNotFoundError(f'Blueprint JSON directory does not exist: {folder}')
    records=[]
    problems=[]
    for path in sorted(folder.glob('*.json')):
        try:
            value=json.loads(path.read_text(encoding='utf-8'))
        except (OSError,ValueError) as exc:
            problems.append({'asset_path':path.name, 'status':'invalid_json', 'step':'parse', 'error':str(exc)})
            continue
        status=value.get('status','unknown')
        if status not in ('failed','partial','in_progress'):
            continue
        asset=value.get('asset_path',path.name)
        errors=value.get('errors',[])
        if not isinstance(errors,list):
            errors=[errors]
        for graph in value.get('graphs',[]):
            if isinstance(graph,dict):
                for err in graph.get('errors',[]):
                    if isinstance(err,dict):
                        errors.append({'step':'graph.'+str(err.get('step','unknown')),
                                       'error':err.get('error',str(err)),
                                       'graph':graph.get('graph')})
                    else:
                        errors.append({'step':'graph.unknown','error':str(err),
                                       'graph':graph.get('graph')})
        if not errors:
            problems.append({'asset_path':asset,'status':status,'step':'unknown','error':'No error details recorded'})
        for err in errors:
            if isinstance(err,dict):
                error=err.get('error',json.dumps(err,ensure_ascii=False,default=str))
                step=err.get('step','unknown')
            else:
                error=err
                step='unknown'
            problems.append({'asset_path':asset, 'status':status,
                             'step':str(step),'error':str(error)})
        records.append({'asset_path':asset,'status':status, 'error_count':len(errors)})
    return records,problems


def write_report(output, name='blueprint_failures_diagnostic.csv'):
    records,problems=collect(output)
    target=Path(output)/name
    with target.open('w',encoding='utf-8-sig',newline='') as file:
        writer=csv.DictWriter(file,fieldnames=['asset_path','status','step','error'])
        writer.writeheader()
        writer.writerows(problems)
    return target,records,problems


def main():
    ap=argparse.ArgumentParser(description='Read exported Blueprint failure JSON and make a local diagnostic CSV. No MCP requests.')
    ap.add_argument('--output',required=True,help='Full existing MyGameProject_GAS_Test_EXPORT directory')
    args=ap.parse_args()
    target,records,problems=write_report(args.output)
    print('[DIAGNOSTICS] Records with failed/partial/in_progress:',len(records))
    print('[DIAGNOSTICS] Errors:',len(problems))
    for (step, error),count in Counter((p['step'],p['error'][:150]) for p in problems).most_common(12):
        print(f'{count:4} | {step} | {error[:140]}')
    print('[CSV]',target)

if __name__=='__main__':
    main()
