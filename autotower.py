"""Aktifkan semua jadwal NH: python autotower.py."""
import os
from schedule_function import manage


if __name__ == '__main__':
    manage('install')
    target = 'Windows Task Scheduler' if os.name == 'nt' else 'Linux crontab'
    print(f'Semua jadwal aktif sesuai jam WIB ({target}). Terminal boleh ditutup; server/PC harus tetap nyala.')
