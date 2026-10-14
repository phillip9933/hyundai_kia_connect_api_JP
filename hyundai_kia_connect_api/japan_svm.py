"""Japan's request-correlated surround-view capture protocol.

The public Japan app polls /svm/async with the capture's msgId. No uncorrelated
latest-image endpoint is established, so passive reads use completed captures
held in this adapter's memory and never trigger a capture.
"""

import base64
import binascii
import datetime as dt
import re
import time

from .exceptions import (
    InvalidAPIResponseError,
    NoDataFound,
    RequestTimeoutError,
    SafetyAcknowledgmentError,
    UnsupportedControlError,
)
from .svm import (
    SVMDetails,
    _parse_bool,
    _parse_door_open,
    _parse_float,
    _parse_float_list,
    _parse_image_sizes,
    _parse_int,
    redact_svm_metadata,
)
from .utils import get_child_value, parse_datetime


def parse_japan_svm(payload: dict, timezone: dt.tzinfo) -> SVMDetails:
    """Parse a Japan resMsg.scsDetail without logging image or location data."""
    detail = payload.get("scsDetail")
    if not isinstance(detail, dict):
        raise InvalidAPIResponseError("Japan camera response is incomplete")
    encoded = detail.get("svmImage")
    if not isinstance(encoded, str) or not encoded:
        raise InvalidAPIResponseError("Japan camera image is missing")
    try:
        image = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error):
        raise InvalidAPIResponseError("Japan camera image is invalid") from None
    if not image:
        raise InvalidAPIResponseError("Japan camera image is empty")
    raw_time = get_child_value(detail, "gpsDetail.time")
    captured_at = None
    if raw_time:
        try:
            captured_at = parse_datetime(raw_time, timezone)
        except (ValueError, TypeError, AttributeError):
            raise InvalidAPIResponseError("Japan camera timestamp is invalid") from None
    size, sizes = _parse_image_sizes(detail.get("imageSize"))
    return SVMDetails(
        image_bytes=image,
        captured_at=captured_at,
        captured_at_raw=raw_time,
        latitude=_parse_float(get_child_value(detail, "gpsDetail.coord.lat")),
        longitude=_parse_float(get_child_value(detail, "gpsDetail.coord.lon")),
        heading=_parse_int(get_child_value(detail, "gpsDetail.head")),
        speed=(
            _parse_float(get_child_value(detail, "gpsDetail.speed.value")),
            get_child_value(detail, "gpsDetail.speed.unit"),
        ),
        door_open=_parse_door_open(detail.get("doorOpen")),
        trunk_open=_parse_bool(detail.get("trunkOpen")),
        image_size=size,
        image_sizes=sizes,
        valid_angle_of_view=_parse_float_list(detail.get("validAngleofView")),
        raw_metadata=redact_svm_metadata(payload, gps=False),
    )


class JapanSVM:
    """Capture mixin using the Japan adapter's sanitized HTTP helpers."""

    _SVM_POLL_INTERVAL_SECONDS = 5
    _SVM_POLL_TIMEOUT_SECONDS = 120

    def get_svm_details(self, token, vehicle) -> SVMDetails:
        """Return this session's last completed capture without waking the car."""
        details = getattr(self, "_japan_svm_cache", {}).get(vehicle.id)
        if details is None:
            raise NoDataFound("Japan camera requires a new manual capture")
        return details

    def request_svm_capture(
        self, token, vehicle, acknowledged_warning: bool = False
    ) -> SVMDetails:
        """Request once and poll only that request; an ACK is not an image."""
        if acknowledged_warning is not True:
            raise SafetyAcknowledgmentError(
                "request_svm_capture requires acknowledged_warning=True"
            )
        if vehicle.supports_svm is not True:
            raise UnsupportedControlError(
                "Japan vehicle does not support camera capture"
            )
        if not isinstance(vehicle.id, str) or not re.fullmatch(
            r"[A-Za-z0-9_-]+", vehicle.id
        ):
            raise InvalidAPIResponseError("Japan vehicle identifier is invalid")
        path = f"/api/v1/spa/vehicles/{vehicle.id}/svm/async"
        result = self._request("POST", path, headers=self._headers(token))
        message_id = result.get("msgId")
        if not isinstance(message_id, str) or not message_id:
            raise InvalidAPIResponseError("Japan camera request identifier is missing")
        deadline = time.monotonic() + self._SVM_POLL_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            try:
                result = self._request(
                    "GET",
                    path,
                    headers=self._headers(token),
                    params={"msgId": message_id},
                )
            except NoDataFound:
                remaining = deadline - time.monotonic()
                if remaining > 0:
                    time.sleep(min(self._SVM_POLL_INTERVAL_SECONDS, remaining))
                continue
            details = parse_japan_svm(self._payload(result), self.data_timezone)
            if not hasattr(self, "_japan_svm_cache"):
                self._japan_svm_cache = {}
            self._japan_svm_cache[vehicle.id] = details
            return details
        raise RequestTimeoutError("Japan camera capture timed out")
