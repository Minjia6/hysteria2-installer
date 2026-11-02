"""sing-box 卸载：systemd unit 缺失时仍能卸掉核心，未知状态则 fail-closed。

用 ast 抽出生产代码，配上假的 systemctl，锁行为而不是源码字符串。
"""
import ast
import copy
import io
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlencode

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import portal

SOURCE = Path(portal.__file__).read_text(encoding='utf-8')
TREE = ast.parse(SOURCE)
NODE = shutil.which('node')
HELPERS = (
    '_systemd_load_state',
    '_systemd_state',
    '_systemd_service_snapshot',
    '_restore_systemd_service_state',
    '_service_stop_confirmed',
    '_h_post_uninstall_core',
)


def extract(name, namespace):
    node = next(item for item in ast.walk(TREE)
                if isinstance(item, ast.FunctionDef) and item.name == name)
    module = ast.Module(body=[copy.deepcopy(node)], type_ignores=[])
    exec(compile(ast.fix_missing_locations(module), str(Path(portal.__file__)), 'exec'), namespace)
    return namespace[name]


class FakeSystemctl:
    def __init__(self, units):
        self.units = units
        self.commands = []
        self.fail_stop = set()
        self.fail_restore_start = set()
        self.on_command = None

    def run(self, args, **kwargs):
        args = list(args)
        self.commands.append(args)
        if self.on_command:
            self.on_command(args)
        if not args or args[0] != 'systemctl':
            return SimpleNamespace(returncode=1, stdout='', stderr='unexpected')
        if args[1] == 'show' and '--property=LoadState' in args:
            return self._load(args, kwargs)
        if args[1] == 'daemon-reload':
            return SimpleNamespace(returncode=0, stdout='', stderr='')
        if args[1] == 'reset-failed':
            return SimpleNamespace(returncode=0, stdout='', stderr='')
        name = args[-1]
        spec = self.units.setdefault(name, {
            'load': 'loaded', 'active': 'inactive', 'enabled': 'disabled'})
        if args[1:3] == ['disable', '--now']:
            if name in self.fail_stop:
                return SimpleNamespace(returncode=1, stdout='', stderr='stop failed')
            spec['active'] = 'inactive'
            spec['enabled'] = 'disabled'
            return SimpleNamespace(returncode=0, stdout='', stderr='')
        if args[1] == 'is-active' and len(args) == 3:
            return SimpleNamespace(returncode=0, stdout=spec['active'] + '\n', stderr='')
        if args[1] == 'is-enabled' and len(args) == 3:
            return SimpleNamespace(returncode=0, stdout=spec['enabled'] + '\n', stderr='')
        if args[1] == 'disable' and len(args) == 3:
            spec['enabled'] = 'disabled'
            return SimpleNamespace(returncode=0, stdout='', stderr='')
        if args[1] == 'enable' and len(args) == 3:
            spec['enabled'] = 'enabled'
            return SimpleNamespace(returncode=0, stdout='', stderr='')
        if args[1] == 'start' and len(args) == 3:
            if name in self.fail_restore_start:
                return SimpleNamespace(returncode=1, stdout='', stderr='start failed')
            spec['active'] = 'active'
            return SimpleNamespace(returncode=0, stdout='', stderr='')
        if args[1] == 'stop' and len(args) == 3:
            spec['active'] = 'inactive'
            return SimpleNamespace(returncode=0, stdout='', stderr='')
        return SimpleNamespace(returncode=1, stdout='', stderr='unhandled')

    def _load(self, args, kwargs):
        spec = self.units.get(args[-1], {})
        error = spec.get('load_error')
        if error == 'timeout':
            raise subprocess.TimeoutExpired(args, kwargs.get('timeout', 5))
        if error == 'oserror':
            raise OSError('systemctl missing')
        if error == 'rc':
            return SimpleNamespace(returncode=1, stdout=spec.get('load', ''), stderr='fail')
        if error == 'blank':
            return SimpleNamespace(returncode=0, stdout='\n', stderr='')
        return SimpleNamespace(returncode=0, stdout=spec.get('load', 'loaded') + '\n', stderr='')


class SingboxUninstallTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

        def fake_path(value):
            text = str(value)
            if text.startswith(('/etc/', '/usr/', '/var/')):
                text = str(self.root / text.lstrip('/'))
            return Path(text)

        self.fake_path = fake_path
        self.binary = fake_path(portal.ANYTLS_BINARY)
        self.binary.parent.mkdir(parents=True, exist_ok=True)
        self.binary.write_text('sing-box-bin', encoding='utf-8')
        self.unit_paths = {}
        for unit in (portal.ANYTLS_SERVICE, portal.TUIC_SERVICE):
            path = fake_path('/etc/systemd/system/%s.service' % unit)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('unit %s\n' % unit, encoding='utf-8')
            self.unit_paths[unit] = path
        self.fake = FakeSystemctl({})
        self.ns = {
            'parse_qs': parse_qs,
            'ANYTLS_SERVICE': portal.ANYTLS_SERVICE,
            'TUIC_SERVICE': portal.TUIC_SERVICE,
            'ANYTLS_BINARY': portal.ANYTLS_BINARY,
            'Path': fake_path,
            'os': SimpleNamespace(close=lambda fd: None),
            'subprocess': SimpleNamespace(
                run=self.fake.run,
                SubprocessError=subprocess.SubprocessError,
                TimeoutExpired=subprocess.TimeoutExpired),
            'core_operation_lock': threading.RLock(),
            'acquire_core_operation_lock': lambda: 7,
        }
        for name in HELPERS:
            extract(name, self.ns)
        self.handler = SimpleNamespace(
            is_authenticated=lambda: True,
            _core_users=lambda name: [],
            response=None)
        self.handler.reply_json = self.reply_json

    def tearDown(self):
        self.tmp.cleanup()

    def reply_json(self, status, payload):
        self.handler.response = (status, payload)
        return self.handler.response

    def uninstall(self, name='sing-box'):
        body = urlencode({'name': name}).encode()
        self.handler.headers = {'Content-Length': str(len(body))}
        self.handler.rfile = io.BytesIO(body)
        return self.ns['_h_post_uninstall_core'](self.handler, '/token/')

    def set_units(self, **specs):
        self.fake.units.clear()
        self.fake.units.update(specs)

    def assert_binary_kept(self):
        self.assertTrue(self.binary.exists())
        self.assertEqual(self.binary.read_text(encoding='utf-8'), 'sing-box-bin')
        for path in self.unit_paths.values():
            self.assertTrue(path.exists(), path)

    def assert_no_disable_now(self):
        self.assertFalse(
            [cmd for cmd in self.fake.commands if cmd[:3] == ['systemctl', 'disable', '--now']],
            self.fake.commands)

    def test_both_units_not_found_still_removes_core(self):
        self.set_units(
            **{name: {'load': 'not-found', 'active': '', 'enabled': 'not-found'}
               for name in self.unit_paths})
        status, body = self.uninstall()
        self.assertEqual(status, 200, body)
        self.assertIn('已卸载', body['message'])
        self.assert_no_disable_now()
        self.assertFalse(self.binary.exists())
        for path in self.unit_paths.values():
            self.assertFalse(path.exists(), path)
        self.assertIn(['systemctl', 'daemon-reload'], self.fake.commands)
        for unit in self.unit_paths:
            self.assertIn(['systemctl', 'reset-failed', unit], self.fake.commands)

    def test_only_loaded_unit_is_stopped(self):
        self.set_units(**{
            portal.ANYTLS_SERVICE: {'load': 'loaded', 'active': 'inactive', 'enabled': 'disabled'},
            portal.TUIC_SERVICE: {'load': 'not-found', 'active': '', 'enabled': 'not-found'},
        })
        status, body = self.uninstall()
        self.assertEqual(status, 200, body)
        stopped = [cmd for cmd in self.fake.commands if cmd[:3] == ['systemctl', 'disable', '--now']]
        self.assertEqual(stopped, [['systemctl', 'disable', '--now', portal.ANYTLS_SERVICE]])
        self.assertFalse(self.binary.exists())
        self.assertNotIn(['systemctl', 'enable', portal.TUIC_SERVICE], self.fake.commands)
        self.assertNotIn(['systemctl', 'start', portal.TUIC_SERVICE], self.fake.commands)
        self.assertNotIn(['systemctl', 'disable', portal.TUIC_SERVICE], self.fake.commands)

    def test_unknown_load_state_aborts_and_keeps_core(self):
        cases = (
            {'load': 'masked', 'active': 'active', 'enabled': 'enabled'},
            {'load_error': 'rc', 'load': 'loaded', 'active': 'active', 'enabled': 'enabled'},
            {'load_error': 'timeout', 'active': 'active', 'enabled': 'enabled'},
            {'load_error': 'oserror'},
            {'load_error': 'blank', 'active': 'inactive', 'enabled': 'disabled'},
        )
        for spec in cases:
            with self.subTest(spec=spec):
                self.fake.commands.clear()
                self.binary.write_text('sing-box-bin', encoding='utf-8')
                self.set_units(**{
                    portal.ANYTLS_SERVICE: {'load': 'loaded', 'active': 'inactive', 'enabled': 'disabled'},
                    portal.TUIC_SERVICE: dict(spec),
                })
                status, body = self.uninstall()
                self.assertEqual(status, 500, body)
                self.assertIn(portal.TUIC_SERVICE, body['error'])
                self.assertIn('保留所有文件', body['error'])
                self.assert_no_disable_now()
                self.assert_binary_kept()
                self.assertNotIn(['systemctl', 'daemon-reload'], self.fake.commands)

    def test_unusable_snapshot_aborts_before_stop(self):
        self.set_units(**{
            portal.ANYTLS_SERVICE: {'load': 'loaded', 'active': 'activating', 'enabled': 'enabled'},
            portal.TUIC_SERVICE: {'load': 'loaded', 'active': 'inactive', 'enabled': 'disabled'},
        })
        status, body = self.uninstall()
        self.assertEqual(status, 500, body)
        self.assertIn('快照', body['error'])
        self.assert_no_disable_now()
        self.assert_binary_kept()

    def test_stop_failure_restores_original_active_and_enabled(self):
        self.set_units(**{
            portal.ANYTLS_SERVICE: {'load': 'loaded', 'active': 'active', 'enabled': 'enabled'},
            portal.TUIC_SERVICE: {'load': 'loaded', 'active': 'inactive', 'enabled': 'disabled'},
        })
        self.fake.fail_stop.add(portal.TUIC_SERVICE)
        status, body = self.uninstall()
        self.assertEqual(status, 500, body)
        self.assertIn('已恢复原服务状态', body['error'])
        self.assertEqual(self.fake.units[portal.ANYTLS_SERVICE]['active'], 'active')
        self.assertEqual(self.fake.units[portal.ANYTLS_SERVICE]['enabled'], 'enabled')
        self.assertEqual(self.fake.units[portal.TUIC_SERVICE]['active'], 'inactive')
        self.assertEqual(self.fake.units[portal.TUIC_SERVICE]['enabled'], 'disabled')
        self.assert_binary_kept()
        self.assertNotIn(['systemctl', 'daemon-reload'], self.fake.commands)
        self.assertIn(['systemctl', 'enable', portal.ANYTLS_SERVICE], self.fake.commands)
        self.assertIn(['systemctl', 'start', portal.ANYTLS_SERVICE], self.fake.commands)
        self.assertIn(['systemctl', 'disable', portal.TUIC_SERVICE], self.fake.commands)
        self.assertIn(['systemctl', 'stop', portal.TUIC_SERVICE], self.fake.commands)

    def test_rollback_failure_keeps_core_files(self):
        self.set_units(**{
            portal.ANYTLS_SERVICE: {'load': 'loaded', 'active': 'active', 'enabled': 'enabled'},
            portal.TUIC_SERVICE: {'load': 'not-found', 'active': '', 'enabled': 'not-found'},
        })
        self.fake.fail_stop.add(portal.ANYTLS_SERVICE)
        self.fake.fail_restore_start.add(portal.ANYTLS_SERVICE)
        status, body = self.uninstall()
        self.assertEqual(status, 500, body)
        self.assertIn('恢复未确认', body['error'])
        self.assertIn(portal.ANYTLS_SERVICE, body['error'])
        self.assert_binary_kept()
        self.assertNotIn(['systemctl', 'daemon-reload'], self.fake.commands)
        self.assertNotIn(
            ['systemctl', 'disable', '--now', portal.TUIC_SERVICE], self.fake.commands)
        self.assertNotIn(['systemctl', 'start', portal.TUIC_SERVICE], self.fake.commands)

    def test_core_file_removed_only_after_stops_are_confirmed(self):
        self.set_units(**{
            portal.ANYTLS_SERVICE: {'load': 'loaded', 'active': 'active', 'enabled': 'enabled'},
            portal.TUIC_SERVICE: {'load': 'loaded', 'active': 'inactive', 'enabled': 'static'},
        })
        seen = {}

        def watch(args):
            if args == ['systemctl', 'daemon-reload']:
                seen['binary'] = self.binary.exists()
                seen['units'] = {name: path.exists() for name, path in self.unit_paths.items()}

        self.fake.on_command = watch
        status, body = self.uninstall()
        self.assertEqual(status, 200, body)
        self.assertTrue(seen['binary'])
        self.assertFalse(any(seen['units'].values()))
        self.assertFalse(self.binary.exists())
        disable_at = [i for i, cmd in enumerate(self.fake.commands)
                      if cmd[:3] == ['systemctl', 'disable', '--now']]
        reload_at = self.fake.commands.index(['systemctl', 'daemon-reload'])
        self.assertEqual(len(disable_at), 2)
        self.assertLess(max(disable_at), reload_at)
        self.assertLess(reload_at, self.fake.commands.index(
            ['systemctl', 'reset-failed', portal.TUIC_SERVICE]))

    def test_protocols_in_use_and_auth_still_block_uninstall(self):
        self.set_units(**{
            name: {'load': 'not-found', 'active': '', 'enabled': 'not-found'}
            for name in self.unit_paths})
        self.handler._core_users = lambda name: ['AnyTLS']
        status, body = self.uninstall()
        self.assertEqual(status, 409, body)
        self.assert_binary_kept()
        self.assertEqual(self.fake.commands, [])
        self.handler._core_users = lambda name: []
        self.handler.is_authenticated = lambda: False
        status, body = self.uninstall()
        self.assertEqual(status, 401, body)
        self.assert_binary_kept()
        self.assertEqual(self.fake.commands, [])


