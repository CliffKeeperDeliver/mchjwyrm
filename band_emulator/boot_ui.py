"""Native control panel for the real firmware boot runner; no simulated watch UI."""
import argparse
import json
import queue
import threading
import tkinter as tk
from tkinter import ttk

from boot_machine import BootMachine, HERE, load_profile


class BootWorker(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True)
        self.commands = queue.Queue()
        self.updates = queue.Queue(maxsize=1)
        self.closed = threading.Event()

    def publish(self, data):
        try:
            self.updates.get_nowait()
        except queue.Empty:
            pass
        self.updates.put_nowait(data)

    def run(self):
        machine = None
        running = False
        try:
            machine = BootMachine(load_profile(HERE / 'profiles/n66gl_2.3.98.json'), output=None)
            self.publish(dict(report=machine.report(), running=False))
            while not self.closed.is_set():
                notice = ''
                try:
                    was_running = running
                    action, value = self.commands.get(timeout=0 if running else 0.1)
                except queue.Empty:
                    action, value = None, None
                try:
                    if action == 'reset':
                        running = False
                        machine = BootMachine(load_profile(HERE / 'profiles/n66gl_2.3.98.json'), output=None)
                    elif action == 'run':
                        running = True
                    elif action == 'pause':
                        running = False
                    elif action == 'step':
                        running = False
                        machine.step()
                    elif action == 'save':
                        running = False
                        machine.save('manual')
                        notice = 'Состояние сохранено в памяти процесса.'
                    elif action == 'restore':
                        running = False
                        machine.restore('manual')
                        notice = 'Состояние восстановлено.'
                    elif action == 'breakpoint':
                        running = False
                        machine.set_breakpoint(int(value, 0))
                        notice = f'Точка останова: {value}'
                    elif action == 'gpio':
                        running = False
                        pin, level = (int(item, 0) for item in value)
                        machine.gpio.drive(pin, level)
                        notice = f'Виртуальный GPIO {pin} = {level}. Нажмите «Запуск» для обработки входа.'
                    elif action == 'adc':
                        running = False
                        channel, sample = (int(item, 0) for item in value)
                        machine.set_adc_input(channel, sample)
                        notice = f'Виртуальный АЦП {channel} = {sample}; применяется при следующем чтении прошивкой.'
                    elif action == 'export':
                        target = HERE / 'results/boot-ui.json'
                        target.parent.mkdir(exist_ok=True)
                        target.write_text(json.dumps(machine.report(), ensure_ascii=False, indent=2), encoding='utf-8')
                        notice = f'Отчёт: {target}'
                    if running:
                        report = machine.advance(30000, 300)
                        if report['status'] not in ('timeout', 'budget'):
                            running = False
                    if action or was_running or machine.status in machine.FAULTS:
                        self.publish(dict(report=machine.report(), running=running, notice=notice))
                except Exception as error:
                    running = False
                    self.publish(dict(report=machine.report(), running=False, notice=str(error)))
        except Exception as error:
            self.publish(dict(error=str(error)))


