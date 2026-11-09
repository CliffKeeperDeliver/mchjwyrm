"""LAN browser debugger. One worker owns Unicorn; HTTP never touches the CPU.

No shell, arbitrary paths, uploads or MMIO stubs are exposed. A per-process
capability token protects all API access. Intended for a trusted LAN or VPN.
"""
import argparse
import concurrent.futures
import hmac
import ipaddress
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import queue
import secrets
import socket
import threading
import time
from urllib.parse import urlsplit
from urllib.request import Request, build_opener, ProxyHandler
import webbrowser

from boot_machine import BootMachine, HERE, load_profile


def integer(value):
    if type(value) is int:
        return value
    if isinstance(value, str) and len(value) <= 20:
        return int(value, 16 if value.lower().startswith('0x') else 10)
    raise ValueError('Нужно целое число или адрес 0x…')


class MachineSession(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True, name='firmware-cpu')
        self.commands = queue.Queue(maxsize=32)
        self.closed = threading.Event()
        self.ready = threading.Event()
        self.lock = threading.Lock()
        self.running = False
        self.version = 0
        self.state = dict(ready=False, running=False, version=0)
        self.full_report = {}

    def publish(self, notice='', report=None):
        r = self.machine.report() if report is None else report
        self.version += 1
        # Polling transfers only useful changing state, not 80 pin descriptions
        # and every previous replacement. Full evidence is downloadable.
        keys = ('status', 'instructions', 'registers', 'tasks', 'milestones',
                'fault', 'breakpoints', 'snapshots', 'virtual_adc_inputs',
                'virtual_clock', 'virtual_factory_sector', 'gpio', 'limitations')
        state = {key: r.get(key) for key in keys}
        state.update(ready=True, running=self.running, version=self.version,
                     console=r['console'][-24000:], trace=r['trace_tail'],
                     services=r['hle_replacements'][-16:], notice=notice,
                     exception_count=r['exceptions']['total_events'])
        with self.lock:
            self.state, self.full_report = state, r

    def view(self, full=False):
        with self.lock:
            return self.full_report if full else self.state

    def submit(self, command):
        if self.closed.is_set() or not self.ready.is_set():
            raise ValueError('Эмулятор ещё не готов или остановлен')
        future = concurrent.futures.Future()
        try:
            self.commands.put_nowait((command, future))
        except queue.Full:
            raise ValueError('Очередь команд заполнена') from None
        return future

    def execute(self, c):
        if not isinstance(c, dict):
            raise ValueError('Ожидается объект команды')
        action = c.get('action')
        d = self.machine
        if action == 'run':
            if d.status in d.FAULTS:
                raise ValueError('После аппаратной ошибки восстановите снимок или сбросьте машину')
            self.running = True
            return 'Выполнение продолжается', None
        self.running = False
        result = None
        if action == 'pause':
            pass
        elif action == 'reset':
            self.machine = BootMachine(load_profile(HERE / 'profiles/n66gl_2.3.98.json'), output=None)
        elif action == 'step':
            count = integer(c.get('count', 1))
            if not 1 <= count <= 10000:
                raise ValueError('Шаг: 1–10000 инструкций')
            d.advance(count, 1000, stepping=True)
        elif action in ('save', 'restore', 'delete_snapshot'):
            name = c.get('name', 'manual')
            if not isinstance(name, str) or not 1 <= len(name) <= 32 or not all(x.isalnum() or x in '_-' for x in name):
                raise ValueError('Имя снимка: 1–32 буквы, цифры, _ или -')
            if action == 'save':
                d.save(name)
            elif action == 'restore':
                d.restore(name)
            else:
                if name not in d.checkpoints:
                    raise ValueError('Снимок не найден')
                del d.checkpoints[name]
        elif action in ('breakpoint', 'remove_breakpoint'):
            address = integer(c.get('address'))
            if action == 'breakpoint':
                d.set_breakpoint(address)
            else:
                d.breakpoints.discard(address)
        elif action == 'gpio':
            d.gpio.drive(integer(c.get('pin')), integer(c.get('level')))
        elif action == 'adc':
            d.set_adc_input(integer(c.get('channel')), integer(c.get('value')))
        elif action in ('read_memory', 'write_memory', 'register'):
            if action != 'read_memory' and d.status in d.FAULTS:
                raise ValueError('Сначала восстановите снимок или сбросьте машину')
            if action == 'register':
                d.set_register(c.get('name'), integer(c.get('value')))
            else:
                address = integer(c.get('address'))
                if action == 'write_memory':
                    raw = c.get('hex', '')
                    if not isinstance(raw, str) or len(raw) > 1536:
                        raise ValueError('Запись: не более 256 байт HEX')
                    data = bytes.fromhex(raw)
                    if not 1 <= len(data) <= 256:
                        raise ValueError('Запись: 1–256 байт')
                    d.write_memory(address, data)
                else:
                    data = d.read_memory(address, integer(c.get('size', 128)))
                result = dict(address=hex(address), hex=data.hex(), size=len(data),
                              instructions=d.instruction_count)
        else:
            raise ValueError('Неизвестная команда')
        return 'Команда выполнена · машина на паузе', result

    def run(self):
        try:
            self.machine = BootMachine(load_profile(HERE / 'profiles/n66gl_2.3.98.json'), output=None)
            self.publish()
            self.ready.set()
            while not self.closed.is_set():
                try:
                    command, future = self.commands.get(timeout=0 if self.running else 0.1)
                except queue.Empty:
                    command = None
                if command is not None:
                    # A timed-out HTTP request can cancel a command while it is
                    # still queued, so it cannot silently execute much later.
                    if not future.set_running_or_notify_cancel():
                        continue
                    try:
                        notice, result = self.execute(command)
                        self.publish(notice)
                        future.set_result(dict(ok=True, result=result, version=self.version))
                    except (ValueError, KeyError, TypeError) as error:
                        self.running = False
                        self.publish(str(error))
                        future.set_result(dict(ok=False, error=str(error)))
                if self.running:
                    r = self.machine.advance(20000, 250)
                    self.running = r['status'] in ('budget', 'timeout')
                    self.publish(report=r)
        except Exception as error:
            with self.lock:
                self.state = dict(ready=False, running=False, version=self.version + 1, error=str(error))
        finally:
            self.closed.set()
            while not self.commands.empty():
                _, future = self.commands.get_nowait()
                if not future.done():
                    future.set_result(dict(ok=False, error='Рабочий поток завершён'))


class DebugServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = not hasattr(socket, 'SO_EXCLUSIVEADDRUSE')
    allow_reuse_port = False

    def server_bind(self):
        # Windows SO_REUSEADDR permits a second process to steal the listener.
        # The launcher must reuse the existing machine, never replace its port.
        if hasattr(socket, 'SO_EXCLUSIVEADDRUSE'):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()

    def __init__(self, address, session, token):
        self.session, self.token = session, token
        super().__init__(address, Handler)
        hosts = {'127.0.0.1'}
        if address[0] == '0.0.0.0':
            try:
                hosts.update(item[4][0] for item in socket.getaddrinfo(socket.gethostname(), self.server_address[1], socket.AF_INET))
            except OSError:
                pass
        elif address[0] != '127.0.0.1':
            hosts.add(address[0])
        self.addresses = [f'http://{host}:{self.server_address[1]}/' for host in sorted(hosts)
                          if not ipaddress.ip_address(host).is_link_local]


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass  # Do not log capability URLs or memory contents.

    def reply(self, status, body, kind='application/json; charset=utf-8', attachment=False):
        if isinstance(body, dict):
            body = json.dumps(body, ensure_ascii=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', kind)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
        if attachment:
            self.send_header('Content-Disposition', 'attachment; filename="band-debug-report.json"')
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def authorized(self):
        expected = 'Bearer ' + self.server.token
        supplied = self.headers.get('Authorization', '')
        if not hmac.compare_digest(supplied.encode(), expected.encode()):
            self.reply(401, dict(error='Нужен ключ доступа из окна запуска или ссылки подключения'))
            return False
        origin = self.headers.get('Origin')
        # An HTTPS reverse proxy (remote preview) changes the scheme and may
        # rewrite Host; the page is still same-origin if its host matches.
        hosts = {self.headers.get('Host', ''), self.headers.get('X-Forwarded-Host', '')} - {''}
        if origin and not any(origin == scheme + host for scheme in ('http://', 'https://') for host in hosts):
            self.reply(403, dict(error='Другой источник запроса запрещён'))
            return False
        return True

    def do_GET(self):
        path = urlsplit(self.path).path
        if path.startswith('/api/'):
            if not self.authorized():
                return
            if path in ('/api/state', '/api/report'):
                self.reply(200, self.server.session.view(full=path.endswith('report')), attachment=path.endswith('report'))
            elif path == '/api/connect':
                self.reply(200, dict(addresses=self.server.addresses))
            else:
                self.reply(404, dict(error='Не найдено'))
            return
        assets = {'/': ('index.html', 'text/html'), '/app.js': ('app.js', 'text/javascript'), '/style.css': ('style.css', 'text/css')}
        if path not in assets:
            self.reply(404, dict(error='Не найдено'))
            return
        name, kind = assets[path]
        self.reply(200, (HERE / 'web' / name).read_bytes(), kind + '; charset=utf-8')

    def do_POST(self):
        self.connection.settimeout(5)
        try:
            length = int(self.headers.get('Content-Length', '0'))
        except ValueError:
            length = 0
        # Consume the body before any early reply: closing a socket with unread
        # request bytes sends RST and the client loses the 401/403 response.
        try:
            body = self.rfile.read(length) if 1 <= length <= 8192 else b''
        except OSError:
            return
        if not self.authorized():
            return
        if self.path != '/api/command':
            self.reply(404, dict(error='Не найдено'))
            return
        try:
            if not 1 <= length <= 8192 or self.headers.get_content_type() != 'application/json':
                raise ValueError('Ожидается JSON до 8192 байт')
            command = json.loads(body)
            if not isinstance(command, dict) or not isinstance(command.get('action'), str):
                raise ValueError('Нужно имя команды action')
            future = self.server.session.submit(command)
            try:
                result = future.result(timeout=10)
            except concurrent.futures.TimeoutError:
                cancelled = future.cancel()
                self.reply(503, dict(error='Команда отменена в очереди' if cancelled else 'Команда ещё выполняется; обновите состояние'))
                return
            self.reply(200 if result['ok'] else 400, result)
        except (ValueError, UnicodeError, OSError) as error:
            self.reply(400, dict(error=str(error)))


def main():
    p = argparse.ArgumentParser(description='Band 9 browser debugger for a local network')
    p.add_argument('--host', default='0.0.0.0')
    p.add_argument('--port', type=int, default=8765)
    p.add_argument('--open', action='store_true')
    args = p.parse_args()
    session = MachineSession()
    token = secrets.token_urlsafe(24)
    try:
        server = DebugServer((args.host, args.port), session, token)
    except OSError:
        # Double-clicking the launcher while the server is running opens that
        # session instead of resetting it or starting a second CPU.
        try:
            previous = json.loads((HERE / 'results/browser-session.json').read_text(encoding='utf-8'))
            link = next(x for x in previous['links'] if x.startswith(f'http://127.0.0.1:{args.port}/#key='))
            saved_key = link.split('#key=', 1)[1]
            request = Request(f'http://127.0.0.1:{args.port}/api/connect', headers={'Authorization': 'Bearer ' + saved_key})
            with build_opener(ProxyHandler({})).open(request, timeout=2) as response:
                info = json.load(response)
            if not isinstance(info.get('addresses'), list):
                raise ValueError('Not a debugger server')
        except (OSError, ValueError, KeyError, StopIteration):
            raise SystemExit(f'Cannot bind {args.host}:{args.port}. Choose another --port or stop the process using it.') from None
        print('The debugger is already running. Reusing the existing machine.', flush=True)
        if args.open:
            webbrowser.open(link)
        return
    session.start()
    port = server.server_address[1]
    links = [f'{address}#key={token}' for address in server.addresses]
    print('Band 9 browser debugger. Keep this computer and this process running.', flush=True)
    for link in links:
        print(link, flush=True)
    print('All browsers share one machine. Ctrl+C stops the server.', flush=True)
    (HERE / 'results').mkdir(exist_ok=True)
    (HERE / 'results/browser-session.json').write_text(json.dumps(dict(links=links, port=port), indent=2), encoding='utf-8')
    if args.open:
        webbrowser.open(f'http://127.0.0.1:{port}/#key={token}')
    try:
        server.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        pass
    finally:
        session.closed.set()
        server.server_close()
        session.join(timeout=5)


if __name__ == '__main__':
    main()
