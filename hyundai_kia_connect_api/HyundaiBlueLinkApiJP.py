"""Read-only Hyundai Japan Bluelink adapter.

Contracts are based on the Japan app and its public login page. Vehicle
commands and forced refresh remain disabled until separately verified.
"""

import base64
import copy
import datetime as dt
import re
import time
import uuid
from urllib.parse import parse_qs, urljoin, urlparse
from zoneinfo import ZoneInfo

import requests

from .ApiImpl import ApiImpl, ApiImplSession
from .ApiImplType1 import ApiImplType1
from .const import ENGINE_TYPES
from .exceptions import (
    APIError,
    AuthenticationError,
    ConsentRequiredError,
    DeviceIDError,
    InvalidAPIResponseError,
    NoDataFound,
    RateLimitingError,
    RequestTimeoutError,
    ServiceTemporaryUnavailable,
    UnsupportedControlError,
)
from .Token import Token
from .utils import parse_datetime
from .Vehicle import Vehicle


class HyundaiBlueLinkApiJP(ApiImpl):
    """Cached status for Hyundai Japan; no vehicle-changing requests."""

    data_timezone = ZoneInfo("Asia/Tokyo")
    supports_remote_commands = False
    supports_force_refresh = False
    BASE_URL = "https://ccs-apigw.hyundaijapan.com:8080"
    SERVICE_ID = "e084c0e4-fcfe-47e3-99c9-36eb93593737"
    APP_ID = "8be122e0-b0d9-40b5-a46e-67b25194ba88"
    REDIRECT_URI = BASE_URL + "/api/v1/user/oauth2/redirect"
    # CFB128 keystream for the fixed UUID prefix and a ten-digit epoch.
    # Derived offline from Japan app 1.0.17; no app binary is distributed.
    _STAMP_STREAM = base64.b64decode(
        "yGeH2vkxepxXpBulEUnI04QXO3CM9oT4E20p+AB8lqHuRZmVEfdQlJmzBX5z5pk="
    )
    BASIC_AUTHORIZATION = "Basic ZTA4NGMwZTQtZmNmZS00N2UzLTk5YzktMzZlYjkzNTkzNzM3OjFZcWV4dng3TWxiS3c2YUFvYk1FbDJqQVFvQTBtUVhyV1E1SWpIT083TzRZRHRkRw=="

    # Reuse only pure schema parsers, never Type1 HTTP/logging helpers.
    _update_vehicle_properties = ApiImplType1._update_vehicle_properties
    _update_vehicle_properties_ccs2 = ApiImplType1._update_vehicle_properties_ccs2
    _get_time_from_string = ApiImplType1._get_time_from_string

    def __init__(self, region: int, brand: int, language: str) -> None:
        if region != 10 or brand != 2:
            raise APIError("Japan is supported only for Hyundai in region 10")
        super().__init__()
        self.session = ApiImplSession()

    def _stamp(self) -> str:
        epoch = str(int(time.time()))
        if len(epoch) != 10:
            raise APIError("Japan stamp requires a supported ten-digit epoch")
        plain = f"{self.APP_ID}:{epoch}".encode()
        return base64.b64encode(
            bytes(a ^ b for a, b in zip(self._STAMP_STREAM, plain, strict=True))
        ).decode()

    def _headers(self, token: Token | None = None) -> dict:
        headers = {
            "ccsp-service-id": self.SERVICE_ID,
            "ccsp-application-id": self.APP_ID,
            "offset": "9",
            "Stamp": self._stamp(),
        }
        if token is not None:
            headers["Authorization"] = token.access_token
            headers["ccsp-device-id"] = token.device_id
        return headers

    def _request(self, method: str, path: str, *, json_response=True, **kwargs):
        """Translate errors without exposing URLs, payloads, or vendor messages."""
        kwargs.setdefault("allow_redirects", False)
        try:
            response = self.session.request(method, self.BASE_URL + path, **kwargs)
            if response.status_code in (401, 403):
                raise AuthenticationError("Japan authentication was rejected")
            if response.status_code == 429:
                raise RateLimitingError("Japan API rate limit reached")
            if response.status_code >= 500:
                raise ServiceTemporaryUnavailable("Japan API is unavailable")
            if response.status_code == 400:
                try:
                    error_result = response.json()
                except ValueError:
                    error_result = None
                if isinstance(error_result, dict):
                    if error_result.get("error") in ("invalid_grant", "invalid_token"):
                        raise AuthenticationError("Japan token grant was rejected")
                    if str(error_result.get("resCode")) in ("4003", "4006", "4016"):
                        raise AuthenticationError("Japan authentication was rejected")
            if not json_response and 300 <= response.status_code < 400:
                return response
            if not 200 <= response.status_code < 300:
                raise APIError("Japan API request was rejected")
            if not json_response:
                return response
            result = response.json()
        except (requests.exceptions.Timeout, RequestTimeoutError):
            raise RequestTimeoutError("Japan API request timed out") from None
        except requests.exceptions.RequestException:
            raise APIError("Japan API connection failed") from None
        except ValueError:
            raise InvalidAPIResponseError("Japan API returned invalid JSON") from None
        if not isinstance(result, dict):
            raise InvalidAPIResponseError("Japan API returned an invalid object")
        self._check_errors(result)
        return result

    @staticmethod
    def _check_errors(result: dict) -> None:
        code = str(result.get("resCode", result.get("resultCode", "")))
        error = {
            "4002": DeviceIDError,
            "4003": AuthenticationError,
            "4006": AuthenticationError,
            "4016": AuthenticationError,
            "4081": RequestTimeoutError,
            "5031": ServiceTemporaryUnavailable,
            "5091": RateLimitingError,
            "5921": NoDataFound,
            "7501": AuthenticationError,
        }.get(code)
        if error is not None:
            raise error("Japan API rejected the request")
        if result.get("retCode", result.get("resultType")) == "F":
            raise APIError("Japan API rejected the request")
        if code and code != "0000":
            raise APIError("Japan API rejected the request")
        if result.get("error"):
            raise AuthenticationError("Japan token grant was rejected")

    @staticmethod
    def _payload(result: dict) -> dict:
        payload = result.get("resMsg", result.get("responseEntity"))
        if not isinstance(payload, dict):
            raise InvalidAPIResponseError("Japan API response payload is missing")
        return payload

    def _exchange_token(self, grant: dict, username, password, device_id, pin) -> Token:
        headers = self._headers()
        headers["Authorization"] = self.BASIC_AUTHORIZATION
        result = self._request(
            "POST",
            "/api/v1/user/oauth2/token",
            headers=headers,
            data={
                "client_id": self.SERVICE_ID,
                "redirect_uri": self.REDIRECT_URI,
                **grant,
            },
        )
        try:
            access = result["access_token"]
            token_type = result["token_type"]
            expiry = result["expires_in"]
            if isinstance(expiry, bool) or not re.fullmatch(r"[0-9]+", str(expiry)):
                raise ValueError
            lifetime = int(expiry)
            if not isinstance(access, str) or not access or lifetime <= 0:
                raise ValueError
            if token_type != "Bearer":
                raise ValueError
            refresh = result.get("refresh_token", grant.get("refresh_token"))
            if not isinstance(refresh, str) or not refresh:
                raise ValueError
            valid_until = dt.datetime.now(dt.UTC) + dt.timedelta(seconds=lifetime)
        except (KeyError, TypeError, ValueError, OverflowError):
            raise InvalidAPIResponseError(
                "Japan token response is incomplete"
            ) from None
        return Token(
            username=username,
            password=password,
            access_token=f"{token_type} {access}",
            refresh_token=refresh,
            device_id=device_id,
            pin=pin,
            valid_until=valid_until,
        )

    def login(self, username: str, password: str, pin: str | None = None) -> Token:
        """Establish cookies, sign in, exchange the code, and register a device."""
        response = self._request(
            "GET",
            "/api/v1/user/oauth2/authorize",
            json_response=False,
            allow_redirects=False,
            params={
                "response_type": "code",
                "client_id": self.SERVICE_ID,
                "redirect_uri": self.REDIRECT_URI,
                "lang": "ja",
            },
        )
        for _ in range(5):
            if not 300 <= response.status_code < 400:
                break
            target = urljoin(response.url, response.headers.get("Location", ""))
            parsed = urlparse(target)
            expected = urlparse(self.BASE_URL)
            if (parsed.scheme, parsed.netloc) != (expected.scheme, expected.netloc):
                raise AuthenticationError(
                    "Japan login redirected to an unexpected host"
                )
            response = self._request(
                "GET",
                parsed.path + ("?" + parsed.query if parsed.query else ""),
                json_response=False,
                allow_redirects=False,
            )
        else:
            raise AuthenticationError("Japan login exceeded its redirect limit")
        self._request("POST", "/api/v1/user/language", json={"lang": "ja"})
        result = self._request(
            "POST",
            "/api/v1/user/signin",
            json={"email": username, "password": password, "mobileNum": ""},
        )
        redirect = result.get("redirectUrl")
        if not isinstance(redirect, str):
            raise ConsentRequiredError(
                "Complete Japan account setup in the official app"
            )
        parsed = urlparse(redirect)
        expected = urlparse(self.REDIRECT_URI)
        code = parse_qs(parsed.query).get("code", [])
        if (
            (parsed.scheme, parsed.netloc, parsed.path)
            != (
                expected.scheme,
                expected.netloc,
                expected.path,
            )
            or len(code) != 1
            or not code[0]
        ):
            raise AuthenticationError(
                "Japan login returned no valid authorization code"
            )
        token = self._exchange_token(
            {"grant_type": "authorization_code", "code": code[0]},
            username,
            password,
            None,
            pin,
        )
        headers = self._headers()
        # The app uses blank placeholders; requests rejects whitespace header
        # values. This fresh session has no default authorization/device header.
        headers.update({"Authorization": "", "ccsp-device-id": ""})
        result = self._request(
            "POST",
            "/api/v1/spa/notifications/register",
            headers=headers,
            json={
                "uuid": str(uuid.uuid4()),
                "pushRegId": uuid.uuid4().hex,
                "pushType": "GCM",
            },
        )
        token.device_id = self._payload(result).get("deviceId")
        if not isinstance(token.device_id, str) or not token.device_id:
            raise InvalidAPIResponseError("Japan device registration was incomplete")
        return token

    def refresh_access_token(self, token: Token) -> Token:
        """Use the refresh grant; preserve the registered device identity."""
        if not token.refresh_token:
            raise AuthenticationError("Japan refresh token is missing; sign in again")
        return self._exchange_token(
            {"grant_type": "refresh_token", "refresh_token": token.refresh_token},
            token.username,
            token.password,
            token.device_id,
            token.pin,
        )

    def get_vehicles(self, token: Token) -> list[Vehicle]:
        """Discover vehicles and retrieve profiles to select the status protocol."""
        result = self._request(
            "GET", "/api/v1/spa/vehicles", headers=self._headers(token)
        )
        entries = self._payload(result).get("vehicles")
        if not isinstance(entries, list):
            raise InvalidAPIResponseError("Japan vehicle list is missing")
        vehicles = []
        engine_types = {
            "GN": ENGINE_TYPES.ICE,
            "EV": ENGINE_TYPES.EV,
            "PHEV": ENGINE_TYPES.PHEV,
            "PE": ENGINE_TYPES.PHEV,
            "HV": ENGINE_TYPES.HEV,
        }
        for entry in entries:
            if not isinstance(entry, dict) or not isinstance(
                entry.get("vehicleId"), str
            ):
                raise InvalidAPIResponseError("Japan vehicle identifier is missing")
            vehicle_id = entry["vehicleId"]
            if not re.fullmatch(r"[A-Za-z0-9_-]+", vehicle_id):
                raise InvalidAPIResponseError("Japan vehicle identifier is invalid")
            result = self._request(
                "GET",
                f"/api/v1/spa/vehicles/{vehicle_id}/profile",
                headers=self._headers(token),
            )
            profiles = self._payload(result).get("vinInfo")
            if not isinstance(profiles, list) or len(profiles) != 1:
                raise InvalidAPIResponseError("Japan vehicle profile is incomplete")
            basic = profiles[0].get("basic") if isinstance(profiles[0], dict) else None
            if not isinstance(basic, dict):
                raise InvalidAPIResponseError("Japan vehicle profile is incomplete")
            protocol = basic.get("ccuCCS2ProtocolSupport")
            if type(protocol) is not int or protocol < 0:
                raise InvalidAPIResponseError(
                    "Japan vehicle status protocol is missing"
                )
            vehicles.append(
                Vehicle(
                    id=vehicle_id,
                    name=entry.get("nickname") or entry.get("vehicleName"),
                    model=basic.get("modelName") or entry.get("vehicleName"),
                    registration_date=entry.get("regDate"),
                    VIN=basic.get("vin"),
                    timezone=self.data_timezone,
                    engine_type=engine_types.get(entry.get("type")),
                    ccu_ccs2_protocol_support=protocol,
                )
            )
        return vehicles

    def update_vehicle_with_cached_state(self, token: Token, vehicle: Vehicle) -> None:
        """Read stored status; do not request location updates or driving history."""
        protocol = vehicle.ccu_ccs2_protocol_support
        if type(protocol) is not int or protocol < 0:
            raise InvalidAPIResponseError("Japan vehicle status protocol is missing")
        if not isinstance(vehicle.id, str) or not re.fullmatch(
            r"[A-Za-z0-9_-]+", vehicle.id
        ):
            raise InvalidAPIResponseError("Japan vehicle identifier is invalid")
        endpoint = "ccs2/carstatus/latest" if protocol else "status/latest"
        headers = self._headers(token)
        headers["ccuCCS2ProtocolSupport"] = str(protocol)
        payload = self._payload(
            self._request(
                "GET", f"/api/v1/spa/vehicles/{vehicle.id}/{endpoint}", headers=headers
            )
        )
        try:
            parsed_vehicle = copy.deepcopy(vehicle)
            if protocol:
                state = payload["state"]["Vehicle"]
                if not isinstance(state, dict):
                    raise TypeError
                if not state.get("Date"):
                    raise ValueError
                parse_datetime(state["Date"], dt.UTC)
                self._update_vehicle_properties_ccs2(parsed_vehicle, state)
            else:
                info = payload["vehicleStatusInfo"]
                state = {
                    "status": info["vehicleStatus"],
                    "vehicleLocation": info.get("vehicleLocation"),
                }
                if not isinstance(state["status"], dict):
                    raise TypeError
                if not state["status"].get("time"):
                    raise ValueError
                parse_datetime(state["status"]["time"], self.data_timezone)
                # Japan's legacy temperature scale depends on profile options
                # and hvacTempType. A fixed regional range would invent values.
                state = copy.deepcopy(state)
                state["status"].pop("airTemp", None)
                parsed_vehicle.air_temperature = (None, None)
                self._update_vehicle_properties(parsed_vehicle, state)
        except (KeyError, TypeError, ValueError, AttributeError):
            raise InvalidAPIResponseError("Japan cached status is incomplete") from None
        vehicle.__dict__.update(parsed_vehicle.__dict__)

    def _unsupported(self, *args, **kwargs):
        raise UnsupportedControlError("Japan vehicle commands are not verified")

    force_refresh_vehicle_state = _unsupported
    lock_action = _unsupported
    start_climate = _unsupported
    stop_climate = _unsupported
    start_charge = _unsupported
    stop_charge = _unsupported
    set_charge_limits = _unsupported
    set_charging_current = _unsupported
    set_windows_state = _unsupported
    charge_port_action = _unsupported
    schedule_charging_and_climate = _unsupported
    start_hazard_lights = _unsupported
    start_hazard_lights_and_horn = _unsupported
    valet_mode_action = _unsupported
    set_vehicle_to_load_discharge_limit = _unsupported
    set_navigation = _unsupported
    request_svm_capture = _unsupported
