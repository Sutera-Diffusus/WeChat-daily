"""Local WeChat bridge MVP."""

from .models import IncomingMessage, SendResult
from .service import BridgeService

__all__ = ["BridgeService", "IncomingMessage", "SendResult"]
