"""Rate limiting middleware for the KnowCode FastAPI server.

Implements tiered rate limits to protect the API from runaway agent loops:
- Standard endpoints (health, stats, search, context, query): 60 requests/minute
- Expensive endpoints (trace_calls, impact): 10 requests/minute

Uses slowapi with client IP-based keying.
"""

from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address
from fastapi import FastAPI

# --- Rate Limit Tiers ---
STANDARD_LIMIT = "60/minute"
EXPENSIVE_LIMIT = "10/minute"

# Create the limiter instance, keyed by client IP address
limiter = Limiter(key_func=get_remote_address)


def setup_rate_limiting(app: FastAPI) -> None:
    """Attach rate limiting middleware and error handler to the FastAPI app.

    Must be called after all routers have been included so that the limiter
    can decorate the route handlers.
    """
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)  # type: ignore[arg-type]
