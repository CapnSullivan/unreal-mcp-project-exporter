#!/usr/bin/env python3
"""Unreal MCP project inventory + resumable Blueprint exporter.
Derived from the user's autonomous MCP batch probe architecture. Read-only to game assets.
"""
import argparse
import hashlib
import json
import re
import sys
import time
import zipfile
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import requests

ASSET = 'editor_toolset.toolsets.asset.AssetTools'
BP = 'editor_toolset.toolsets.blueprint.BlueprintTools'
OBJ = 'editor_toolset.toolsets.object.ObjectTools'
PROG = 'editor_toolset.toolsets.programmatic.ProgrammaticToolset'


def utc():
    return datetime.now(timezone.utc).isoformat()


def dump(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + '.tmp')
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str), encoding='utf-8')
    temp.replace(path)


def digest(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def decode_json_string(value):
    """Decode JSON carried as a STRING, without changing typed arrays/objects.

    Tool returnValue may be a list of paths, a dict, a bool, or a text value.
    A list of asset paths is NOT a list of MCP content blocks.
    """
    if not isinstance(value, str):
        return value
    if value.lstrip().startswith(('{', '[')):
        try:
            return json.loads(value)
        except ValueError:
            pass
    return value


def extract_tool_envelope(result, name):
    """Unwrap a tools/call result, preserving typed returnValue values.

    Accept both MCP 2025-11-25 text content and structuredContent.
    Do not mistake lists from Unreal AssetTools for MCP content blocks.
    """
    if not isinstance(result, dict):
        raise RuntimeError(f'{name}: MCP tool result is not an object: {type(result).__name__}')
    if result.get('isError'):
        raise RuntimeError(f'{name}: MCP tool error: {str(result.get("content", result))[:1200]}')

    envelope = result.get('structuredContent')
    if not isinstance(envelope, dict) or 'returnValue' not in envelope:
        blocks = result.get('content')
        if not isinstance(blocks, list):
            raise RuntimeError(f'{name}: missing MCP content and structuredContent; keys={list(result)}')
        texts = [block.get('text') for block in blocks
                 if isinstance(block, dict) and block.get('type') == 'text'
                 and isinstance(block.get('text'), str)]
        if not texts:
            raise RuntimeError(f'{name}: no textual or structured result; '
                               f'content_types={[b.get("type") if isinstance(b, dict) else type(b).__name__ for b in blocks]}, '
                               f'keys={list(result)}')
        try:
            envelope = json.loads(texts[0])
        except ValueError as exc:
            raise RuntimeError(f'{name}: MCP tool returned non-JSON text: {texts[0][:450]!r}') from exc

    if not isinstance(envelope, dict):
        raise RuntimeError(f'{name}: malformed tool envelope: {str(envelope)[:450]}')
    if envelope.get('isError') or 'error' in envelope:
        raise RuntimeError(f'{name}: tool reported error: {str(envelope)[:1200]}')
    if 'returnValue' not in envelope:
        raise RuntimeError(f'{name}: tool response missing returnValue: {str(envelope)[:750]}')

    return decode_json_string(envelope['returnValue'])


class MCP:
    def __init__(self, url, timeout=120):
        self.url = url
        self.timeout = timeout
        self.session = requests.Session()
        self.headers = {'Accept': 'application/json, text/event-stream', 'Content-Type': 'application/json'}
        self.id = 0
        response = self.rpc('initialize', {'protocolVersion':'2025-11-25', 'capabilities':{},
                                           'clientInfo':{'name':'gasp-project-export','version':'1.5'}})
        print('[MCP] Protocol:', response.get('result', {}).get('protocolVersion'), flush=True)
        self.rpc('notifications/initialized', {}, notify=True)

    def rpc(self, method, params, notify=False):
        data = {'jsonrpc':'2.0', 'method':method, 'params':params}
        if not notify:
            self.id += 1
            data['id'] = self.id
        r = self.session.post(self.url, json=data, headers=self.headers, timeout=self.timeout)
        r.raise_for_status()
        if r.headers.get('Mcp-Session-Id'):
            self.headers['Mcp-Session-Id'] = r.headers['Mcp-Session-Id']
        if notify:
            return {}
        if 'text/event-stream' in r.headers.get('Content-Type',''):
            parts = re.findall(r'(?m)^data:\s*(.+)$', r.text)
            if not parts:
                raise RuntimeError('Empty MCP event stream')
            result = json.loads(parts[-1])
        else:
            result = r.json()
        if result.get('error'):
            raise RuntimeError(f'MCP RPC {method}: {result["error"]}')
        return result

    def tool(self, group, name, args):
        response = self.rpc('tools/call', {'name':'call_tool','arguments':{
            'toolset_name':group,'tool_name':name,'arguments':args}})
        if 'result' not in response:
            raise RuntimeError(f'{name}: tools/call did not return result; keys={list(response)}')
        return extract_tool_envelope(response['result'], name)

    def program(self, script):
        return self.tool(PROG, 'execute_tool_script', {'script':script})


def inventory_files(root, output):
    root = Path(root)
    extension = {'.uasset','.umap'}
    assets = []
    for base in (root / 'Content',):
        if base.exists():
            for f in base.rglob('*'):
                if f.is_file() and f.suffix.lower() in extension:
                    rel = f.relative_to(base).with_suffix('').as_posix()
                    assets.append({'asset_path':'/Game/'+rel,'file':str(f), 'bytes':f.stat().st_size,
                                   'mtime_ns':f.stat().st_mtime_ns})
    dump(output/'filesystem_assets.json', {'project':str(root),'records':assets})
    files = []
    for base in (root / 'Source', root / 'Config', root / 'Plugins'):
        if base.is_dir():
            for f in base.rglob('*'):
                if f.is_file() and f.suffix.lower() in {'.h','.hpp','.cpp','.cs','.ini'}:
                    files.append({'path':f.relative_to(root).as_posix(),'sha256':digest(f),
                                  'content':f.read_text(encoding='utf-8-sig', errors='replace')})
    for f in list(root.glob('*.uproject')) + list((root/'Plugins').rglob('*.uplugin')) if (root/'Plugins').is_dir() else list(root.glob('*.uproject')):
        files.append({'path':f.relative_to(root).as_posix(),'sha256':digest(f),
                      'content':f.read_text(encoding='utf-8-sig', errors='replace')})
    dump(output/'source_config.json', {'records':files})
    return assets


def local_asset(root, path):
    if not path.startswith('/Game/'):
        return None
    base = root/'Content'/path[len('/Game/'):]
    for ext in ('.uasset','.umap'):
        f = Path(str(base)+ext)
        if f.is_file():
            return f
    return None


def check_project(mcp, root, paths):
    # MCP does not expose actual .uproject. Cross-check 3 filesystem path correspondences,
    # but do NOT pretend this proves editor identity when two projects share the same asset names.
    sample = [p for p in paths if p.startswith('/Game/')][:40]
    matches = [p for p in sample if local_asset(root, p)]
    if not matches:
        raise RuntimeError('MCP /Game asset paths do not match local Content. Wrong project/editor?')
    print(f'[Identity] {len(matches)}/{len(sample)} sampled MCP /Game paths exist on disk. '
          'This is a heuristic, NOT proof of editor identity.', flush=True)
    if len(sample)>=3 and len(matches)<3:
        raise RuntimeError('Too few local MCP asset matches; cannot safely continue')
    return {'sampled':len(sample),'matched':len(matches),'verified':False,
            'warning':'MCP does not expose actual opened .uproject; only path overlap was checked'}


class AssetFileResolver:
    """Resolve /Game and plugin package paths to existing .uasset/.umap files.

    For plugin content, ONLY the actual matching local file can authorize
    reusing an old export. Unknown mounts are deliberately never cached.
    """

    def __init__(self, project_root, engine_root=None):
        self.root = Path(project_root).resolve()
        self.engine_root = Path(engine_root).resolve() if engine_root else None
        self.mounts = {}
        self.hash_cache = {}
        self._scan_descriptors(self.root / 'Plugins', 'project')
        if self.engine_root is not None:
            # Accept either UE_5.x/ or UE_5.x/Engine/ as --engine-root.
            candidates = [self.engine_root / 'Engine' / 'Plugins', self.engine_root / 'Plugins']
            valid = [p for p in candidates if p.is_dir()]
            if not valid:
                raise ValueError(f'No Engine/Plugins or Plugins directory under --engine-root={self.engine_root}')
            self._scan_descriptors(valid[0], 'engine')

    def _scan_descriptors(self, plugin_folder, source):
        if not plugin_folder.is_dir():
            return
        for descriptor in plugin_folder.rglob('*.uplugin'):
            content = descriptor.parent / 'Content'
            if content.is_dir():
                self.mounts.setdefault(descriptor.stem.casefold(), []).append({
                    'content': content, 'descriptor': descriptor, 'source': source})

    def resolve(self, asset_path):
        if asset_path.startswith('/Game/'):
            return local_asset(self.root, asset_path)
        parts = asset_path.strip('/').split('/')
        if len(parts) < 2 or not parts[0] or not parts[1]:
            return None
        entries = self.mounts.get(parts[0].casefold(), [])
        relative = Path(*parts[1:])
        matches = []
        for entry in entries:
            base = entry['content'] / relative
            for ext in ('.uasset', '.umap'):
                candidate = Path(str(base) + ext)
                if candidate.is_file():
                    matches.append(candidate)
        # A duplicated mount/path is ambiguous; do not guess which plugin UE mounted.
        return matches[0] if len(matches) == 1 else None

    def signature(self, asset_path):
        path = self.resolve(asset_path)
        if path is None:
            return None
        stat = path.stat()
        key = (str(path), stat.st_size, stat.st_mtime_ns)
        sha = self.hash_cache.get(key)
        if sha is None:
            sha = digest(path)
            self.hash_cache[key] = sha
        return {'size': stat.st_size, 'mtime_ns': stat.st_mtime_ns, 'sha256': sha}

    def details(self, asset_path):
        path = self.resolve(asset_path)
        if path is None:
            return {'asset_path': asset_path, 'resolved': False}
        return {'asset_path': asset_path, 'resolved': True,
                'physical_file': str(path), 'sha256': self.signature(asset_path)['sha256']}


def local_signature(root, path):
    """Backward compatible /Game-only helper; main uses AssetFileResolver."""
    f = local_asset(root, path)
    if f is None:
        return None
    return {'size': f.stat().st_size, 'mtime_ns': f.stat().st_mtime_ns, 'sha256': digest(f)}


def engine_root_from_environment():
    """Use configured engine path; on Windows infer it from the running editor exe.

    Multiple different running engine installations are ambiguous: fail closed.
    """
    explicit = os.environ.get('UE_ENGINE_ROOT') or os.environ.get('UNREAL_ENGINE_ROOT')
    if explicit:
        return explicit
    if os.name != 'nt':
        return None
    try:
        query = ('Get-Process UnrealEditor -ErrorAction SilentlyContinue | '
                 'Select-Object -ExpandProperty Path')
        run = subprocess.run(['powershell', '-NoProfile', '-NonInteractive', '-Command', query],
                             capture_output=True, text=True, timeout=12, check=False)
        installs = set()
        for line in run.stdout.splitlines():
            exe = Path(line.strip())
            if (exe.name.lower() == 'unrealeditor.exe'
                and exe.parent.name.lower() == 'win64'
                and exe.parent.parent.name.lower() == 'binaries'
                and exe.parent.parent.parent.name.lower() == 'engine'):
                installs.add(str(exe.parent.parent.parent.parent))
        if len(installs) == 1:
            engine = next(iter(installs))
            print('[Plugins] Detected UE engine from running UnrealEditor:', engine, flush=True)
            return engine
        if len(installs) > 1:
            print('[Plugins] Multiple running UE installations; specify --engine-root explicitly.', flush=True)
    except (OSError, subprocess.SubprocessError):
        pass
    return None


def plugin_resolution_report(resolver, assets):
    plugins = [p for p in assets if not p.startswith('/Game/')]
    unresolved = [p for p in plugins if resolver.resolve(p) is None]
    mounts = {}
    for name, entries in sorted(resolver.mounts.items()):
        mounts[name] = [{'descriptor': str(e['descriptor']), 'origin': e['source']}
                        for e in entries]
    return {'plugin_assets': len(plugins), 'resolved': len(plugins)-len(unresolved),
            'unresolved': len(unresolved), 'unresolved_paths': unresolved,
            'engine_root': str(resolver.engine_root) if resolver.engine_root else None,
            'mounts': mounts,
            'note': 'Only resolved plugin files can be cached with a verified SHA-256. '
                    'Unresolved plugin assets are re-exported on every run.'}


def cached_ok(doc, signature, *, dsl=None, properties=None):
    if not doc or doc.get('status') != 'complete' or doc.get('source_signature') != signature:
        return False
    if dsl is not None and doc.get('includes_dsl') != dsl:
        return False
    if properties is not None and doc.get('includes_properties') != properties:
        return False
    return True


def read_json(path):
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None


def asset_reference(path):
    return {'refPath': path + '.' + path.rsplit('/', 1)[-1]}


EXTERNAL_PACKAGE_SEGMENTS = frozenset({'__ExternalActors__', '__ExternalObjects__'})


def is_external_package(asset_path):
    """Unreal OFPA packages, not ordinary assets loadable through AssetTools.

    These packages still belong to the project. Record them separately rather
    than concealing them or attempting a misleading asset-level MCP export.
    """
    if not isinstance(asset_path, str) or not asset_path.startswith('/'):
        return False
    return any(part in EXTERNAL_PACKAGE_SEGMENTS
               for part in asset_path.strip('/').split('/')[1:])


def external_package_report(paths, resolver):
    records = []
    for path in paths:
        physical = resolver.resolve(path)
        # Avoid hashing the entire map content on every metadata refresh;
        # filesystem_assets.json already records size+mtime for /Game files.
        if physical is None:
            records.append({'asset_path': path, 'physical_file': None,
                            'status': 'not_directly_queryable_via_AssetTools',
                            'file_found': False})
        else:
            st = physical.stat()
            records.append({'asset_path': path, 'physical_file': str(physical),
                            'size': st.st_size, 'mtime_ns': st.st_mtime_ns,
                            'status': 'not_directly_queryable_via_AssetTools',
                            'file_found': True})
    return {
        'count': len(records),
        'file_found': sum(r['file_found'] for r in records),
        'reason': ('Unreal OFPA external actor/object package paths; '
                   'AssetTools methods may reject them with Asset does not exist. '
                   'They remain part of the level; NOT deleted or treated as audited Blueprint graphs.'),
        'records': records,
    }


def execute_batch(mcp, items, output, phase):
    # No import unreal. JSON is embedded as a literal in the MCP sandbox.
    operations = {'registry':['get_asset_class','get_dependencies','get_referencers','get_asset_tags']}
    assert phase in operations
    script = '''import json

def run():
    rows=[]
    for path in ITEMS:
        row={"asset_path":path,"errors":{}}
        for name in NAMES:
            try:
                response=execute_tool("editor_toolset.toolsets.asset.AssetTools."+name,json.dumps({"asset_path":path}))
                if "returnValue" not in response:
                    raise RuntimeError(str(response)[:500])
                row[name]=response["returnValue"]
            except Exception as exc:
                row["errors"][name]=str(exc)
        rows.append(row)
    return {"records":rows}
'''.replace('ITEMS',json.dumps(items,ensure_ascii=False)).replace('NAMES',json.dumps(operations[phase]))
    result = mcp.program(script)
    if not isinstance(result, dict) or not isinstance(result.get('records'),list):
        raise RuntimeError(f'Malformed MCP {phase} batch response: {str(result)[:300]}')
    received = [row.get('asset_path') for row in result['records']]
    if len(received) != len(items) or set(received) != set(items):
        raise RuntimeError('MCP registry batch returned missing, duplicated or extra assets')
    for row in result['records']:
        p=row.get('asset_path')
        if p not in items:
            raise RuntimeError('MCP returned unexpected asset: '+str(p))
        f=output/'registry'/ (hashlib.sha1(p.encode()).hexdigest()[:16]+'.json')
        dump(f,{'asset_path':p,'status':'complete' if not row.get('errors') else 'partial',
                'exported_utc':utc(), 'data':row})
    return result['records']


def registry_result_path(output, path):
    return output / 'registry' / (hashlib.sha1(path.encode()).hexdigest()[:16] + '.json')


def export_registry_resilient(mcp, items, output):
    """Try the original batch; bisect only known asset-specific MCP failures.

    Never hide transport/RPC failures by labelling thousands of items as bad.
    Persist completed records at their stable v1.3 JSON paths for resumption.
    Return number of asset-specific failed items in this batch.
    """
    if not items:
        return 0
    paths = [path for path, _signature in items]
    try:
        execute_batch(mcp, paths, output, 'registry')
    except requests.RequestException:
        raise  # Network/server outage is not an asset failure.
    except RuntimeError as exc:
        detail = str(exc)
        # Non-asset-specific RPC/protocol failures must still stop the export.
        if ('Asset does not exist' not in detail
                and 'asset does not exist' not in detail.lower()
                and 'Failed to load asset' not in detail):
            raise
        if len(items) > 1:
            midpoint = len(items) // 2
            print(f'[Registry] isolating asset-specific failure within {len(items)}-item batch',
                  flush=True)
            return (export_registry_resilient(mcp, items[:midpoint], output)
                    + export_registry_resilient(mcp, items[midpoint:], output))
        path, signature = items[0]
        # This is a failed extraction, not a valid cached snapshot.
        dump(registry_result_path(output, path), {
            'asset_path': path, 'status': 'failed', 'source_signature': signature,
            'exported_utc': utc(),
            'data': {'asset_path': path, 'errors': {'batch': detail}},
        })
        print(f'[Registry] failed asset {path}: {detail[:230]}', flush=True)
        return 1
    for path, signature in items:
        f = registry_result_path(output, path)
        data = read_json(f)
        if not isinstance(data, dict) or data.get('asset_path') != path:
            raise RuntimeError(f'Registry result missing after batch: {path}')
        data['source_signature'] = signature
        dump(f, data)
    return 0


def export_blueprint(mcp, path, properties=False, dsl=False):
    obj = {'refPath':path+'.'+path.rsplit('/',1)[-1]}
    # The sandbox runs one job per Blueprint and returns its data; no asset saving.
    script = '''import json

def call(group,name,arguments):
    r=execute_tool(group+"."+name,json.dumps(arguments))
    if "returnValue" not in r:
        raise RuntimeError(str(r)[:300])
    return r["returnValue"]

def run():
    bp=BLUEPRINT
    row={"asset_path":ASSET,"metadata":{},"graphs":[],"errors":[]}
    B="editor_toolset.toolsets.blueprint.BlueprintTools"
    O="editor_toolset.toolsets.object.ObjectTools"
    for method in ("get_parent","list_functions","list_events","list_variables","list_event_dispatchers"):
        try:
            row["metadata"][method]=call(B,method,{"blueprint":bp})
        except Exception as e:
            row["errors"].append({"step":method,"error":str(e)})
    try:
        cdo=call(B,"get_default_object",{"blueprint":bp})
        row["metadata"]["default_object"]=cdo
        try:
            schema=call(O,"list_properties",{"instance":cdo})
            if isinstance(schema,str):schema=json.loads(schema)
            row["metadata"]["cdo_schema"]=schema
            if isinstance(schema,dict) and schema:
                row["metadata"]["class_defaults"]=call(O,"get_properties",{"instance":cdo,"properties":list(schema.keys())})
        except Exception as e:
            row["errors"].append({"step":"cdo_properties","error":str(e)})
        # ActorTools.get_components expects an Actor. A Blueprint CDO is not
        # necessarily a valid Actor instance for this endpoint. Record coverage,
        # not a false assertion of complete component hierarchy.
        try:
            components=call("editor_toolset.toolsets.actor.ActorTools","get_components",{"actor":cdo})
            row["metadata"]["components"]=components
            row["metadata"]["component_probe_status"]="returned"
        except Exception as e:
            row["metadata"]["component_probe_status"]="unsupported_on_cdo"
            row["metadata"]["component_probe_reason"]=str(e)
    except Exception as e:
        row["errors"].append({"step":"get_default_object","error":str(e)})
    try:
        graphs=call(B,"list_graphs",{"blueprint":bp})
    except Exception as e:
        row["errors"].append({"step":"list_graphs","error":str(e)})
        return row
    for graph in graphs:
        entry={"graph":graph,"nodes":[],"errors":[]}
        try:
            refs=call(B,"find_nodes",{"graph":graph,"title":"","entry_points_only":False})
            infos=call(B,"get_node_infos",{"nodes":refs}) if refs else []
            if len(infos)!=len(refs):
                entry["errors"].append({"step":"get_node_infos","error":"Node count mismatch","references":len(refs),"infos":len(infos)})
            entry["nodes"]=infos
            if INCLUDE_DSL:
                try:
                    entry["dsl"]=call(B,"read_graph_dsl",{"graph":graph})
                except Exception as e:
                    entry["errors"].append({"step":"read_graph_dsl","error":str(e)})
            if INCLUDE_PROPERTIES:
                for node in infos:
                    try:
                        schema=call(O,"list_properties",{"instance":node["node"]})
                        if isinstance(schema,str):schema=json.loads(schema)
                        node["property_schema"]=schema
                        if isinstance(schema,dict) and schema:
                            values=call(O,"get_properties",{"instance":node["node"],"properties":list(schema.keys())})
                            node["properties"]=values
                    except Exception as e:
                        entry["errors"].append({"step":"node_properties","node":node.get("node"),"error":str(e)})
        except Exception as e:
            entry["errors"].append({"step":"graph_nodes","error":str(e)})
        row["graphs"].append(entry)
    return row
'''.replace('BLUEPRINT',json.dumps(obj)).replace('ASSET',json.dumps(path)).replace('INCLUDE_DSL', 'True' if dsl else 'False').replace('INCLUDE_PROPERTIES','True' if properties else 'False')
    row=mcp.program(script)
    if not isinstance(row,dict) or row.get('asset_path')!=path:
        raise RuntimeError('Malformed Blueprint result or wrong asset')
    errors=list(row.get('errors',[]))
    for graph in row.get('graphs',[]):errors.extend(graph.get('errors',[]))
    row['status']='complete' if not errors else 'partial'
    row['error_count']=len(errors)
    row['graph_count']=len(row.get('graphs',[]))
    row['graph_data_present']=isinstance(row.get('graphs'),list) and not any('error' in g for g in row.get('graphs',[]))
    row['node_count']=sum(len(g.get('nodes',[])) for g in row.get('graphs',[]))
    row['exported_utc']=utc()
    return row


# v1.5: Recovery uses one documented read-only MCP endpoint at a time.
# The original fast exporter remains available for previously successful assets.
def _exception_text(exc):
    return str(exc)[:4000]


def _safe_tool(mcp, group, name, args, errors, step):
    """Record asset-level errors. Transport failures are fatal and resumable."""
    try:
        return mcp.tool(group, name, args), True
    except requests.RequestException:
        raise
    except (RuntimeError, TypeError, ValueError, KeyError) as exc:
        errors.append({'step': step, 'error': _exception_text(exc)})
        return None, False


def _schema_properties(mcp, ref, errors, stage):
    schema, ok = _safe_tool(mcp, OBJ, 'list_properties', {'instance': ref}, errors, stage + '.list_properties')
    if not ok:
        return None
    if isinstance(schema, str):
        try:
            schema=json.loads(schema)
        except ValueError:
            errors.append({'step': stage + '.parse_schema', 'error': 'Property schema is a non-JSON string'})
            return {'raw_schema': schema}
    data = {'schema':schema}
    if isinstance(schema, dict) and schema:
        values, ok = _safe_tool(mcp, OBJ, 'get_properties', {'instance':ref, 'properties':list(schema)},
                                errors, stage + '.get_properties')
        if ok:
            data['values']=values
    return data


def _get_node_infos_resilient(mcp, refs, errors, chunk_size=16):
    """Bisect faulty node chunks; never lose data from unaffected nodes."""
    if not refs:
        return []
    gathered=[]
    def query(chunk):
        try:
            infos=mcp.tool(BP,'get_node_infos',{'nodes':chunk})
            if not isinstance(infos, list) or len(infos)!=len(chunk):
                raise RuntimeError(f'Unexpected NodeInfo count: expected={len(chunk)}, got={len(infos) if isinstance(infos,list) else type(infos).__name__}')
            gathered.extend(infos)
        except requests.RequestException:
            raise
        except (RuntimeError,ValueError,TypeError) as exc:
            if len(chunk)>1:
                mid=len(chunk)//2
                query(chunk[:mid])
                query(chunk[mid:])
            else:
                errors.append({'step':'get_node_infos','node':chunk[0], 'error':_exception_text(exc)})
    for i in range(0,len(refs),chunk_size):
        query(refs[i:i+chunk_size])
    return gathered


def export_blueprint_recovery(mcp, path, *, properties=False, dsl=False,
                              checkpoint=None, previous=None, signature=None,
                              node_batch=16, asset_class=None):
    """Read-only fallback for large/special Blueprint types. Saves after every graph."""
    obj=asset_reference(path)
    row={
        'asset_path':path, 'metadata':{}, 'graphs':[], 'errors':[],
        'status':'in_progress', 'recovery_version':15,
        'source_signature':signature, 'includes_properties':properties,
        'includes_dsl':dsl, 'exported_utc':utc(),
    }
    reusable={}
    if (isinstance(previous, dict) and previous.get('recovery_version')==15
        and previous.get('source_signature')==signature
        and previous.get('includes_properties')==properties
        and previous.get('includes_dsl')==dsl):
        for entry in previous.get('graphs',[]):
            if (isinstance(entry,dict) and entry.get('checkpoint_complete') is True
                and not entry.get('errors')):
                reusable[json.dumps(entry.get('graph'),sort_keys=True,ensure_ascii=False)]=entry

    # Keep metadata probes separate, so an unsupported endpoint cannot destroy graphs.
    for method in ('get_parent','list_functions','list_events','list_variables','list_event_dispatchers'):
        value, ok=_safe_tool(mcp, BP, method, {'blueprint':obj}, row['errors'], method)
        if ok:
            row['metadata'][method]=value
    cdo, ok=_safe_tool(mcp,BP,'get_default_object',{'blueprint':obj},row['errors'],'get_default_object')
    if ok:
        row['metadata']['default_object']=cdo
        props=_schema_properties(mcp,cdo,row['errors'],'cdo_properties')
        if props is not None:
            row['metadata']['cdo_schema']=props['schema']
            if 'values' in props:
                row['metadata']['class_defaults']=props['values']
        # ActorTools.get_components is not a valid general-purpose CDO inspector.
        # Do not call it for non-actor or unknown Blueprint subtypes.
        row['metadata']['component_probe_status']='not_attempted_on_cdo_without_actor_type_check'

    graphs, ok=_safe_tool(mcp,BP,'list_graphs',{'blueprint':obj},row['errors'],'list_graphs')
    graph_first_failure = row['errors'][-1] if not ok else None
    if not ok:
        # Some tools do not resolve a constructed object reference for special BPs.
        loaded, loaded_ok=_safe_tool(mcp,ASSET,'load_asset',{'asset_path':path},
                                     row['errors'],'load_asset_fallback')
        if loaded_ok and isinstance(loaded,dict) and loaded.get('refPath') and loaded != obj:
            row['metadata']['loaded_asset_ref']=loaded
            obj=loaded
            graphs, ok=_safe_tool(mcp,BP,'list_graphs',{'blueprint':obj},row['errors'],
                                   'list_graphs_loaded_ref')
            if ok and graph_first_failure in row['errors']:
                row['errors'].remove(graph_first_failure)
                row.setdefault('recovered_tool_failures',[]).append(graph_first_failure)
    if not ok or not isinstance(graphs,list):
        if ok:
            row['errors'].append({'step':'list_graphs','error':f'Expected list, got {type(graphs).__name__}'})
        row['graph_listing_status']='failed'
        row['status']='failed'  # Core data absent: never call this complete or partially graphed.
        row['error_count']=len(row['errors'])
        row['graph_count']=0
        row['node_count']=0
        row['exported_utc']=utc()
        if checkpoint:
            checkpoint(row)
        return row

    row['graph_listing_status']='complete'
    row['declared_graph_count']=len(graphs)
    row['asset_class']=asset_class
    if not graphs and asset_class and any(tag in str(asset_class).lower() for tag in
                                      ('animblueprint','controlrig','pose','statetree')):
        row['errors'].append({'step':'graph_coverage',
                              'error':f'BlueprintTools.list_graphs returned no graphs for special asset class {asset_class}; cannot prove full graph coverage'})
    if checkpoint:
        checkpoint(row)
    for index,graph in enumerate(graphs,1):
        key=json.dumps(graph,sort_keys=True,ensure_ascii=False)
        if key in reusable:
            entry=reusable[key]
            row['graphs'].append(entry)
            if checkpoint:
                checkpoint(row)
            print(f'[BlueprintGraph] {index}/{len(graphs)} cached {path}',flush=True)
            continue
        entry={'graph':graph,'nodes':[],'errors':[], 'checkpoint_complete':False}
        refs,ok=_safe_tool(mcp,BP,'find_nodes',
                         {'graph':graph,'title':'','entry_points_only':False},
                         entry['errors'],'find_nodes')
        if ok and isinstance(refs,list):
            entry['expected_node_count']=len(refs)
            entry['nodes']=_get_node_infos_resilient(mcp,refs,entry['errors'],node_batch)
        elif ok:
            entry['errors'].append({'step':'find_nodes','error':f'Expected list, got {type(refs).__name__}'})
        if dsl:
            val,ok=_safe_tool(mcp,BP,'read_graph_dsl',{'graph':graph},
                              entry['errors'],'read_graph_dsl')
            if ok:
                entry['dsl']=val
        if properties:
            for node in entry['nodes']:
                ref=node.get('node') if isinstance(node,dict) else None
                if not isinstance(ref,dict) or 'refPath' not in ref:
                    entry['errors'].append({'step':'node_properties','error':'NodeInfo did not supply a valid node ref'})
                    continue
                vals=_schema_properties(mcp,ref,entry['errors'],'node_properties.'+ref['refPath'])
                if vals is not None:
                    node['property_schema']=vals['schema']
                    if 'values' in vals:
                        node['properties']=vals['values']
        entry['checkpoint_complete']=True
        row['graphs'].append(entry)
        if checkpoint:
            checkpoint(row)
        print(f'[BlueprintGraph] {index}/{len(graphs)} done {path} nodes={len(entry["nodes"])} '
              f'errors={len(entry["errors"])}',flush=True)
    total_errors=len(row['errors'])+sum(len(x['errors']) for x in row['graphs'])
    row['error_count']=total_errors
    row['graph_count']=len(row['graphs'])
    row['node_count']=sum(len(x.get('nodes',[])) for x in row['graphs'])
    row['status']='complete' if total_errors==0 else 'partial'
    row['exported_utc']=utc()
    if checkpoint:
        checkpoint(row)
    return row


def main():
    ap=argparse.ArgumentParser(description='Full /Game asset inventory + resumable Blueprint export over Unreal MCP; read-only to game assets')
    ap.add_argument('--project',required=True,help='Directory containing .uproject')
    ap.add_argument('--output',help='Output folder (default: Research/MyGameProject_GAS_Test_EXPORT)')
    ap.add_argument('--url',default='http://127.0.0.1:8000/mcp')
    ap.add_argument('--phase',choices=['all','inventory','registry','blueprints'],default='all')
    ap.add_argument('--properties',action='store_true',help='Also read properties of every Blueprint node; slower and larger')
    ap.add_argument('--dsl',action='store_true',help='Also read Blueprint graph DSL')
    ap.add_argument('--batch-size',type=int,default=25)
    ap.add_argument('--asset',action='append',help='Limit detailed export to given /Game/ asset; repeatable')
    ap.add_argument('--include-plugins',action='store_true',help='Include discovered plugin content assets in MCP registry; detailed Blueprint export is limited to /Game/')
    ap.add_argument('--engine-root',help='UE installation directory (or its Engine/ directory), for SHA-256 caching of engine-plugin assets; alternatively set UE_ENGINE_ROOT')
    ap.add_argument('--refresh-plugins',action='store_true',help='Force re-export of plugin registry records even if their file hashes are unchanged')
    ap.add_argument('--max-blueprints',type=int,default=0,help='Optional cap for trial runs; 0=all')
    ap.add_argument('--zip',action='store_true',help='Pack exported data into ZIP at the end')
    ap.add_argument('--only-failed-blueprints',action='store_true',
                    help='Retry only existing failed/partial/in_progress Blueprint records; do not touch the completed ones')
    ap.add_argument('--blueprint-export-mode', choices=['auto','fast','resilient'],default='auto',
                    help='auto: previous failures go directly to resilient read-only extraction')
    ap.add_argument('--mcp-timeout',type=int,default=120,
                    help='HTTP read timeout in seconds, default 120')
    ap.add_argument('--node-batch',type=int,default=16,
                    help='Max refs per get_node_infos query in resilient mode; default 16')
    opts=ap.parse_args()
    if opts.batch_size < 1:
        ap.error('--batch-size must be a positive integer')
    if opts.node_batch < 1:
        ap.error('--node-batch must be a positive integer')
    if opts.mcp_timeout < 1:
        ap.error('--mcp-timeout must be a positive integer')
    root=Path(opts.project).resolve()
    if not root.is_dir() or not list(root.glob('*.uproject')):
        ap.error('--project must be a directory containing a .uproject file')
    out=Path(opts.output).resolve() if opts.output else Path.cwd()/(root.name+'_EXPORT')
    if out==root or root in out.parents:
        ap.error('--output must be outside the Unreal project (to avoid accidental content scanning)')
    out.mkdir(parents=True,exist_ok=True)
    print('[Export DB]', out, flush=True)
    try:
        engine_hint = opts.engine_root or (engine_root_from_environment() if opts.include_plugins else None)
        resolver = AssetFileResolver(root, engine_hint)
    except ValueError as exc:
        ap.error(str(exc))
    if opts.phase in ('all','inventory'):
        files=inventory_files(root,out)
        print(f'[Filesystem] /Game assets on disk: {len(files)}',flush=True)
    if opts.phase=='inventory':return
    mcp=MCP(opts.url,timeout=opts.mcp_timeout)
    paths=mcp.tool(ASSET,'find_assets',{'folder_path':'/Game','name':'','recursive':True})
    paths=sorted(set(p for p in paths if isinstance(p,str) and p.startswith('/Game/')))
    ident=check_project(mcp,root,paths)
    if opts.asset:
        missing=set(opts.asset)-set(paths)
        if missing: raise RuntimeError('Requested /Game assets not found by MCP: '+repr(sorted(missing)))
    if opts.include_plugins:
        plugin_mounts = mcp.tool(ASSET, 'get_plugin_content_paths', {'include_engine': True})
        if not isinstance(plugin_mounts, list):
            raise RuntimeError('get_plugin_content_paths returned a non-list')
        valid_mounts = {p.rstrip('/') + '/' for p in plugin_mounts
                        if isinstance(p, str) and p.startswith('/') and p.rstrip('/') not in ('', '/Game', '/Engine')}
        other = mcp.tool(ASSET,'find_assets',{'folder_path':'','name':'','recursive':True})
        if not isinstance(other, list):
            raise RuntimeError('find_assets returned a non-list')
        plugin_assets = {p for p in other if isinstance(p,str) and
                         any(p.startswith(mount) for mount in valid_mounts)}
        paths=sorted(set(paths)|plugin_assets)
        report = plugin_resolution_report(resolver, paths)
        report['mcp_plugin_mounts'] = sorted(valid_mounts)
        dump(out/'plugin_resolution.json', report)
        print(f'[Plugins] mounts={len(valid_mounts)}, registered={len(plugin_assets)}, '
              f'verified_files={report["resolved"]}, unresolved={report["unresolved"]}',flush=True)
        if report['unresolved']:
            print('[Plugins] Unresolved files cannot use verified cache. '
                  'For engine plugins supply --engine-root or UE_ENGINE_ROOT. '
                  'See plugin_resolution.json for other unresolved mounts.',flush=True)
    discovered_paths = paths
    external_paths = [path for path in discovered_paths if is_external_package(path)]
    # Preserve full discovery and file metadata; do not try to load OFPA
    # internal packages as standalone AssetTools objects.
    external_report = external_package_report(external_paths, resolver)
    dump(out / 'external_packages.json', external_report)
    paths = [path for path in discovered_paths if not is_external_package(path)]
    dump(out/'mcp_discovery.json',{
        'source': 'MCP AssetTools.find_assets', 'total': len(discovered_paths),
        'paths': discovered_paths, 'registry_eligible': len(paths),
        'external_packages_excluded_from_assettools': len(external_paths),
        'external_packages_manifest': 'external_packages.json',
        'project_identity': ident, 'utc': utc(),
    })
    print(f'[MCP] discovered assets (Game + optional plugins): {len(discovered_paths)}', flush=True)
    print(f'[Registry] excluded {len(external_paths)} OFPA external packages from AssetTools; '
          'recorded in external_packages.json', flush=True)
    if opts.phase in ('all','registry'):
        pending=[]
        for path in paths:
            f=out/'registry'/(hashlib.sha1(path.encode()).hexdigest()[:16]+'.json')
            state=resolver.signature(path)
            previous=None
            if f.is_file():
                try:previous=json.loads(f.read_text(encoding='utf-8'))
                except (ValueError,OSError):pass
            if state is not None and not (opts.refresh_plugins and not path.startswith('/Game/')) and cached_ok(previous,state):
                continue
            pending.append((path,state))
        print(f'[Registry] pending {len(pending)}/{len(paths)}',flush=True)
        failed_this_run = 0
        for start in range(0,len(pending),opts.batch_size):
            batch=pending[start:start+opts.batch_size]
            failed_this_run += export_registry_resilient(mcp, batch, out)
            print(f'[Registry] processed {start+len(batch)}/{len(pending)} '
                  f'failed_in_run={failed_this_run}', flush=True)
    if opts.phase in ('all','blueprints'):
        candidates=[]
        asset_classes={}
        for path in paths:
            f=out/'registry'/(hashlib.sha1(path.encode()).hexdigest()[:16]+'.json')
            kind=None
            if f.is_file():
                try:kind=json.loads(f.read_text(encoding='utf-8'))['data'].get('get_asset_class')
                except (ValueError,KeyError):pass
            if kind is None:
                try:kind=mcp.tool(ASSET,'get_asset_class',{'asset_path':path})
                except Exception:continue
            if 'Blueprint' in str(kind) or str(kind).endswith('_C') or kind in ('WidgetBlueprint','AnimBlueprint'):
                candidates.append(path)
                asset_classes[path]=kind
        if opts.only_failed_blueprints:
            def _needs_retry(p):
                old=read_json(out/'blueprints'/(hashlib.sha1(p.encode()).hexdigest()[:16]+'.json'))
                return isinstance(old,dict) and old.get('status') in ('failed','partial','in_progress')
            candidates=[p for p in candidates if _needs_retry(p)]
        if opts.asset:
            candidates=[p for p in candidates if p in opts.asset]
        else:
            candidates=[p for p in candidates if p.startswith('/Game/')]
        if opts.max_blueprints:
            candidates=candidates[:opts.max_blueprints]
        print(f'[Blueprints] discovered {len(candidates)} candidates',flush=True)
        if opts.only_failed_blueprints:
            # Preserve original v1.4 errors before replacing failed JSONs.
            from gasp_blueprint_failures import write_report
            report_path, records, problems = write_report(out, 'blueprint_failures_before_v15.csv')
            print(f'[Diagnostics] saved previous failures: {len(records)} records, '
                  f'{len(problems)} error messages; {report_path}',flush=True)
        for index,path in enumerate(candidates,1):
            f=out/'blueprints'/(hashlib.sha1(path.encode()).hexdigest()[:16]+'.json')
            stat=resolver.signature(path)
            previous=None
            if f.is_file():
                try:previous=json.loads(f.read_text(encoding='utf-8'))
                except (OSError,ValueError):pass
            try:
                dirty=mcp.tool(ASSET,'is_dirty',{'asset_path':path})
            except RuntimeError as exc:
                # A stale/unloadable Blueprint must not abort other exports.
                # Do NOT mask a dead MCP session or general protocol failure.
                if 'Asset does not exist' not in str(exc):
                    raise
                row = {'asset_path':path, 'status':'failed',
                       'errors':[{'step':'is_dirty', 'error':str(exc)}],
                       'source_signature':stat, 'exported_utc':utc(),
                       'includes_properties':opts.properties,
                       'includes_dsl':opts.dsl}
                dump(f, row)
                print(f'[Blueprints] {index}/{len(candidates)} failed {path} '
                      'is_dirty: asset missing', flush=True)
                continue
            if not dirty and stat is not None and cached_ok(previous,stat,properties=opts.properties,dsl=opts.dsl):
                continue
            mode=opts.blueprint_export_mode
            if mode=='auto' and isinstance(previous,dict) and previous.get('status') in ('failed','partial','in_progress'):
                mode='resilient'
            elif mode=='auto':
                mode='fast'
            fast_failure=None
            if mode=='fast':
                try:
                    row=export_blueprint(mcp,path,properties=opts.properties,dsl=opts.dsl)
                except requests.RequestException:
                    raise
                except Exception as error:
                    fast_failure=_exception_text(error)
                    print(f'[Blueprints] fast extraction failed for {path}; switching to resilient mode: {fast_failure[:200]}',flush=True)
                    mode='resilient'
            if mode=='resilient':
                def save_progress(value):
                    dump(f,value)
                try:
                    row=export_blueprint_recovery(
                        mcp,path,properties=opts.properties,dsl=opts.dsl,
                        previous=previous, signature=stat, checkpoint=save_progress,
                        node_batch=opts.node_batch, asset_class=asset_classes.get(path))
                except requests.RequestException:
                    raise
                except Exception as error:
                    # Preserve successful graph checkpoints even when a later
                    # unexpected exception interrupts the recovery routine.
                    saved=read_json(f)
                    if (isinstance(saved,dict) and saved.get('recovery_version')==15
                        and saved.get('asset_path')==path and saved.get('source_signature')==stat):
                        row=saved
                        row.setdefault('errors',[]).append({'step':'resilient_export',
                            'error':_exception_text(error)})
                        row['status']='in_progress' if row.get('graphs') else 'failed'
                    else:
                        row={'asset_path':path,'status':'failed','errors':[{'step':'resilient_export','error':_exception_text(error)}],
                             'exported_utc':utc(),'recovery_version':15}
                if fast_failure:
                    row['fast_export_failure_before_recovery']=fast_failure
            row['source_signature']=stat
            row['editor_dirty_at_export']=dirty
            if dirty:
                row['status']='partial'
                row.setdefault('errors',[]).append({'step':'is_dirty','error':'Asset has unsaved editor changes; disk fingerprint cannot validate snapshot'})
            row['includes_properties']=opts.properties
            row['includes_dsl']=opts.dsl
            dump(f,row)
            print(f'[Blueprints] {index}/{len(candidates)} {row["status"]} {path} graphs={row.get("graph_count",0)} nodes={row.get("node_count",0)}',flush=True)
    # Only count files for assets eligible in the CURRENT discovery. Keep old
    # files on disk for forensic comparison, but never claim they are current.
    registry_current = {hashlib.sha1(path.encode()).hexdigest()[:16] + '.json'
                        for path in paths}
    all_registry_files = list((out/'registry').glob('*.json'))
    registry_files = [f for f in all_registry_files if f.name in registry_current]
    registry_orphans = len(all_registry_files) - len(registry_files)
    bp_files=list((out/'blueprints').glob('*.json'))
    def statuses(files):
        counts={'complete':0,'partial':0,'failed':0}
        for f in files:
            try:
                value=json.loads(f.read_text(encoding='utf-8'))
                state=value.get('status','failed')
                counts[state]=counts.get(state,0)+1
            except (OSError,ValueError):counts['failed']+=1
        return counts
    summary={'project':str(root),'updated_utc':utc(),
             'mcp_assets':len(discovered_paths),
             'registry_eligible_assets':len(paths),
             'old_registry_records_not_in_current_discovery':registry_orphans,
             'external_packages': {'count':len(external_paths),
                                   'file_found':external_report['file_found'],
                                   'manifest':'external_packages.json'},
             'plugin_resolution': read_json(out/'plugin_resolution.json') if opts.include_plugins else None,
             'registry':statuses(registry_files),'blueprints':statuses(bp_files),
             'identity':ident,'note':('Complete refers to the attempted fields, not the entire Unreal asset schema. '
                                      'External OFPA packages are catalogued separately, and old registry records '
                                      'for absent or excluded paths are retained on disk but excluded from coverage counts.')}
    dump(out/'export_summary.json',summary)
    if opts.zip:
        archive=out.with_suffix('.zip')
        temp_archive=archive.with_name(archive.name + '.tmp')
        try:
            with zipfile.ZipFile(temp_archive,'w',compression=zipfile.ZIP_DEFLATED,compresslevel=3,allowZip64=True) as z:
                for f in out.rglob('*'):
                    if f.is_file():z.write(f,f.relative_to(out.parent))
            temp_archive.replace(archive)
        finally:
            if temp_archive.exists():
                temp_archive.unlink()
        print('[Archive]',archive,flush=True)
    print('[DONE]',json.dumps(summary,ensure_ascii=False),flush=True)


if __name__=='__main__':
    try:main()
    except (requests.RequestException,RuntimeError) as exc:
        print('ERROR:',exc,file=sys.stderr)
        sys.exit(1)
