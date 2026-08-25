import json
import os

SETTINGS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'downloader_settings.json')

class Settings:
    def __init__(self):
        self.thread_count = 8
        self.speed_limit = 0
        self.save_directory = os.path.normpath(os.path.expanduser('~/Downloads'))
        self.load()

    def load(self):
        if not os.path.exists(SETTINGS_FILE):
            return
        try:
            with open(SETTINGS_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
            self.thread_count = data.get('thread_count', 8)
            self.speed_limit = data.get('speed_limit', 0)
            path = data.get('save_directory', os.path.expanduser('~/Downloads'))
            self.save_directory = os.path.normpath(path)
        except (json.JSONDecodeError, OSError):
            pass

    def save(self):
        data = {
            'thread_count': self.thread_count,
            'speed_limit': self.speed_limit,
            'save_directory': os.path.normpath(self.save_directory),
        }
        with open(SETTINGS_FILE, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2)
