"""Friends-beta worker: claim a batch, search it, upload a small result, delete the light curves.

``rhexas worker --dashboard`` and ``rhexas worker-dashboard`` serve a page from
``dashboard.sqlite`` in the worker cache. That page does not call the coordinator
and does not show the invite token.

``link_this_computer`` is the device login an installer or setup screen can call.
"""

from .login import link_this_computer, login_device, save_worker_token, token_path

__all__ = ["link_this_computer", "login_device", "save_worker_token", "token_path"]
