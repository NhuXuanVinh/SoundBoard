"""Run with the virtual environment's Python, or double-click start.bat."""
import sys


if __name__ == '__main__':
    try:
        from soundboard.ui import run
    except ImportError as exc:
        print(f'Missing dependency: {exc}\nInstall with: python -m pip install -r requirements.txt')
        sys.exit(1)
    sys.exit(run())
