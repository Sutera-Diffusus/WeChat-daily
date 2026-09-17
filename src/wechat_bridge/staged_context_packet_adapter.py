"""Compatibility/public name for the K2→K3 staged packet adapter.

The public integration contract uses ``staged_context_packet_adapter``.  The
implementation lives in :mod:`context_packet_stage_adapter` so older local
callers using that descriptive name remain source-compatible.
"""

from .context_packet_stage_adapter import (  # noqa: F401
    ADAPTER_PACKET_VERSION,
    ADAPTER_SCHEMA_VERSION,
    AdaptedContextPacket,
    AdaptedContextPacketResult,
    ContextPacketAdapterError,
    ContextPacketMapping,
    ContextPacketStageAdapter,
    ContextPacketStageOrchestrator,
    StagedContextPacketResult,
    adapt_context_packet,
    adapt_context_packet_with_mapping,
    analyze_context_packet,
)

__all__ = [
    "ADAPTER_PACKET_VERSION",
    "ADAPTER_SCHEMA_VERSION",
    "AdaptedContextPacket",
    "AdaptedContextPacketResult",
    "ContextPacketAdapterError",
    "ContextPacketMapping",
    "ContextPacketStageAdapter",
    "ContextPacketStageOrchestrator",
    "StagedContextPacketResult",
    "adapt_context_packet",
    "adapt_context_packet_with_mapping",
    "analyze_context_packet",
]
