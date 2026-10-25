"""Browser service: renders untrusted URLs in a real Google Chrome (headed, under Xvfb) for ``read_url``.

Runs in its own image (``browser/Dockerfile``) and only imports the SSRF guard from the bot package, so it must
not import :mod:`yggdrasil.config` or anything that needs the bot's settings.
"""
