"""OCR model handlers.

Import this package to get the registry populated with every built-in handler.
"""

from .base import OCRHandler, OCRResponseError, OCRResult, Region, scale_box
from .ovisocr2 import OvisOCR2Handler
from .registry import available_handlers, get_handler, register_handler

__all__ = [
    "OCRHandler",
    "OCRResponseError",
    "OCRResult",
    "OvisOCR2Handler",
    "Region",
    "available_handlers",
    "get_handler",
    "register_handler",
    "scale_box",
]
