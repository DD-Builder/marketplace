"""Shared test helpers.

There is no database fixture any more: the app's only durable state is JSON committed
next to the site, so tests use ``tmp_path`` directly.
"""
