"""Sinks: every outbound connection, each off until the owner approves it (FR-65).

``spec`` declares what a sink may request (``SinkSpec``) and its digest, ``gate`` says
whether a sink is enabled, and ``store`` writes the derived ``sink_spec`` table. The
only places that open a socket (``rheo_finance.http`` and, later, the IMAP source) ask
the gate before every request.
"""
