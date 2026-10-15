"""Friends-beta worker: claim a batch, search it, upload a small result, delete the light curves.

``rhexas worker --dashboard`` and ``rhexas worker-dashboard`` serve a page from
``dashboard.sqlite`` in the worker cache. That page does not call the coordinator
and does not show the invite token.
"""
