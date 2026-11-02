"""CodePro combined runner for Render (single process).

Flask API runs in a daemon thread, the Telegram bot polls in the MAIN thread
(PTB cannot run in a non-main thread). Self-healing loop restarts polling
after transient network failures.
"""
import os
import threading
import time

RUN_BOT = os.getenv("RUN_BOT", "1") == "1"

os.environ.pop("no_proxy", None)
os.environ.pop("NO_PROXY", None)


def _run_api():
    from backend import app as flask_app
    port = int(os.getenv("PORT", "5000"))
    print(f"API listening on 0.0.0.0:{port}", flush=True)
    flask_app.run(host="0.0.0.0", port=port)


if __name__ == "__main__":
    api_thread = threading.Thread(target=_run_api, daemon=True)
    api_thread.start()
    if not RUN_BOT:
        api_thread.join()
    else:
        import bot as botmod
        while True:
            try:
                botmod.main()
            except KeyboardInterrupt:
                break
            except SystemExit:
                raise
            except Exception as e:  # noqa: BLE001 - self-healing restart
                print(f"bot crashed ({e}), restarting in 15s", flush=True)
                time.sleep(15)
