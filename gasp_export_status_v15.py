#!/usr/bin/env python3
"""Non-invasive console status monitor for gasp_project_export_v15.py.

The exporter is launched unchanged. Its stdout/stderr go directly to an append-only
log file; this wrapper tails the file and renders a low-frequency status line.
There are NO MCP calls and NO scanning of Content/ or registry/ while monitoring.
"""
from __future__ import annotations

import argparse
from collections import deque
from datetime import datetime
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import time


RE_PENDING = re.compile(r'^\[Registry\] pending (\d+)/(\d+)')
RE_REG = re.compile(r'^\[Registry\] processed (\d+)/(\d+)')
RE_EXCLUDED = re.compile(r'^\[Registry\] excluded (\d+) OFPA external packages')
RE_BP_TOTAL = re.compile(r'^\[Blueprints\] discovered (\d+) candidates')
RE_BP = re.compile(r'^\[Blueprints\] (\d+)/(\d+) (\S+) (.+)$')
RE_GRAPH = re.compile(r'^\[BlueprintGraph\] (\d+)/(\d+) (\S+) (.+)$')


def parse_line(state, line, now=None):
    """Only parse known console events; never reinterpret them as MCP state."""
    now = time.monotonic() if now is None else now
    line = line.strip()
    if not line:
        return
    state['last_message_at'] = now
    state['last_line'] = line
    state['tail'].append(line)

    if line.startswith('[Filesystem]'):
        state['phase'] = 'Файловая инвентаризация'
        state['done'] = state['total'] = None
    elif line.startswith('[MCP]'):
        state['phase'] = 'Подключение MCP'
    elif line.startswith('[Identity]'):
        state['phase'] = 'Проверка соответствия проекта'
    elif line.startswith('[Plugins]'):
        state['phase'] = 'Обнаружение плагинов'
    elif (m := RE_EXCLUDED.match(line)):
        state['external_excluded'] = int(m.group(1))
    elif line.startswith('[Registry] failed asset '):
        state['soft_errors'] += 1
        state['last_problem'] = line
    elif (m := RE_PENDING.match(line)):
        state['phase'] = 'Registry'
        state['done'] = 0
        state['total'] = int(m.group(1))
        state['cached'] = int(m.group(2)) - state['total']
    elif (m := RE_REG.match(line)):
        state['phase'] = 'Registry'
        state['done'], state['total'] = int(m.group(1)), int(m.group(2))
    elif (m := RE_BP_TOTAL.match(line)):
        state['latest_graph'] = None
        state['phase'] = 'Blueprint'
        state['done'] = 0
        state['total'] = int(m.group(1))
        state['cached'] = None
    elif (m := RE_BP.match(line)):
        state['latest_graph'] = None
        state['phase'] = 'Blueprint'
        state['done'], state['total'] = int(m.group(1)), int(m.group(2))
        state['latest_asset'] = m.group(4)
        if m.group(3).lower() in ('partial', 'failed'):
            state['soft_errors'] += 1
            state['last_problem'] = line
    elif (m := RE_GRAPH.match(line)):
        # Keepalive + progress within individual large Blueprint.
        state['latest_graph'] = (int(m.group(1)), int(m.group(2)))
    elif line.startswith('[Archive]'):
        state['phase'] = 'Архивация ZIP'
        state['done'] = state['total'] = None
    elif line.startswith('[DONE]'):
        state['phase'] = 'Завершение'
        state['done'] = state['total'] = None
        state['done_marker'] = True
    elif line.startswith('ERROR:') or line.startswith('Traceback (most recent call last):'):
        state['last_problem'] = line


def new_state():
    return {'phase': 'Запуск экспортёра', 'done': None, 'total': None, 'cached': None, 'external_excluded': 0,
            'last_message_at': time.monotonic(), 'last_line': '', 'tail': deque(maxlen=12),
            'latest_asset': None, 'soft_errors': 0, 'last_problem': None, 'done_marker': False}


def brief_seconds(value):
    value = max(0, int(value))
    hours, rest = divmod(value, 3600)
    minutes, seconds = divmod(rest, 60)
    return f'{hours:02d}:{minutes:02d}:{seconds:02d}'


