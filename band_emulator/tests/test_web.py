"""Actual HTTP commands, two clients, RAM isolation, snapshots and access bounds."""
import http.client
import json
from pathlib import Path
import sys
import threading
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from web_server import DebugServer, MachineSession


class WebTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.worker = MachineSession()
        cls.worker.start()
        assert cls.worker.ready.wait(10)
        cls.server = DebugServer(('127.0.0.1', 0), cls.worker, 'test-token')
        cls.http_thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.http_thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.worker.closed.set()
        cls.worker.join(5)
        cls.http_thread.join(5)

    def request(self, path, body=None, token='test-token', origin=None):
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_address[1], timeout=15)
        headers = {'Content-Type': 'application/json'}
        if token is not None:
            headers['Authorization'] = 'Bearer ' + token
        if origin:
            headers['Origin'] = origin
        connection.request('POST' if body is not None else 'GET', path,
                           json.dumps(body) if body is not None else None, headers)
        response = connection.getresponse()
        raw = response.read()
        status, kind = response.status, response.getheader('Content-Type')
        connection.close()
        return status, json.loads(raw) if 'application/json' in kind else raw

    def command(self, action, **extra):
        status, result = self.request('/api/command', dict(action=action, **extra))
        self.assertEqual(status, 200, result)
        return result

    def setUp(self):
        self.command('reset')

    def test_static_files_are_public_but_image_and_paths_are_not(self):
        for path in ('/', '/app.js', '/style.css'):
            self.assertEqual(self.request(path, token=None)[0], 200)
        for path in ('/../boot_machine.py', '/profiles/n66gl_2.3.98.json', '/results/browser-session.json'):
            self.assertEqual(self.request(path, token=None)[0], 404)

    def test_api_requires_key_and_rejects_cross_origin(self):
        for path in ('/api/state', '/api/report', '/api/connect'):
            self.assertEqual(self.request(path, token=None)[0], 401)
            self.assertEqual(self.request(path, token='bad')[0], 401)
        self.assertEqual(self.request('/api/command', {'action':'step'}, token=None)[0], 401)
        self.assertEqual(self.request('/api/command', {'action':'step'}, origin='http://unrelated.test')[0], 403)
        self.assertEqual(self.request('/api/state')[1]['instructions'], 0)

    def test_two_connections_observe_same_machine_and_snapshot(self):
        self.command('step', count=1)
        self.assertEqual(self.request('/api/state')[1]['instructions'], 1)
        self.command('save', name='before')
        self.command('step', count=5)
        self.assertEqual(self.request('/api/state')[1]['instructions'], 6)
        self.command('restore', name='before')
        r = self.request('/api/state')[1]
        self.assertEqual(r['instructions'], 1)
        self.assertFalse(r['running'])
        self.command('delete_snapshot', name='before')
        self.assertEqual(self.request('/api/state')[1]['snapshots'], [])

    def test_memory_write_alias_read_restore_and_flash_protection(self):
        self.command('save', name='ram')
        self.command('write_memory', address='0x20002000', hex='41 42 43 44')
        read = self.command('read_memory', address='0x00202000', size=4)
        self.assertEqual(read['result']['hex'], '41424344')
        self.command('restore', name='ram')
        self.assertEqual(self.command('read_memory', address='0x20002000', size=4)['result']['hex'], '00000000')
        for c in [dict(action='write_memory', address='0xc100000', hex='00'),
                  dict(action='read_memory', address='0x40009000', size=4),
                  dict(action='read_memory', address='0x20000000', size=4097),
                  dict(action='write_memory', address='0x20002000', hex='00'*257)]:
            self.assertEqual(self.request('/api/command', c)[0], 400)

    def test_inputs_registers_breakpoints_and_named_snapshots(self):
        self.command('adc', channel=4, value=720)
        self.command('gpio', pin=46, level=1)
        self.command('register', name='r0', value='0xabcd')
        self.command('breakpoint', address='0xc51a964')
        self.command('save', name='inputs')
        self.command('adc', channel=4, value=600)
        self.command('gpio', pin=46, level=0)
        self.command('restore', name='inputs')
        r = self.request('/api/state')[1]
        self.assertEqual(r['registers']['r0'], '0xabcd')
        self.assertEqual(r['virtual_adc_inputs']['4'], 720)
        self.assertEqual(r['gpio']['banks']['0x40089000']['inputs'], '0x4000')
        self.assertIn('0xc51a964', r['breakpoints'])
        self.command('remove_breakpoint', address='0xc51a964')
        self.assertEqual(self.request('/api/state')[1]['breakpoints'], [])

    def test_invalid_commands_keep_worker_alive(self):
        for body in [[], {'action':'exec'}, {'action':'step','count':10001},
                     {'action':'save','name':'../x'}, {'action':'gpio','pin':80,'level':1},
                     {'action':'adc','channel':4,'value':-1}, {'action':'register','name':'xpsr','value':0}]:
            self.assertEqual(self.request('/api/command', body)[0], 400)
        self.command('step')
        self.assertTrue(self.worker.is_alive())
        self.assertEqual(self.request('/api/state')[1]['instructions'], 1)

    def test_terminal_fault_cannot_be_bypassed_by_pc_edit(self):
        self.command('save', name='clean')
        self.command('register', name='pc', value='0x0c1610c8')
        self.command('step')
        self.assertEqual(self.request('/api/state')[1]['status'], 'firmware_assert')
        for c in [dict(action='run'), dict(action='register',name='pc',value='0xc100010'),
                  dict(action='write_memory',address='0x20000000',hex='00')]:
            self.assertEqual(self.request('/api/command', c)[0], 400)
        self.command('restore', name='clean')
        self.command('step')
        self.assertEqual(self.request('/api/state')[1]['instructions'], 1)

    def test_full_report_and_connection_addresses(self):
        report = self.request('/api/report')[1]
        self.assertFalse(report['hardware_accuracy'])
        self.assertIn('hle_replacements', report)
        addresses = self.request('/api/connect')[1]['addresses']
        self.assertEqual(addresses, [f'http://127.0.0.1:{self.server.server_address[1]}/'])

    def test_second_server_cannot_replace_live_port(self):
        with self.assertRaises(OSError):
            DebugServer(self.server.server_address, self.worker, 'other-token')
        self.command('step')
        self.assertEqual(self.request('/api/state')[1]['instructions'], 1)


if __name__ == '__main__':
    unittest.main()
