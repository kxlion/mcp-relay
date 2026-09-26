"""Static coherence of the protocol size bounds.

The result/frame bounds include the largest possible serialized ClientResult
protocol envelope. If a future PR changes the frame or request-id shape without
updating that margin, these tests fail instead of moving the failure to a
confusing runtime refusal.
"""

import json

import mcp_relay.json_bounds as json_bounds
from mcp_relay.output_models import ProviderToolResult
from mcp_relay.protocol import ClientResult


def _compact_size(value: object) -> int:
    return len(
        json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    )


def test_client_result_envelope_margin_matches_maximum_wire_envelope() -> None:
    result = ProviderToolResult(content=[])
    result_payload = result.model_dump(mode="json", by_alias=True, exclude_none=True)
    frame_payload = ClientResult(
        version=2,
        type="result",
        request_id="r" * json_bounds.MAX_REQUEST_ID_LENGTH,
        result=result,
    ).model_dump(mode="json", by_alias=True, exclude_none=True)
    measured_envelope = _compact_size(frame_payload) - _compact_size(result_payload)

    assert getattr(json_bounds, "MAX_CLIENT_RESULT_ENVELOPE_BYTES", None) == (
        measured_envelope
    )


def test_result_and_maximum_envelope_fit_in_ws_frame() -> None:
    envelope = getattr(json_bounds, "MAX_CLIENT_RESULT_ENVELOPE_BYTES", 0)
    assert (
        json_bounds.MAX_TOOL_RESULT_BYTES + envelope
        <= json_bounds.MAX_WS_MESSAGE_BYTES
    )
