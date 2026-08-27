"""HTTP surface, one blueprint per area of the UI.

URLs are unchanged from when these lived in app.py — the web UI calls them
literally and nothing uses url_for — but Flask endpoint names are now
blueprint-qualified (display.index rather than index).
"""

from splitflap.web import (
    apps,
    display,
    hardware,
    network,
    notify,
    playlists,
    search,
    system,
    tuning,
)

BLUEPRINTS = (
    display.bp,
    hardware.bp,
    tuning.bp,
    playlists.bp,
    apps.bp,
    search.bp,
    network.bp,
    notify.bp,
    system.bp,
)


def register(flask_app):
    for blueprint in BLUEPRINTS:
        flask_app.register_blueprint(blueprint)
