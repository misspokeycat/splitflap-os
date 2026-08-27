"""Splitflap OS server — entry point and composition root.

systemd starts the service as:

    python -c "import app; app.app.run(host='0.0.0.0', port=80)"

so this module must keep its name and keep exposing ``app``. Everything it is
built from lives in the splitflap package.

Importing this module brings the server up: settings load, the transport
opens, apps register, the display/schedule/trigger/network loops start and the
broker connects. Set SPLITFLAP_NO_BACKGROUND_TASKS=1 to import it without any
of that — see splitflap.tasks.
"""

import logging

from flask import Flask

# Import order here is the boot order. Each module does its own setup as it is
# imported — the transport opens the connection, plugins build the app registry
# — and Python guarantees each finishes before the next begins.
# splitflap.startup comes last on purpose: it homes the display and connects to
# the broker, and both need everything above them to already exist.
from splitflap import network, playlist, scheduler, triggers  # noqa: F401
from splitflap import startup  # noqa: F401
from splitflap.web import register

app = Flask(__name__)
register(app)


if __name__ == '__main__':
    logging.info("Web UI running on 0.0.0.0:80")
    app.run(host='0.0.0.0', port=80)
