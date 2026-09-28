"""Desktop lifetime owns both localhost services; no startup task or service."""
import argparse
import os
import sys
import threading
from pathlib import Path

from .app import App, PROJECT


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', default=str(PROJECT / '.local' / 'app'))
    parser.add_argument('--runtime', default=os.environ.get('LEDGERLENS_EZ_RUNTIME',
                        str(PROJECT / '.local' / 'runtime' / 'ezbookkeeping')))
    parser.add_argument('--serve', action='store_true', help='Development only: no desktop window')
    parser.add_argument('--port', type=int, default=18083)
    parser.add_argument('--ez-port', type=int, default=18084)
    args = parser.parse_args()
    app = App(args.root, args.runtime, args.port, args.ez_port)
    try:
        url = app.start()
        if args.serve:
            print(url, flush=True)
            threading.Event().wait()
        else:
            import webview
            window = webview.create_window('LedgerLens', url, width=1180, height=820, min_size=(760, 600))
            app.window = window
            # Private mode keeps browser authentication out of a normal browser profile.
            webview.start(gui='edgechromium', private_mode=True)
    except KeyboardInterrupt:
        pass
    except Exception as exc:
        if args.serve:
            raise
        import ctypes
        ctypes.windll.user32.MessageBoxW(0, str(exc), 'LedgerLens 启动失败', 0x10)
    finally:
        app.stop()


if __name__ == '__main__':
    main()
