from decimal import Decimal

import pytest
from pydantic import ValidationError

from cookops.application.event_metadata import _bounded_decimal, is_bounded_decimal_string
from cookops.http_sync import CreateEventPayload, UpdateEventMetadataPayload


def test_create_event_requires_strict_wire_calendar_dates() -> None:
    payload = {
        "event_id": "3d8b2b21-c378-4574-9e46-9338c81305ef",
        "name": "Event",
        "base_expected_attendance": 1,
        "budget_amount": "0",
    }
    with pytest.raises(ValidationError):
        CreateEventPayload.model_validate(payload)
    with pytest.raises(ValidationError):
        CreateEventPayload.model_validate(
            {**payload, "start_date": "2026-02-30", "end_date": "2026-03-01"}
        )
    with pytest.raises(ValidationError):
        CreateEventPayload.model_validate(
            {**payload, "start_date": "0000-01-01", "end_date": "0000-01-01"}
        )


def test_metadata_accepts_legacy_and_new_payload_shapes() -> None:
    common = {
        "event_id": "3d8b2b21-c378-4574-9e46-9338c81305ef",
        "name": "Event",
        "location": None,
        "budget_amount": "0",
        "general_note": None,
    }
    assert UpdateEventMetadataPayload.model_validate(common).start_date is None
    assert (
        UpdateEventMetadataPayload.model_validate(
            {**common, "start_date": "2026-01-01", "end_date": "2026-01-02"}
        ).end_date.isoformat()
        == "2026-01-02"
    )
    with pytest.raises(ValidationError):
        UpdateEventMetadataPayload.model_validate({**common, "start_date": "2026-01-01"})


def test_event_metadata_decimal_contract_rejects_huge_exponents_before_materialization() -> None:
    assert is_bounded_decimal_string("25.50")
    assert not is_bounded_decimal_string("1e100000000")
    assert not is_bounded_decimal_string("1" + "0" * 100)
    assert not is_bounded_decimal_string("1.")
    assert not _bounded_decimal(Decimal("1E+100000000"))
    with pytest.raises(ValidationError):
        UpdateEventMetadataPayload.model_validate(
            {
                "event_id": "3d8b2b21-c378-4574-9e46-9338c81305ef",
                "name": "Event",
                "location": None,
                "budget_amount": "1e100000000",
                "general_note": None,
            }
        )