def status_line(state, alive, elapsed, silent, warn_after=90, width=100):
    phase = state['phase']
    done, total = state['done'], state['total']
    if isinstance(total, int) and total > 0 and isinstance(done, int):
        ratio = max(0.0, min(done / total, 1.0))
        steps = int(ratio * 14)
        bar = '[' + '#' * steps + '-' * (14 - steps) + ']'
        p = f'{bar} {done}/{total} ({ratio:.1%})'
        if state['cached'] is not None and phase == 'Registry':
            p += f' | кэш: {state["cached"]}'
            if state.get('external_excluded'):
                p += f' | OFPA: {state["external_excluded"]} отдельно'
        if phase == 'Blueprint' and state.get('latest_graph'):
            a,b = state['latest_graph']
            p += f' | граф {a}/{b}'
    elif total == 0:
        p = 'очередь пуста'
    else:
        p = 'прогресс внутри операции недоступен'
    note = f'нет сообщений {brief_seconds(silent)}'
    if alive:
        note += ' | процесс жив'
        if silent >= warn_after:
            note += ' | ожидание не доказывает зависание'
    prefix = '[ДОЛГОЕ ОЖИДАНИЕ; НЕ СБОЙ] ' if alive and silent >= warn_after else ''
    body = f'{prefix}{phase} | {p} | прошло {brief_seconds(elapsed)} | {note}'
    # Non-ASCII may have console-dependent widths; truncate conservatively.
    return body[:max(20, width - 3)]


def export_summary_path(script_args, cwd):
    try:
        ix = script_args.index('--output')
        return Path(script_args[ix + 1]).resolve() / 'export_summary.json'
    except (ValueError, IndexError):
        pass
    try:
        ix = script_args.index('--project')
        return cwd / (Path(script_args[ix + 1]).resolve().name + '_EXPORT') / 'export_summary.json'
    except (ValueError, IndexError):
        return None


def read_counts(path):
    if path is None or not path.is_file():
        return None
    try:
        summary = json.loads(path.read_text(encoding='utf-8'))
        rows = {kind: summary.get(kind, {}) for kind in ('registry', 'blueprints')}
        return {kind: {status: int(rows[kind].get(status, 0)) for status in ('complete', 'partial', 'failed')}
                for kind in rows}
    except (OSError, ValueError, TypeError):
        return None


