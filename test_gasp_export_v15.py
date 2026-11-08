import importlib.util
import csv
import json
import tempfile
import unittest
from pathlib import Path

HERE=Path(__file__).parent
sp=importlib.util.spec_from_file_location('gasp_project_export_v15', HERE/'gasp_project_export_v15.py')
e=importlib.util.module_from_spec(sp)
sp.loader.exec_module(e)
sp=importlib.util.spec_from_file_location('gasp_blueprint_failures', HERE/'gasp_blueprint_failures.py')
r=importlib.util.module_from_spec(sp)
sp.loader.exec_module(r)

class MCPStub:
    def __init__(self, *, fails_on=None, load_alternative=False):
        self.calls=[]
        self.fails_on=fails_on or set()
        self.load_alternative=load_alternative
    def tool(self,group,name,args):
        self.calls.append((group,name,args))
        if (name in self.fails_on or (name=='get_node_infos' and len(args.get('nodes',[]))>1 and 'multi_node_fail' in self.fails_on)):
            raise RuntimeError('mock failure: '+name)
        if name=='get_parent':return {'refPath':'/Script/Engine.Actor'}
        if name in ('list_functions','list_events','list_variables','list_event_dispatchers'):return []
        if name=='get_default_object':return {'refPath':'CDO'}
        if name=='list_properties':return {'Name':{'type':'str'}}
        if name=='get_properties':return {'Name':'Value'}
        if name=='list_graphs':return [{'refPath':'Graph0'},{'refPath':'Graph1'}]
        if name=='find_nodes':return [{'refPath':args['graph']['refPath']+'_node0'}, {'refPath':args['graph']['refPath']+'_node1'}]
        if name=='get_node_infos':
            return [{'node':x,'input_pins':[], 'output_pins':[], 'type_id':'Mock'} for x in args['nodes']]
        if name=='read_graph_dsl':return f'(graph {args["graph"]["refPath"]})'
        if name=='load_asset':return {'refPath':'special ref'}
        raise AssertionError(name)

class Tests(unittest.TestCase):
    def test_normal_recovery(self):
        m=MCPStub()
        saved=[]
        row=e.export_blueprint_recovery(m,'/Game/BP_Test',properties=True,dsl=True,
                                        signature={'sha256':'aaa'}, checkpoint=lambda v:saved.append(json.loads(json.dumps(v))))
        self.assertEqual(row['status'],'complete')
        self.assertEqual(row['graph_count'],2)
        self.assertEqual(row['node_count'],4)
        self.assertTrue(all(g['checkpoint_complete'] for g in row['graphs']))
        self.assertGreaterEqual(len(saved),3)
        self.assertIn('properties',row['graphs'][0]['nodes'][0])
        self.assertEqual(row['graphs'][0]['dsl'],'(graph Graph0)')
        self.assertNotIn('get_components',[name for _,name,_ in m.calls])
    def test_failed_graph_read_captures_diagnostics(self):
        m=MCPStub(fails_on={'list_graphs'})
        row=e.export_blueprint_recovery(m,'/Game/BP_Test')
        self.assertEqual(row['status'],'failed')
        self.assertTrue(any(x['step']=='list_graphs' for x in row['errors']))
        self.assertTrue(any(x['step']=='load_asset_fallback' for x in row['errors']) is False)
    def test_special_blueprint_zero_graphs_is_not_complete(self):
        class Empty(MCPStub):
            def tool(self, group, name, args):
                if name=='list_graphs':return []
                return super().tool(group,name,args)
        row=e.export_blueprint_recovery(Empty(),'/Game/ABP',asset_class='AnimBlueprint')
        self.assertEqual(row['status'],'partial')
        self.assertEqual(row['graph_count'],0)
        self.assertEqual(row['errors'][-1]['step'],'graph_coverage')
    def test_chunk_bisects(self):
        m=MCPStub(fails_on={'multi_node_fail'})
        row=e.export_blueprint_recovery(m,'/Game/BP_Test',node_batch=16)
        self.assertEqual(row['status'],'complete')
        self.assertEqual(row['node_count'],4)
        self.assertGreaterEqual(len([x for x in m.calls if x[1]=='get_node_infos']),6)
    def test_metadata_error_is_partial_with_graphs(self):
        m=MCPStub(fails_on={'get_default_object'})
        row=e.export_blueprint_recovery(m,'/Game/BP_Test')
        self.assertEqual(row['status'],'partial')
        self.assertEqual(row['graph_count'],2)
    def test_checkpoint_graph_resumption(self):
        prev=None
        m=MCPStub()
        first=e.export_blueprint_recovery(m,'/Game/BP_Test',dsl=True,signature={'sha256':'x'})
        m2=MCPStub()
        row=e.export_blueprint_recovery(m2,'/Game/BP_Test',dsl=True,signature={'sha256':'x'},previous=first)
        self.assertEqual(row['status'],'complete')
        self.assertEqual(len([x for x in m2.calls if x[1]=='find_nodes']),0)
        m3=MCPStub()
        row=e.export_blueprint_recovery(m3,'/Game/BP_Test',dsl=False,signature={'sha256':'x'},previous=first)
        self.assertEqual(row['status'],'complete')
        self.assertEqual(len([x for x in m3.calls if x[1]=='find_nodes']),2)
    def test_retry_graphs_with_errors(self):
        failed_m=MCPStub(fails_on={'get_default_object','find_nodes'})
        first=e.export_blueprint_recovery(failed_m,'/Game/BP_Test',signature={'sha256':'x'})
        self.assertEqual(first['status'],'partial')
        recovered=MCPStub()
        second=e.export_blueprint_recovery(recovered,'/Game/BP_Test',signature={'sha256':'x'},previous=first)
        self.assertEqual(second['status'],'complete')
        self.assertEqual(len([c for c in recovered.calls if c[1]=='find_nodes']),2)
    def test_csv_offline(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder=Path(tmp)/'blueprints'
            folder.mkdir()
            (folder/'x.json').write_text(json.dumps({'status':'failed','asset_path':'/Game/X',
                'errors':[{'step':'export','error':'Blueprint invalid'}]}),encoding='utf8')
            (folder/'y.json').write_text(json.dumps({'status':'complete','asset_path':'/Game/Y'}),encoding='utf8')
            target, records, problems=r.write_report(tmp)
            self.assertTrue(target.is_file())
            self.assertEqual(len(records),1)
            self.assertEqual(problems[0]['error'],'Blueprint invalid')
            with target.open(encoding='utf-8-sig',newline='') as f:
                self.assertEqual(list(csv.DictReader(f))[0]['asset_path'],'/Game/X')
    def test_v14_registry_is_unchanged(self):
        v14=HERE/'gasp_project_export_v14.py'
        if not v14.exists():
            self.skipTest('v1.4 side-by-side source not present')
        src=v14.read_text(encoding='utf8')
        dst=(HERE/'gasp_project_export_v15.py').read_text(encoding='utf8')
        start='def export_registry_resilient('
        end='def export_blueprint('
        self.assertEqual(src.split(start,1)[1].split(end,1)[0],dst.split(start,1)[1].split(end,1)[0])

if __name__=='__main__':
    unittest.main(verbosity=2)
