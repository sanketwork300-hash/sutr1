import atexit

from posthog import Posthog

from sutr.config import settings

posthog_client = Posthog(
    project_api_key=settings.posthog_project_token,
    host=settings.posthog_host,
    enable_exception_autocapture=True,
    # Without a key (self-hosted default, tests) events would queue up and the
    # background consumer would 401 on every flush.
    disabled=not settings.posthog_project_token,
)

atexit.register(posthog_client.shutdown)
