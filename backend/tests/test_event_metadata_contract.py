from decimal import Decimal

import pytest
from pydantic import ValidationError

from cookops.application.event_metadata import _bounded_decimal, is_bounded_decimal_string
from cookops.http_sync import UpdateEventMetadataPayload


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
