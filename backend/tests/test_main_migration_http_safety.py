"""Read-only checks of acceptance tooling; never import or run its application IO."""
import ast
from pathlib import Path
import re
import unittest
from urllib.parse import urlparse

PATH = Path(__file__).parents[1] / 'scripts/check_main_migration_visualization_http.py'
SOURCE = PATH.read_text()
TREE = ast.parse(SOURCE)


def helpers():
    names={'PLUGINS','SOURCE','ID','HASH','EXPECTED_FILES','FORBIDDEN','NATIVE_CODE'}
    nodes=[n for n in TREE.body if isinstance(n,ast.Assign) and any(isinstance(t,ast.Name) and t.id in names for t in n.targets) or isinstance(n,ast.FunctionDef) and n.name=='local_base']
    env={'re':re,'urlparse':urlparse}
    exec(compile(ast.Module(body=nodes,type_ignores=[]),str(PATH),'exec'),env)
    return env


class MainMigrationAcceptanceSafety(unittest.TestCase):
    def test_only_existing_local_frontend(self):
        local=helpers()['local_base']
        for value in ('http://frontend','http://frontend:80/','http://localhost:7001','http://127.0.0.1:7001/','http://[::1]:7001'):
            with self.subTest(value=value):self.assertEqual(local(value),value.rstrip('/'))
        for value in ('https://frontend','http://frontend:7000','http://localhost:7000','http://39.106.98.67:7000','http://evil.invalid:7001','http://frontend/api','http://frontend?x=1','http://frontend/#x','http://a:b@frontend','http://@frontend','http://:x@frontend','http://localhost:7001\n',' http://frontend',None,42):
            with self.subTest(value=value),self.assertRaises(RuntimeError):local(value)

    def test_exact_ids_not_prefixes_and_tag_scope(self):
        env=helpers()
        self.assertEqual(len(env['PLUGINS']),6)
        self.assertEqual(len(env['EXPECTED_FILES']),20)
        self.assertTrue(env['ID'].fullmatch('synthetic-file:123'))
        self.assertFalse(env['ID'].fullmatch('x/path'))
        self.assertFalse(env['ID'].fullmatch('x\n'))
        self.assertTrue(env['HASH'].fullmatch('a'*64))
        self.assertFalse(env['HASH'].fullmatch('a'*63))
        self.assertIn('"metadata.regression_run": tag',SOURCE)
        self.assertIn('"filename": {"$in": names}',SOURCE)
        self.assertNotIn('delete_many(',SOURCE)
        self.assertNotIn('drop_database(',SOURCE)
        self.assertNotIn('"$regex"',SOURCE)

    def test_native_fixture_has_all_isolation_properties(self):
        creates=[n for n in ast.walk(TREE) if isinstance(n,ast.Call) and isinstance(n.func,ast.Attribute) and n.func.attr=='create']
        self.assertEqual(len(creates),1)
        kwargs={k.arg:k.value for k in creates[0].keywords}
        for name,expected in [('network_mode','none'),('user','65534:65534'),('read_only',True),('cap_drop',['ALL']),('security_opt',['no-new-privileges:true'])]:
            self.assertEqual(ast.literal_eval(kwargs[name]),expected)
        self.assertNotIn('volumes',kwargs)
        self.assertNotIn('mounts',kwargs)
        self.assertIn('assert not container.attrs.get("Mounts")',SOURCE.replace(' and not container.attrs.get("Mounts")','; assert not container.attrs.get("Mounts")'))
        code=helpers()['NATIVE_CODE'];ast.parse(code)
        self.assertIn('assert os.getuid()==65534',code)
        self.assertNotIn('pip install',code)
        self.assertNotIn('requests.',code)
        self.assertNotIn('urllib',code)
        self.assertNotIn('/Users/',code)

    def test_owner_cas_restore_and_preserve_business_state(self):
        self.assertIn('"_id": record["_id"], "user_id": record["user_id"]',SOURCE)
        self.assertIn('"user_id": foreign',SOURCE)
        self.assertIn('owner_pending.add(identifier)',SOURCE)
        self.assertIn('finally:\n                    await restore_owner(identifier)',SOURCE)
        self.assertIn('await state(plugin, originals[plugin])',SOURCE)
        self.assertIn('uploaded == deleted and not owner_pending',SOURCE)
        self.assertIn('before == after',SOURCE)
        self.assertIn('"script_model_endpoint_calls": 0',SOURCE)
        self.assertNotIn('/api/v1/agent',SOURCE)

    def test_all_plugin_rejections_and_pure_validation_retained(self):
        for label in ('missing-version','stale-version','undeclared-option','undeclared-operation:','format-mismatch','unsupported-or-malicious-source','foreign-owner','disabled-plugin'):
            self.assertIn(label,SOURCE)
        self.assertIn('validate_payload(private, reader, kind',SOURCE)
        self.assertIn('external-reference-cram-denied',SOURCE)
        self.assertIn('source_bytes=size',SOURCE)
        self.assertIn('if __name__ == "__main__":',SOURCE)


if __name__=='__main__':unittest.main()
