"""NH cron equivalent for Windows, all schedules in WIB (UTC+7).

python schedule_function.py install   # register this user's Task Scheduler jobs
python schedule_function.py list
python schedule_function.py run towers|ramen|beast
python schedule_function.py uninstall
"""
import argparse
from contextlib import contextmanager, redirect_stdout, redirect_stderr
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

from dotenv import dotenv_values
from login_api import NoRedirect

ROOT = Path(__file__).resolve().parent
WIB = timezone(timedelta(hours=7))
# Cron labels: (task name, purpose, job, WIB times, weekday restriction).
SCHEDULES = (
    ('NH-Auto-Ramen', 'Claim free ramen stamina daily at 12:01 and 18:01 WIB', 'ramen', ('12:01', '18:01'), ()),
    ('NH-Auto-Towers', 'GST then SNT (Quit after each) daily at 01:00 WIB', 'towers', ('01:00',), ()),
    ('NH-Auto-GNW', 'Great Ninja War daily at 02:00 WIB', 'gnw', ('02:00',), ()),
    ('NH-Auto-Beast-20', 'Tailed Beast daily at 20:00 WIB; keep current team, wait cooldown', 'beast', ('20:00',), ()),
    ('NH-Auto-Beast-Weekend-15', 'Extra Tailed Beast Saturday/Sunday at 15:00 WIB', 'beast', ('15:00',), ('Saturday', 'Sunday')),
)
NS = 'http://schemas.microsoft.com/windows/2004/02/mit/task'
ET.register_namespace('', NS)


def task_xml(schedule, sid):
    name, label, job, times, days = schedule
    task = ET.Element('{%s}Task' % NS, version='1.2')

    def node(parent, name, text=None):
        child = ET.SubElement(parent, '{%s}%s' % (NS, name))
        child.text = text
        return child

    info = node(task, 'RegistrationInfo')
    node(info, 'Description', label + '; project: ' + str(ROOT))
    triggers = node(task, 'Triggers')
    date = datetime.now(WIB).date().isoformat()
    for at in times:
        trigger = node(triggers, 'CalendarTrigger')
        node(trigger, 'StartBoundary', date + 'T' + at + ':00+07:00')
        node(trigger, 'Enabled', 'true')
        calendar = node(trigger, 'ScheduleByWeek' if days else 'ScheduleByDay')
        if days:
            node(calendar, 'WeeksInterval', '1')
            weekdays = node(calendar, 'DaysOfWeek')
            for day in days:
                node(weekdays, day)
        else:
            node(calendar, 'DaysInterval', '1')
    principals = node(task, 'Principals')
    principal = node(principals, 'Principal')
    principal.set('id', 'CurrentUser')
    node(principal, 'UserId', sid)
    node(principal, 'LogonType', 'InteractiveToken')
    node(principal, 'RunLevel', 'LeastPrivilege')
    settings = node(task, 'Settings')
    for key, value in (
        ('MultipleInstancesPolicy', 'IgnoreNew'),
        ('DisallowStartIfOnBatteries', 'false'), ('StopIfGoingOnBatteries', 'false'),
        ('StartWhenAvailable', 'false'), ('RunOnlyIfNetworkAvailable', 'true'),
        ('Enabled', 'true'), ('WakeToRun', 'true'), ('ExecutionTimeLimit', 'PT0S'),
    ):
        node(settings, key, value)
    actions = node(task, 'Actions')
    actions.set('Context', 'CurrentUser')
    execute = node(actions, 'Exec')
    node(execute, 'Command', sys.executable)
    node(execute, 'Arguments', '"%s" run %s' % (ROOT / 'schedule_function.py', job))
    node(execute, 'WorkingDirectory', str(ROOT))
    return ET.tostring(task, encoding='unicode')


@contextmanager
def account_lock():
    # ponytail: one account lock; per-account locks only if multiple .env files are supported.
    try:
        import msvcrt
        windows = True
    except ImportError:
        import fcntl
        windows = False
    with (ROOT / 'schedule.lock').open('a+b') as lock:
        if not lock.tell():
            lock.write(b'0')
            lock.flush()
        deadline = time.monotonic() + 300
        while True:
            lock.seek(0)
            try:
                if windows:
                    msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise TimeoutError('Another scheduled account job is busy.')
                time.sleep(1)
        try:
            yield
        finally:
            lock.seek(0)
            if windows:
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def notify_discord(label, status, detail=''):
    try:
        url = (dotenv_values(ROOT / '.env').get('DISCORD_WEBHOOK_URL') or '').strip()
        if not url:
            return
        target = urllib.parse.urlsplit(url)
        if (target.scheme != 'https' or target.hostname not in (
                'discord.com', 'discordapp.com', 'canary.discord.com', 'ptb.discord.com')
                or target.username or target.password or target.port not in (None, 443)
                or not re.fullmatch(r'/api(?:/v\d+)?/webhooks/\d+/[A-Za-z0-9_-]+/?', target.path)):
            raise ValueError('Invalid Discord webhook URL')
        content = '[%s WIB] %s — %s' % (datetime.now(WIB).strftime('%Y-%m-%d %H:%M:%S'), label, status)
        if detail:
            content += '\n' + detail
        body = json.dumps({'content': content[:1900], 'allowed_mentions': {'parse': []}}).encode('utf-8')
        request = urllib.request.Request(url, data=body, headers={
            'Content-Type': 'application/json', 'User-Agent': 'NH-Auto-Tower/1.0'}, method='POST')
        with urllib.request.build_opener(NoRedirect()).open(request, timeout=10) as response:
            if not 200 <= response.status < 300:
                raise RuntimeError('Discord rejected notification')
        print('Discord notification sent: %s.' % label, flush=True)
    except Exception as error:
        # Webhook URLs contain a secret token; never log the URL or exception message.
        # Notification failure must not fail/repeat a successful game action.
        print('Discord notification failed:', type(error).__name__, flush=True)


