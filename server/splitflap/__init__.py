"""Splitflap OS server internals.

``server/app.py`` stays the entry point systemd imports; everything it is
built from lives here.

Logging is configured here rather than in app.py because several modules log
while being imported — transport.py reports the serial connection as it opens
it. The first logging call installs a default WARNING-level handler, which
makes a later basicConfig() a silent no-op and drops every INFO line the
service writes. Configuring it in the package __init__ guarantees it happens
before any submodule runs.
"""

import logging

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(message)s')