def child_stop(proc):
    if proc.poll() is not None:
        return
    print('\n[МОНИТОР] Ctrl+C: сигнал остановки экспортёру. Уже сохранённые JSON остаются.', flush=True)
    try:
        if os.name == 'nt':
            proc.send_signal(signal.CTRL_BREAK_EVENT)
        else:
            os.killpg(proc.pid, signal.SIGINT)
    except (OSError, ValueError, ProcessLookupError):
        try:
            proc.terminate()
        except OSError:
            pass
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        # Do not silently kill Unreal MCP operations or destroy worker state.
        print('[МОНИТОР] Экспортёр не завершился за 10 с; он ещё работает. PID:', proc.pid, flush=True)
        print('[МОНИТОР] Остановите процесс вручную при необходимости.', flush=True)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description='Лёгкий индикатор для существующего экспортёра v1.5: не обращается к MCP и не меняет код экспортёра.',
        epilog='Все неизвестные аргументы передаются gasp_project_export_v15.py без изменений.')
    parser.add_argument('--exporter', default=str(Path(__file__).with_name('gasp_project_export_v15.py')),
                        help='Путь к существующему экспортёру (по умолчанию v1.5 рядом с монитором)')
    parser.add_argument('--monitor-interval', type=float, default=3.0,
                        help='Обновление строки статуса, секунды (по умолчанию 3)')
    parser.add_argument('--warning-after', type=int, default=90,
                        help='Через сколько секунд отсутствия вывода показывать предупреждение (по умолчанию 90)')
    parser.add_argument('--log-file', help='Необязательный путь к журналу; по умолчанию файл с отметкой времени рядом с монитором')
    opts, forwarded = parser.parse_known_args(argv)
    exporter = Path(opts.exporter).resolve()
    if not exporter.is_file():
        parser.error(f'Экспортёр не найден: {exporter}')
    if not forwarded:
        parser.error('Передайте аргументы экспортёра, например --project "D:\\...\\MyGameProject_GAS_Test" --phase all')
    if opts.monitor_interval < 1:
        parser.error('--monitor-interval должен быть >= 1 секунды')
    if opts.warning_after < 1:
        parser.error('--warning-after должен быть >= 1 секунды')

    cwd = Path.cwd().resolve()
    tag = datetime.now().strftime('%Y%m%d_%H%M%S')
    logfile = Path(opts.log_file).resolve() if opts.log_file else Path(__file__).resolve().parent / f'GASP_export_{tag}.log'
    logfile.parent.mkdir(parents=True, exist_ok=True)
    summary_file = export_summary_path(forwarded, cwd)
    # Track the previous summary timestamp: do not report yesterday's errors as today's.
    previous_summary_mtime = summary_file.stat().st_mtime_ns if summary_file and summary_file.is_file() else None
    env = os.environ.copy()
    env['PYTHONIOENCODING'] = 'utf-8'
    flags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == 'nt' else 0
    argv_child = [sys.executable, str(exporter), *forwarded]

    print('[МОНИТОР] Экспортёр:', exporter.name, flush=True)
    print('[МОНИТОР] Лог:', logfile, flush=True)
    print('[МОНИТОР] Индикатор читает только лог; MCP и файлы проекта дополнительно не опрашивает.', flush=True)
    print('[МОНИТОР] Ctrl+C — остановить запуск; длительное ожидание не означает зависание.', flush=True)

    state = new_state()
    start = time.monotonic()
    last_render = -opts.monitor_interval
    last_length = 0
    interrupted = False
    returncode = None
    proc = None
    try:
        with logfile.open('wb') as output:
            proc = subprocess.Popen(argv_child, stdout=output, stderr=subprocess.STDOUT,
                                    env=env, cwd=cwd, creationflags=flags,
                                    start_new_session=(os.name != 'nt'))
            with logfile.open('r', encoding='utf-8', errors='replace') as reader:
                while True:
                    while True:
                        line = reader.readline()
                        if not line:
                            break
                        parse_line(state, line)
                    now = time.monotonic()
                    alive = proc.poll() is None
                    if now - last_render >= opts.monitor_interval or not alive:
                        cols = shutil.get_terminal_size((115, 20)).columns
                        message = status_line(state, alive, now - start,
                                              now - state['last_message_at'], opts.warning_after,
                                              max(56, cols))
                        if sys.stdout.isatty():
                            print('\r' + message.ljust(last_length), end='', flush=True)
                            last_length = len(message)
                        else:
                            print('[СТАТУС]', message, flush=True)
                        last_render = now
                    if not alive:
                        # Drain final output after process exit before reporting result.
                        while True:
                            line = reader.readline()
                            if not line:
                                break
                            parse_line(state, line)
                        returncode = proc.returncode
                        break
                    time.sleep(min(opts.monitor_interval, 1.0))
    except KeyboardInterrupt:
        interrupted = True
        if proc is not None:
            child_stop(proc)
            returncode = proc.poll()
    print('\n', end='')
    if interrupted:
        print('[ОСТАНОВЛЕНО ПОЛЬЗОВАТЕЛЕМ] Лог:', logfile, flush=True)
        return 130
    if returncode != 0:
        print(f'[ОСТАНОВЛЕНО С ОШИБКОЙ] Код завершения: {returncode}.', flush=True)
        if state['last_problem']:
            print('[ПРИЧИНА]', state['last_problem'], flush=True)
        print('[ПОСЛЕДНИЕ СТРОКИ ЖУРНАЛА]', flush=True)
        for line in list(state['tail'])[-7:]:
            print('  ' + line[:350], flush=True)
        print('[ЛОГ]', logfile, flush=True)
        return returncode if isinstance(returncode, int) and returncode > 0 else 1
    fresh_summary = summary_file and summary_file.is_file() and (
        previous_summary_mtime is None or summary_file.stat().st_mtime_ns != previous_summary_mtime)
    counts = read_counts(summary_file) if fresh_summary else None
    if counts:
        partial = sum(item['partial'] for item in counts.values())
        failed = sum(item['failed'] for item in counts.values())
        print('[ИТОГ]', json.dumps(counts, ensure_ascii=False), flush=True)
        if partial or failed:
            print(f'[ЗАВЕРШЕНО С ОШИБКАМИ ДАННЫХ] Частичных: {partial}; неудачных: {failed}.', flush=True)
        else:
            print('[ЗАВЕРШЕНО] Экспортёр вернул код 0; сводка без ошибок.', flush=True)
    else:
        print('[ЗАВЕРШЕНО] Экспортёр вернул код 0.', flush=True)
        if not state['done_marker']:
            print('[ПРИМЕЧАНИЕ] Маркер [DONE] не получен (например, при --phase inventory).', flush=True)
    if state['soft_errors']:
        print(f'[ПРЕДУПРЕЖДЕНИЕ] В логе было {state["soft_errors"]} сообщений об ошибках отдельных ассетов/Blueprint.', flush=True)
    print('[ЛОГ]', logfile, flush=True)
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (OSError, ValueError) as exc:
        print(f'[ОШИБКА МОНИТОРА] {exc}', file=sys.stderr, flush=True)
        sys.exit(2)