def _uninstall_js():
    script = portal.SCRIPT
    start = script.index('async function uninstallCore')
    end = script.index('\nfunction gotoTarget', start)
    return script[start:end]


class UninstallCoreUiTests(unittest.TestCase):
    @unittest.skipUnless(NODE, '需要 node 才能执行前端行为')
    def test_timeout_restores_button_and_clears_timer(self):
        harness = r'''
const assert = require('assert');
const alerts = [];
const alert = (msg) => { alerts.push(String(msg)); };
let loadCalls = 0;
const loadCores = async () => { loadCalls += 1; };
const showToast = () => { throw new Error('should not toast'); };
const showConfirm = async () => true;
const location = { pathname: '/panel/' };
const timers = [];
const cleared = [];
const setTimeout = (fn, ms) => {
  const id = { fn, ms };
  timers.push(id);
  return id;
};
const clearTimeout = (id) => { cleared.push(id); };
let seenSignal = null;
const fetch = (url, opts) => new Promise((resolve, reject) => {
  seenSignal = opts.signal;
  const abort = () => {
    const err = new Error('The operation was aborted');
    err.name = 'AbortError';
    reject(err);
  };
  if (opts.signal.aborted) { abort(); return; }
  opts.signal.addEventListener('abort', abort);
});
'''
        checks = r'''
(async () => {
  const btn = { disabled: false, textContent: '卸载' };
  const pending = uninstallCore({ id: 'sing-box', name: 'sing-box' }, btn);
  await Promise.resolve();
  assert.strictEqual(timers.length, 1);
  assert.strictEqual(timers[0].ms, 120000);
  assert.strictEqual(btn.disabled, true);
  timers[0].fn();
  await pending;
  assert.strictEqual(cleared.length, 1);
  assert.strictEqual(cleared[0], timers[0]);
  assert.strictEqual(btn.disabled, false);
  assert.strictEqual(btn.textContent, '卸载');
  assert.strictEqual(loadCalls, 1);
  assert.strictEqual(alerts.length, 1);
  assert.strictEqual(alerts[0], '卸载请求等待超时。请先刷新核心状态确认实际结果，不要立即重复点击卸载。');
  assert.ok(seenSignal);
})().catch((err) => { console.error(err); process.exitCode = 1; });
'''
        result = subprocess.run([NODE, '-e', harness + _uninstall_js() + checks],
                                capture_output=True, text=True, encoding='utf-8')
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)

    @unittest.skipUnless(NODE, '需要 node 才能执行前端行为')
    def test_invalid_json_does_not_throw_and_restores_button(self):
        harness = r'''
const assert = require('assert');
const alerts = [];
const alert = (msg) => { alerts.push(String(msg)); };
let loadCalls = 0;
const loadCores = async () => { loadCalls += 1; };
const showToast = () => { throw new Error('should not toast'); };
const showConfirm = async () => true;
const location = { pathname: '/panel/' };
const cleared = [];
const setTimeout = (fn, ms) => ({ fn, ms });
const clearTimeout = (id) => { cleared.push(id); };
const fetch = async () => ({
  status: 502,
  json: async () => { throw new SyntaxError('Unexpected token <'); }
});
'''
        checks = r'''
(async () => {
  const btn = { disabled: false, textContent: '卸载 sing-box' };
  let escaped = false;
  try {
    await uninstallCore({ id: 'sing-box', name: 'sing-box' }, btn);
  } catch (err) {
    escaped = true;
    console.error(err);
  }
  assert.strictEqual(escaped, false);
  assert.strictEqual(alerts.length, 1);
  assert.ok(alerts[0].includes('502'), alerts[0]);
  assert.ok(alerts[0].includes('JSON'), alerts[0]);
  assert.strictEqual(btn.disabled, false);
  assert.strictEqual(btn.textContent, '卸载 sing-box');
  assert.strictEqual(loadCalls, 1);
  assert.strictEqual(cleared.length, 1);
})().catch((err) => { console.error(err); process.exitCode = 1; });
'''
        result = subprocess.run([NODE, '-e', harness + _uninstall_js() + checks],
                                capture_output=True, text=True, encoding='utf-8')
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)

    @unittest.skipUnless(NODE, '需要 node 才能做 JS 语法检查')
    def test_uninstall_script_parses(self):
        with tempfile.NamedTemporaryFile('w', suffix='.js', delete=False, encoding='utf-8') as handle:
            handle.write(portal.SCRIPT)
            path = handle.name
        try:
            result = subprocess.run([NODE, '--check', path], capture_output=True, text=True, timeout=60)
        finally:
            Path(path).unlink(missing_ok=True)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == '__main__':
    unittest.main()
