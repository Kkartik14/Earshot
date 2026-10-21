"""Optional framework adapters.

Importing :mod:`earshot` never imports optional framework dependencies. Adapter
modules use duck-typed source records so their mapping logic remains testable
without installing a voice runtime.
"""

from .livekit import LiveKitAdapter
from .pipecat import PipecatAdapter
from .tvic import (
    TVICObservation,
    TVICObservationError,
    TVICRuntimeAdapter,
    TVICRuntimeObservationSink,
    ingest_tvic_observations,
)

__all__ = [
    "LiveKitAdapter",
    "PipecatAdapter",
    "TVICObservation",
    "TVICObservationError",
    "TVICRuntimeAdapter",
    "TVICRuntimeObservationSink",
    "ingest_tvic_observations",
]