class BootPanel:
    def __init__(self, root):
        self.root = root
        self.worker = BootWorker()
        self.last_report = None
        root.title('Band 9 — выполнение прошивки 2.3.98')
        root.geometry('1080x740')
        root.minsize(850, 600)
        frame = ttk.Frame(root, padding=14)
        frame.pack(fill='both', expand=True)
        ttk.Label(frame, text='Band 9 · прошивка 2.3.98', font=('Segoe UI', 18, 'bold')).pack(anchor='w')
        ttk.Label(frame, text='Экспериментальный HLE-режим. Код ОС выполняется; часть аппаратных служб заменена.').pack(anchor='w', pady=(4, 12))
        toolbar = ttk.Frame(frame)
        toolbar.pack(fill='x')
        for label, command in [('Запуск', 'run'), ('Пауза', 'pause'), ('Шаг', 'step'),
                               ('Сброс', 'reset'), ('Сохранить', 'save'), ('Восстановить', 'restore'), ('Отчёт', 'export')]:
            ttk.Button(toolbar, text=label, command=lambda c=command: self.send(c)).pack(side='left', padx=(0, 5))
        self.status = tk.StringVar(value='Загрузка образа…')
        ttk.Label(frame, textvariable=self.status, font=('Consolas', 11)).pack(anchor='w', pady=12)
        bp = ttk.Frame(frame)
        bp.pack(fill='x', pady=(0, 10))
        ttk.Label(bp, text='Остановиться на адресе:').pack(side='left')
        self.address = ttk.Entry(bp, width=16)
        self.address.insert(0, '0x0c1404e8')
        self.address.pack(side='left', padx=8)
        ttk.Button(bp, text='Добавить', command=lambda: self.send('breakpoint', self.address.get())).pack(side='left')
        ttk.Label(bp, text='  Дисплей и интерфейс браслета пока не подключены.').pack(side='left')
        notebook = ttk.Notebook(frame)
        notebook.pack(fill='both', expand=True)
        task_tab = ttk.Frame(notebook, padding=10)
        notebook.add(task_tab, text='Задачи ОС')
        self.tasks = ttk.Treeview(task_tab, columns=('pid', 'name', 'state', 'entry', 'tcb'), show='headings', height=7)
        for key, label, width in [('pid', 'PID', 55), ('name', 'Имя из прошивки', 200), ('state', 'Состояние (код)', 120),
                                  ('entry', 'Точка входа', 140), ('tcb', 'TCB', 140)]:
            self.tasks.heading(key, text=label)
            self.tasks.column(key, width=width)
        self.tasks.pack(fill='x')
        self.progress = tk.Text(task_tab, height=13, wrap='word', font=('Consolas', 10))
        self.progress.pack(fill='both', expand=True, pady=(10, 0))
        self.console = self.text_tab(notebook, 'Журнал прошивки')
        self.details = self.text_tab(notebook, 'Регистры и трасса')
        self.services = self.text_tab(notebook, 'Замены оборудования')
        inputs = ttk.Frame(notebook, padding=10)
        notebook.add(inputs, text='Входы платы')
        ttk.Label(inputs, text='Тестовые сигналы, не показания браслета. Изменение входа ставит исполнение на паузу.').pack(anchor='w')
        for action, labels, defaults in [('gpio', ('GPIO 0–79', 'Уровень 0/1'), ('46', '1')),
                                          ('adc', ('Канал АЦП', 'Значение'), ('4', '600'))]:
            row = ttk.Frame(inputs)
            row.pack(fill='x', pady=8)
            entries = []
            for label, default in zip(labels, defaults):
                ttk.Label(row, text=label).pack(side='left')
                entry = ttk.Entry(row, width=10)
                entry.insert(0, default)
                entry.pack(side='left', padx=8)
                entries.append(entry)
            ttk.Button(row, text='Применить', command=lambda a=action, e=entries:
                       self.send(a, [item.get() for item in e])).pack(side='left')
        ttk.Label(inputs, text='АЦП 4 = 600 выбирает виртуальную ревизию платы 4 при загрузке.\nGPIO 46 настроен прошивкой на спадающий фронт: сначала 1, затем 0.').pack(anchor='w', pady=8)
        self.inputs = tk.Text(inputs, wrap='word', font=('Consolas', 10))
        self.inputs.pack(fill='both', expand=True)
        self.notice = tk.StringVar(value='Снимки хранятся до закрытия окна. Неизвестное оборудование останавливает выполнение.')
        ttk.Label(frame, textvariable=self.notice, wraplength=1000).pack(anchor='w', pady=(10, 0))
        root.protocol('WM_DELETE_WINDOW', self.close)
        self.worker.start()
        root.after(100, self.poll)

    def text_tab(self, notebook, title):
        page = ttk.Frame(notebook)
        notebook.add(page, text=title)
        widget = tk.Text(page, wrap='word', font=('Consolas', 10))
        scroll = ttk.Scrollbar(page, command=widget.yview)
        widget.configure(yscrollcommand=scroll.set)
        scroll.pack(side='right', fill='y')
        widget.pack(fill='both', expand=True)
        return widget

    def send(self, action, value=None):
        self.worker.commands.put((action, value))

    @staticmethod
    def replace(widget, text):
        widget.configure(state='normal')
        widget.delete('1.0', 'end')
        widget.insert('1.0', text)
        widget.configure(state='disabled')

    def poll(self):
        try:
            update = self.worker.updates.get_nowait()
            if 'error' in update:
                self.status.set(update['error'])
            else:
                r = self.last_report = update['report']
                activity = 'Выполняется' if update['running'] else 'Остановлено'
                self.status.set(f"{activity} · {r['status']} · PC {r['registers']['pc']} · {r['instructions']:,} инструкций")
                for row in self.tasks.get_children():
                    self.tasks.delete(row)
                for task in r['tasks']:
                    self.tasks.insert('', 'end', values=[task[k] for k in ('pid', 'name', 'state', 'entry', 'tcb')])
                lines = [f"✓ {m['stage']} — {m['instructions']:,}" for m in r['milestones']]
                lines += [f"\nПереходов исключений: {r['exceptions']['total_events']}"]
                if r['fault']:
                    lines += ['\nПричина остановки:', json.dumps(r['fault'], ensure_ascii=False, indent=2),
                              'Для продолжения после ошибки нужен сброс или сохранённое состояние.']
                self.replace(self.progress, '\n'.join(lines))
                self.replace(self.console, r['console'] or 'Вывод прошивки появится после старта C runtime.')
                self.replace(self.details, json.dumps(dict(registers=r['registers'], trace=r['trace_tail']), indent=2))
                self.replace(self.services, json.dumps(dict(services=r['hle_replacements'], clock=r['virtual_clock'],
                    factory=r['virtual_factory_sector'], limitations=r['limitations']), indent=2, ensure_ascii=False))
                self.replace(self.inputs, json.dumps(dict(adc=r['virtual_adc_inputs'], gpio=r['gpio']), indent=2, ensure_ascii=False))
                if update.get('notice'):
                    self.notice.set(update['notice'])
        except queue.Empty:
            pass
        if not self.worker.closed.is_set():
            self.root.after(100, self.poll)

    def close(self):
        self.worker.closed.set()
        self.root.destroy()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--smoke-test', action='store_true')
    args = parser.parse_args()
    root = tk.Tk()
    panel = BootPanel(root)
    if args.smoke_test:
        # Exercise widget creation, worker initialization and report rendering.
        def verify():
            if panel.last_report is None:
                root.after(100, verify)
                return
            if panel.last_report['instructions'] == 0:
                panel.send('step')
                root.after(100, verify)
                return
            print('UI smoke test passed', flush=True)
            panel.close()
        root.after(100, verify)
        root.after(15000, panel.close)
    root.mainloop()
    panel.worker.join(timeout=2)
    if args.smoke_test and (panel.last_report is None or panel.last_report['instructions'] != 1):
        raise SystemExit('UI smoke test failed')


if __name__ == '__main__':
    main()