def client(args):
    env = os.environ.copy()
    for key in ('NH_EMAIL', 'NH_PASSWORD', 'NH_SERVER'):
        env.pop(key, None)  # Scheduled jobs always read the current project .env.
    result = subprocess.run([sys.executable, '-u', str(ROOT / 'login_api.py'), '--allow-http'] + args,
                            cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, errors='replace', timeout=10800)
    print(result.stdout, end='', flush=True)
    if result.returncode:
        raise RuntimeError('API job failed; no automatic request retry.')
    if args[0] != '--beast':
        stages = re.findall(r' -> (\d+)\.', result.stdout)
        detail = ('Total Stamina = ' if args[0] == '--ramen' else 'Stage terakhir: ') + stages[-1] if stages else ''
        status = 'Tidak tersedia / sudah diklaim' if 'stamina claim not available/already claimed' in result.stdout else 'Selesai'
        notify_discord(args[0].lstrip('-').upper(), status, detail)
    return result.stdout


def run_job(job):
    if job == 'towers':
        with account_lock():
            # Cron 01:00 WIB: normal battles; 3 total defeats per tower; end with Quit.
            client(['--gst', '--gst-floors', '1000', '--tower-quit'])
            client(['--snt', '--snt-floors', '1000', '--tower-quit'])
    elif job == 'gnw':
        with account_lock():
            # Cron 02:00 WIB: restart if available, fight until default team dead.
            client(['--gnw', '--gnw-run'])
    elif job == 'ramen':
        with account_lock():
            # Cron 12:00 and 18:00 WIB: server availability guards against repeat claims.
            client(['--ramen'])
    elif job == 'beast':
        # Cron 20:00 daily + 15:00 weekends. Release lock/session during the cooldown,
        # so the ramen job can run without disconnecting an active battle session.
        attacks = 0
        initial_retries = 3
        while True:
            with account_lock():
                output = client(['--beast', '--beast-attack'])
            statuses = [line[13:]
                        for line in output.splitlines() if line.startswith('BEAST_STATUS ')]
            if len(statuses) != 1:
                raise RuntimeError('Beast availability/cooldown unconfirmed; stop without retry.')
            state = json.loads(statuses[0])
            attacks += int(state.get('attacked') is True)
            if state.get('active') is False:
                if attacks == 0 and initial_retries > 0:
                    initial_retries -= 1
                    print('Beast event not active yet; waiting 20s for server start...', flush=True)
                    time.sleep(20)
                    continue
                print('Beast event no longer available; cron worker finished.', flush=True)
                notify_discord('TAILED BEAST', 'Selesai' if attacks else 'Event tidak tersedia',
                               'Attack terkonfirmasi: %s.' % attacks)
                return
            cooldown = state.get('cooldown')
            if state.get('active') is not True or type(cooldown) is not int or not 0 <= cooldown <= 86400:
                raise RuntimeError('Invalid Beast cooldown; stop without retry.')
            if not state.get('attacked') and cooldown == 0:
                raise RuntimeError('Beast attack unconfirmed; stop without retry.')
            wait = max(300 if state.get('attacked') else 1, cooldown)
            print('Waiting %s seconds; next login will re-check server cooldown.' % wait, flush=True)
            time.sleep(wait)
    else:
        raise ValueError('Unknown job')


