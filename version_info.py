VERSION = "0.8.0"

# Helper to read changelog (VERSION.md) lazily
import os


def get_changelog():
    root = os.path.dirname(__file__)
    path = os.path.join(root, 'VERSION.md')
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return f.read()
    except Exception:
        return "Version history not available."
