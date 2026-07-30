"""Mounts the Dash dashboard onto the gateway's Flask app (one process, no
separate deployment — see design/dashboard_and_tools.md). Called by
gateway/app/main.py in production and by run_dev.py during WS-D development.
"""

import os

from dash import Dash
from flask import Flask

from . import callbacks, layout


def create_dash(flask_app: Flask) -> Dash:
    app = Dash(
        __name__,
        server=flask_app,
        url_base_pathname="/",
        assets_folder=os.path.join(os.path.dirname(__file__), "assets"),
        title="Cough Monitor",
    )
    app.layout = layout.make_layout()
    callbacks.register(app)
    return app
