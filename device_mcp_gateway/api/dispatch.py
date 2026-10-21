# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Ben Wold. All rights reserved.
# Licensed under the PolyForm Noncommercial License 1.0.0. See LICENSE in the project root for details.
"""Helpers shared by the device and fleet MCP routes.

The gateway identity tag and the "registered but not serving" check live here so neither
route module depends on the other or on ``main``. (The F6 lost-call watcher that also lived
here served the HTTP+SSE transport, where a result arrived on a stream held elsewhere; on
Streamable HTTP the waiting request itself owns the deadline, so it went with SSE in 0.4.0.)
"""

from __future__ import annotations

import os
import uuid
from typing import Any

from fastapi import HTTPException

# Gateway instance ID — tags the sessions and stream entries this replica creates.
_GATEWAY_ID = os.getenv("GATEWAY_ID", str(uuid.uuid4()))

#: Seconds a caller is told to wait before retrying a registered device that is not serving.
#: Short on purpose: the usual cause is a device moving between workers, which a graceful
#: hand-off completes in seconds.
INACTIVE_DEVICE_RETRY_AFTER = 5


def require_serving_device(device: Any, hostname: str) -> Any:
    """Return ``device`` if it can take MCP traffic now; otherwise raise the accurate error.

    These two cases used to share one 404, "Device pod not found or not active":

    - **Not registered** — 404. There is nothing to retry.
    - **Registered, no active pod** — 503 with ``Retry-After``. The device exists and is
      expected back: it is moving between workers (a rolling update, a worker failover), its
      pod is being replaced, or its target is unreachable and it is re-homed when it
      returns. A 404 here told a client the device was gone when it was being moved — and on
      the Streamable HTTP endpoint, 404 is also the spec's signal that a *session* expired,
      so a conforming client re-initialised into the same 404 instead of backing off.
    """
    if device is None:
        raise HTTPException(status_code=404, detail=f"Device '{hostname}' is not registered")
    if not device.pod_active:
        raise HTTPException(
            status_code=503,
            detail=(
                f"Device '{hostname}' is registered but not serving right now — it may be moving "
                f"between workers or its target may be unreachable. Retry shortly; "
                f"GET /v1/devices/{hostname} shows reachable and spawn_error."
            ),
            headers={"Retry-After": str(INACTIVE_DEVICE_RETRY_AFTER)},
        )
    return device
