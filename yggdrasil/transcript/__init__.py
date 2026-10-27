"""Agent transcripts: the whole conversation of each invocation and background job, kept for the console.

``capture`` records pydantic-ai runs, ``omp`` converts background-job messages, ``notes`` collects what tools showed
the model (QQ messages, images), ``store`` persists it all. The format is in ``schema``.
"""