def manage(action):
    if action == 'status':
        show_status()
        return
    if os.name == 'nt':
        if action == 'install':
            sid = subprocess.check_output(['powershell.exe', '-NoProfile', '-Command',
                                           '[System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value'],
                                          text=True).strip()
            for schedule in SCHEDULES:
                with tempfile.TemporaryDirectory() as temp:
                    path = Path(temp) / 'task.xml'
                    path.write_text(task_xml(schedule, sid), encoding='utf-8')
                    subprocess.run(['schtasks.exe', '/Create', '/TN', schedule[0], '/XML', str(path), '/F'], check=True)
                print(schedule[0] + ': ' + schedule[1])
        else:
            for schedule in SCHEDULES:
                args = ['/Query', '/TN', schedule[0], '/V', '/FO', 'LIST'] if action == 'list' else ['/Delete', '/TN', schedule[0], '/F']
                subprocess.run(['schtasks.exe'] + args, check=True)
    else:
        res = subprocess.run(['crontab', '-l'], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        current = res.stdout if res.returncode == 0 else ''
        existing = [line for line in current.splitlines() if not any(s[0] in line for s in SCHEDULES)]
        if action == 'install':
            weekdays = {'Monday': 1, 'Tuesday': 2, 'Wednesday': 3, 'Thursday': 4, 'Friday': 5, 'Saturday': 6, 'Sunday': 7}
            new_lines = list(existing)
            for name, label, job, times, days in SCHEDULES:
                if not days:
                    for at in times:
                        h, m = map(int, at.split(':'))
                        dt = datetime(2026, 1, 5, h, m, tzinfo=WIB).astimezone()
                        new_lines.append(f'{dt.minute} {dt.hour} * * * cd "{ROOT}" && {sys.executable} schedule_function.py run {job} # {name}')
                else:
                    for day in days:
                        ref_day = 4 + weekdays[day]
                        for at in times:
                            h, m = map(int, at.split(':'))
                            dt = datetime(2026, 1, ref_day, h, m, tzinfo=WIB).astimezone()
                            new_lines.append(f'{dt.minute} {dt.hour} * * {dt.strftime("%w")} cd "{ROOT}" && {sys.executable} schedule_function.py run {job} # {name}')
                print(name + ': ' + label)
            content = '\n'.join(new_lines).strip() + '\n'
            subprocess.run(['crontab', '-'], input=content, text=True, check=True)
        elif action == 'list':
            matched = [line for line in current.splitlines() if any(s[0] in line for s in SCHEDULES)]
            print('\n'.join(matched) if matched else 'Tidak ada jadwal NH aktif di crontab.')
        elif action == 'uninstall':
            content = '\n'.join(existing).strip() + '\n' if existing else ''
            if content:
                subprocess.run(['crontab', '-'], input=content, text=True, check=True)
            else:
                subprocess.run(['crontab', '-r'], stderr=subprocess.DEVNULL)
            print('Semua jadwal NH di crontab telah dihapus.')


def show_status():
    logs = ROOT / 'schedule_logs'
    print('=== RIWAYAT JALAN TERAKHIR (LOGS) ===')
    for job in ('ramen', 'towers', 'gnw', 'beast'):
        log_file = logs / f'{job}.log'
        if not log_file.exists():
            print(f'  {job.upper():<7}: Belum pernah jalan.')
            continue
        content = log_file.read_text(encoding='utf-8', errors='replace')
        entries = [e for e in content.split('\n[') if e.strip()]
        if not entries:
            print(f'  {job.upper():<7}: Log kosong.')
            continue
        last = '[' + entries[-1].lstrip('[')
        m_time = re.search(r'\[(.*?)\]', last)
        ts = m_time.group(1)[:19].replace('T', ' ') if m_time else '-'
        res = 'SUKSES' if 'CRON finished' in last else ('GAGAL' if 'CRON stopped' in last else 'TERPUTUS')
        print(f'  {job.upper():<7}: {res} ({ts} WIB)')
    print('\n=== DAFTAR JADWAL RUTIN (WIB) ===')
    for name, label, job, times, days in SCHEDULES:
        day_str = ' [' + ', '.join(days) + ']' if days else ' [Setiap Hari]'
        t_str = ', '.join(times)
        print(f'  {name:<26}: Jam {t_str} WIB{day_str}')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('install', 'list', 'uninstall', 'status', 'run'))
    parser.add_argument('job', nargs='?', choices=('ramen', 'towers', 'gnw', 'beast'))
    args = parser.parse_args()
    if args.action != 'run':
        manage(args.action)
        return 0
    if not args.job:
        parser.error('run requires ramen, towers, gnw, or beast.')
    logs = ROOT / 'schedule_logs'
    logs.mkdir(exist_ok=True)
    with (logs / (args.job + '.log')).open('a', encoding='utf-8', buffering=1) as log:
        with redirect_stdout(log), redirect_stderr(log):
            print('\n[%s] CRON %s starting.' % (datetime.now(WIB).isoformat(), args.job), flush=True)
            try:
                run_job(args.job)
            except Exception as error:
                # Do not expose URLs/credentials from network exceptions in unattended logs.
                print('CRON stopped:', type(error).__name__, flush=True)
                notify_discord(args.job.upper(), 'Gagal / dihentikan', type(error).__name__)
                return 1
            print('CRON finished; no API session left open.', flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
