"""OpenTable booking engine — separate from Resy.

All OpenTable-specific code lives in this package:
- api.py:         OpenTableApiClient (3-step: find → lock → book)
- models.py:      OT Pydantic models (OTSlot, OTAuthToken, etc.)
- fingerprint.py: Mobile app fingerprint pool
- sniper.py:      OTReservationSniper orchestrator
- scout.py:       OT drop & cancellation scouts
"""
