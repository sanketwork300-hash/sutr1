"""Connected accounts: OAuth grants sutr holds on a user's behalf.

Two things need them, and they are the same thing underneath:

- **Spec sources.** Importing an OpenAPI document out of a private GitHub
  repository without asking anyone to paste a personal access token.
- **Deployment targets.** Running a generated MCP server in the user's own
  Google Cloud, Azure, or AWS account.

`providers.py` says what each provider is, `flow.py` runs the redirect grant,
`device.py` runs the AWS device grant, and `store.py` owns persistence plus
the single `access_token()` entry point every caller uses.
"""

from sutr.connections.errors import ConnectError
from sutr.connections.providers import (
    ALL_PROVIDERS,
    DEPLOY_PROVIDERS,
    SOURCE_PROVIDERS,
    OAuthProvider,
    callback_url,
    get_provider,
    is_configured,
)
from sutr.connections.store import (
    access_token,
    delete_connection,
    find_connection,
    list_connections,
    load_connection,
    metadata_of,
    save_connection,
    serialize,
)

__all__ = [
    "ALL_PROVIDERS",
    "DEPLOY_PROVIDERS",
    "SOURCE_PROVIDERS",
    "ConnectError",
    "OAuthProvider",
    "access_token",
    "callback_url",
    "delete_connection",
    "find_connection",
    "get_provider",
    "is_configured",
    "list_connections",
    "load_connection",
    "metadata_of",
    "save_connection",
    "serialize",
]
