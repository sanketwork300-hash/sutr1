"""Hard limits for untrusted OpenAPI input (see Sutr spec §55).

Imported specifications are attacker-controllable documents; every stage of
the pipeline is bounded so a pathological spec cannot exhaust the server.
"""

# Raw document size (upload, paste, or URL fetch).
MAX_SPEC_BYTES = 2 * 1024 * 1024  # 2 MiB

# $ref resolution.
MAX_REF_DEPTH = 50

# Normalization / compilation.
MAX_OPERATIONS = 300
MAX_GENERATED_TOOLS = 200
MAX_SCHEMA_DEPTH = 40
MAX_PARAMS_PER_TOOL = 80

# URL import.
URL_FETCH_TIMEOUT_SECONDS = 15.0
