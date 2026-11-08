import contextlib
import hashlib
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

HERE=Path(__file__).parent
spec=importlib.util.spec_from_file_location('gasp_exporter_integ', HERE/'gasp_project_export_v15.py')
e=importlib.util.module_from_spec(spec)
spec.loader.exec_module(e)

class Integration(unittest.TestCase):
    def test_only_failed_phase_keeps_registry_and_successes_intact(self):
        with tempfile.TemporaryDirectory() as t:
            t=Path(t)
            root=t/'Project'
            (root/'Content').mkdir(parents=True)
            (root/'Project.uproject').write_text('{}',encoding='utf8')
            out=t/'ExistingExport'
            (out/'registry').mkdir(parents=True)
            (out/'blueprints').mkdir(parents=True)
            paths=[f'/Game/BP_{i}' for i in range(4)]
            existing={}
            for i,p in enumerate(paths):
                path=(root/'Content'/f'BP_{i}.uasset')
                path.write_bytes(f'bp-{i}'.encode())
                signature={'sha256':e.digest(path),'size':path.stat().st_size,'mtime_ns':path.stat().st_mtime_ns}
                # Ignore source_signature schema here: cached_ok only for previously complete files;
                # successful blueprints are explicitly excluded by --only-failed-blueprints.
                ident=hashlib.sha1(p.encode()).hexdigest()[:16]
                reg=out/'registry'/f'{ident}.json'
                reg.write_text(json.dumps({'asset_path':p,'status':'complete','data':{'get_asset_class':'Blueprint'},'source_signature':signature}),encoding='utf8')
                bp=out/'blueprints'/f'{ident}.json'
                bp.write_text(json.dumps({'asset_path':p,'status':'complete' if i < 2 else 'failed',
                    'errors':[] if i < 2 else [{'step':'export','error': 'previous '+str(i)}],
                    'source_signature':signature,
                    'includes_properties': True, 'includes_dsl': True}),encoding='utf8')
                existing[p]=(reg.read_bytes(),bp.read_bytes(),path.read_bytes())
            class Stub:
                def __init__(self,*args,**kwargs): pass
                def tool(self, group, name, args):
                    if name=='find_assets':return paths
                    if name=='is_dirty':return False
                    if name=='get_parent':return {'refPath':'Actor'}
                    if name in ('list_events','list_variables','list_event_dispatchers','list_functions','list_graphs'):return []
                    if name=='get_default_object':return {'refPath':'CDO'}
                    if name=='list_properties':return {}
                    if name=='get_properties':return {}
                    raise AssertionError(f'unexpected MCP request {name}')
            old_argv=sys.argv
            args=['gasp_project_export_v15.py','--project',str(root),'--output',str(out),
                  '--phase','blueprints','--properties','--dsl','--only-failed-blueprints','--zip']
            try:
                with patch.object(e,'MCP',Stub):
                    sys.argv=args
                    e.main()
            finally:
                sys.argv=old_argv
            for i,p in enumerate(paths):
                path=(root/'Content'/f'BP_{i}.uasset')
                ident=hashlib.sha1(p.encode()).hexdigest()[:16]
                reg=out/'registry'/f'{ident}.json'
                bp=out/'blueprints'/f'{ident}.json'
                self.assertEqual(reg.read_bytes(),existing[p][0])
                self.assertEqual(path.read_bytes(),existing[p][2])
                if i<2:
                    self.assertEqual(bp.read_bytes(),existing[p][1])
                else:
                    self.assertEqual(json.loads(bp.read_text())['status'],'complete')
            self.assertTrue((out/'blueprint_failures_before_v15.csv').exists())
            self.assertTrue(out.with_suffix('.zip').exists())
            self.assertEqual(json.loads((out/'export_summary.json').read_text())['blueprints']['complete'],4)

if __name__=='__main__': unittest.main()
