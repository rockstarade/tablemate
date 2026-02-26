"""Shared test fixtures."""

import pytest


@pytest.fixture
def sample_find_response():
    """A realistic /4/find API response."""
    return {
        "results": {
            "venues": [
                {
                    "slots": [
                        {
                            "config": {
                                "id": "slot-1",
                                "type": "Dining Room",
                                "token": "config_token_1",
                            },
                            "date": {
                                "start": "2026-03-26 18:30:00",
                                "end": "2026-03-26 20:30:00",
                            },
                        },
                        {
                            "config": {
                                "id": "slot-2",
                                "type": "Dining Room",
                                "token": "config_token_2",
                            },
                            "date": {
                                "start": "2026-03-26 19:00:00",
                                "end": "2026-03-26 21:00:00",
                            },
                        },
                        {
                            "config": {
                                "id": "slot-3",
                                "type": "Bar",
                                "token": "config_token_3",
                            },
                            "date": {
                                "start": "2026-03-26 19:00:00",
                                "end": "2026-03-26 21:00:00",
                            },
                        },
                        {
                            "config": {
                                "id": "slot-4",
                                "type": "Patio",
                                "token": "config_token_4",
                            },
                            "date": {
                                "start": "2026-03-26 19:15:00",
                                "end": "2026-03-26 21:15:00",
                            },
                        },
                        {
                            "config": {
                                "id": "slot-5",
                                "type": "Dining Room",
                                "token": "config_token_5",
                            },
                            "date": {
                                "start": "2026-03-26 20:00:00",
                                "end": "2026-03-26 22:00:00",
                            },
                        },
                    ]
                }
            ]
        }
    }


@pytest.fixture
def sample_auth_response():
    """A realistic /3/auth/password API response."""
    return {
        "id": 12345,
        "token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.test",
        "first_name": "Test",
        "last_name": "User",
        "payment_methods": [
            {
                "id": 67890,
                "is_default": True,
                "display": "Visa ending in 1234",
            }
        ],
    }


@pytest.fixture
def sample_details_response():
    """A realistic /3/details API response."""
    return {
        "book_token": {
            "value": "book_token_abc123",
            "date_expires": "2026-03-26 10:00:30",
        }
    }


@pytest.fixture
def sample_book_response():
    """A realistic /3/book API response."""
    return {
        "resy_token": "resy_conf_xyz789",
    }
