"""Synthetic Japan contracts; every request is mocked and no account is used."""

import datetime as dt
import logging
from unittest.mock import Mock

import pytest
import requests

from hyundai_kia_connect_api.exceptions import (
    APIError,
    AuthenticationError,
    ConsentRequiredError,
    InvalidAPIResponseError,
    RateLimitingError,
    RequestTimeoutError,
    UnsupportedControlError,
)
from hyundai_kia_connect_api.HyundaiBlueLinkApiJP import HyundaiBlueLinkApiJP
from hyundai_kia_connect_api.Token import Token
from hyundai_kia_connect_api.Vehicle import Vehicle
from hyundai_kia_connect_api.VehicleManager import VehicleManager


def envelope(payload):
    return {"retCode": "S", "resCode": "0000", "resMsg": payload}


def response(payload, status=200):
    result = Mock(status_code=status)
    result.json.return_value = payload
    return result


@pytest.fixture
def api():
    result = HyundaiBlueLinkApiJP(10, 2, "en")
    result.session = Mock()
    return result


def test_factory_and_capabilities():
    api = VehicleManager.get_implementation_by_region_brand(10, 2, "en")
    assert isinstance(api, HyundaiBlueLinkApiJP)
    for capability in (
        "supports_remote_commands",
        "supports_force_refresh",
        "supports_window_control",
        "supports_valet_mode",
        "supports_svm",
    ):
        assert getattr(api, capability) is False
    for brand in (1, 3):
        with pytest.raises(APIError):
            VehicleManager.get_implementation_by_region_brand(10, brand, "en")


@pytest.mark.parametrize(
    "epoch,expected",
    [
        (
            1791846000,
            "8AXi68sDH6x6xivBKGT84+YiFhG4wOHVJVpLyjVNr5WMJKGtK8ZnraiLMUhD1qk=",
        ),
        (
            1999999999,
            "8AXi68sDH6x6xivBKGT84+YiFhG4wOHVJVpLyjVNr5WMJKGtK8ZpraCKPEdK36A=",
        ),
        (
            2000000000,
            "8AXi68sDH6x6xivBKGT84+YiFhG4wOHVJVpLyjVNr5WMJKGtK8VgpKmDNU5D1qk=",
        ),
    ],
)
def test_stamp_offline_native_golden_vectors(api, monkeypatch, epoch, expected):
    monkeypatch.setattr("time.time", lambda: epoch)
    assert api._stamp() == expected


def test_stamp_rejects_unsupported_epoch(api, monkeypatch):
    monkeypatch.setattr("time.time", lambda: 10000000000)
    with pytest.raises(APIError):
        api._stamp()


def test_login_contract(api):
    api.session.request.side_effect = [
        response(None),
        response({}),
        response({"redirectUrl": api.REDIRECT_URI + "?code=SYNTHETIC_CODE"}),
        response(
            {
                "access_token": "SYNTHETIC_ACCESS",
                "token_type": "Bearer",
                "refresh_token": "SYNTHETIC_REFRESH",
                "expires_in": 3600,
            }
        ),
        response(envelope({"deviceId": "SYNTHETIC_DEVICE"})),
    ]
    before = dt.datetime.now(dt.UTC)
    token = api.login("SYNTHETIC_USER", "SYNTHETIC_PASSWORD", "1234")
    assert token.device_id == "SYNTHETIC_DEVICE"
    assert token.access_token == "Bearer SYNTHETIC_ACCESS"
    assert before + dt.timedelta(seconds=3600) <= token.valid_until
    calls = api.session.request.call_args_list
    assert [call.args[0] for call in calls] == ["GET", "POST", "POST", "POST", "POST"]
    assert calls[0].kwargs["params"] == {
        "response_type": "code",
        "client_id": api.SERVICE_ID,
        "redirect_uri": api.REDIRECT_URI,
        "lang": "ja",
    }
    assert calls[1].kwargs["json"] == {"lang": "ja"}
    assert calls[3].kwargs["data"]["code"] == "SYNTHETIC_CODE"
    assert calls[4].kwargs["headers"]["Authorization"] == ""
    assert "Stamp" in calls[4].kwargs["headers"]


@pytest.mark.parametrize(
    "signin,error",
    [
        ({"step": "consent"}, ConsentRequiredError),
        ({"redirectUrl": "https://example.invalid/?code=secret"}, AuthenticationError),
        ({"redirectUrl": "bad"}, AuthenticationError),
    ],
)
def test_login_nonterminal_or_untrusted_redirect(api, signin, error):
    api.session.request.side_effect = [response(None), response({}), response(signin)]
    with pytest.raises(error):
        api.login("synthetic", "synthetic")
    assert api.session.request.call_count == 3


def test_refresh_preserves_device_and_refresh_token(api):
    token = Token(
        username="user",
        password="pass",
        device_id="device",
        refresh_token="refresh",
        pin="1234",
    )
    api.session.request.return_value = response(
        {"access_token": "access", "token_type": "Bearer", "expires_in": 600}
    )
    renewed = api.refresh_access_token(token)
    assert renewed.device_id == "device"
    assert renewed.refresh_token == "refresh"
    assert renewed.pin == "1234"
    assert api.session.request.call_args.kwargs["data"]["grant_type"] == "refresh_token"


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"access_token": "x", "token_type": "Bearer", "expires_in": 0},
        {"access_token": "x", "token_type": "Bearer", "expires_in": "bad"},
        {"access_token": None, "token_type": "Bearer", "expires_in": 60},
        {"access_token": "x", "token_type": None, "expires_in": 60},
        {"access_token": "x", "token_type": "Bearer", "expires_in": True},
        {"access_token": "x", "token_type": "Bearer", "expires_in": "9" * 100},
        {
            "access_token": "x",
            "token_type": "Bearer",
            "expires_in": 60,
            "refresh_token": [],
        },
    ],
)
def test_invalid_token_schema(api, payload):
    api.session.request.return_value = response(payload)
    with pytest.raises(InvalidAPIResponseError):
        api.refresh_access_token(Token(refresh_token="synthetic"))


def test_discovery_requires_profile_protocol_and_uses_tokyo(api):
    api.session.request.side_effect = [
        response(
            envelope(
                {
                    "vehicles": [
                        {
                            "vehicleId": "synthetic",
                            "type": "EV",
                            "vehicleName": "IONIQ",
                            "nickname": "My car",
                        }
                    ]
                }
            )
        ),
        response(
            envelope(
                {
                    "vinInfo": [
                        {"basic": {"vin": "SYNTHETIC_VIN", "ccuCCS2ProtocolSupport": 0}}
                    ]
                }
            )
        ),
    ]
    vehicles = api.get_vehicles(Token(access_token="access", device_id="device"))
    assert len(vehicles) == 1
    assert vehicles[0].VIN == "SYNTHETIC_VIN"
    assert str(vehicles[0].timezone) == "Asia/Tokyo"
    assert api.session.request.call_args.args[1].endswith("/synthetic/profile")


@pytest.mark.parametrize("protocol", [None, "0", -1])
def test_discovery_does_not_guess_protocol(api, protocol):
    api.session.request.side_effect = [
        response(envelope({"vehicles": [{"vehicleId": "synthetic"}]})),
        response(
            envelope({"vinInfo": [{"basic": {"ccuCCS2ProtocolSupport": protocol}}]})
        ),
    ]
    with pytest.raises(InvalidAPIResponseError):
        api.get_vehicles(Token())


def test_legacy_cached_wrapper_single_request(api):
    api.session.request.return_value = response(
        envelope(
            {
                "vehicleStatusInfo": {
                    "vehicleStatus": {
                        "time": "20261013090000",
                        "doorLock": True,
                        "battery": {"batSoc": 80},
                    },
                    "vehicleLocation": None,
                }
            }
        )
    )
    vehicle = Vehicle(id="synthetic", ccu_ccs2_protocol_support=0)
    api.update_vehicle_with_cached_state(Token(), vehicle)
    assert vehicle.is_locked is True
    assert vehicle.car_battery_percentage == 80
    assert vehicle.last_updated_at.utcoffset() == dt.timedelta(hours=9)
    api.session.request.assert_called_once()
    assert api.session.request.call_args.args[1].endswith("/status/latest")


def test_ccs2_cached_single_request(api):
    api.session.request.return_value = response(
        envelope(
            {
                "state": {
                    "Vehicle": {
                        "Date": "20261013090000",
                        "Drivetrain": {
                            "FuelSystem": {"DTE": {"Total": 300, "Unit": 1}}
                        },
                        "Green": {
                            "BatteryManagement": {"BatteryRemain": {"Ratio": 65}}
                        },
                    }
                }
            }
        )
    )
    vehicle = Vehicle(id="synthetic", ccu_ccs2_protocol_support=1)
    api.update_vehicle_with_cached_state(Token(), vehicle)
    assert vehicle.ev_battery_percentage == 65
    assert vehicle.total_driving_range == 300
    assert vehicle.total_driving_range_unit == "km"
    assert vehicle.last_updated_at.hour == 18
    assert vehicle.last_updated_at.utcoffset() == dt.timedelta(hours=9)
    api.session.request.assert_called_once()
    assert api.session.request.call_args.args[1].endswith("/ccs2/carstatus/latest")


@pytest.mark.parametrize(
    "payload",
    [{}, {"vehicleStatusInfo": {}}, {"vehicleStatusInfo": {"vehicleStatus": None}}],
)
def test_malformed_status_rejected(api, payload):
    api.session.request.return_value = response(envelope(payload))
    vehicle = Vehicle(id="synthetic", ccu_ccs2_protocol_support=0)
    with pytest.raises(InvalidAPIResponseError):
        api.update_vehicle_with_cached_state(Token(), vehicle)
    assert vehicle.last_updated_at is None


@pytest.mark.parametrize(
    "method",
    [
        "force_refresh_vehicle_state",
        "lock_action",
        "start_climate",
        "stop_climate",
        "start_charge",
        "stop_charge",
        "set_charge_limits",
        "set_charging_current",
        "set_windows_state",
        "charge_port_action",
        "schedule_charging_and_climate",
        "start_hazard_lights",
        "start_hazard_lights_and_horn",
        "valet_mode_action",
        "set_vehicle_to_load_discharge_limit",
        "set_navigation",
        "request_svm_capture",
    ],
)
def test_commands_fail_before_io(api, method):
    with pytest.raises(UnsupportedControlError):
        getattr(api, method)(Token(), Vehicle())
    api.session.request.assert_not_called()


@pytest.mark.parametrize(
    "status,error", [(401, AuthenticationError), (429, RateLimitingError)]
)
def test_http_errors_do_not_expose_payload(api, caplog, status, error):
    secret = "UNIQUE_SYNTHETIC_SECRET_VIN_COORDINATES"
    api.session.request.return_value = response({"resMsg": secret}, status)
    with caplog.at_level(logging.DEBUG), pytest.raises(error) as raised:
        api._request("POST", "/api/v1/user/signin", json={"password": secret})
    assert secret not in caplog.text + str(raised.value)


def test_connection_errors_do_not_expose_url_or_tokens(api, caplog):
    secret = "UNIQUE_SYNTHETIC_PASSWORD_TOKEN_VIN_COORDINATES"
    api.session.request.side_effect = requests.exceptions.Timeout(secret)
    with caplog.at_level(logging.DEBUG), pytest.raises(RequestTimeoutError) as raised:
        api._request("GET", "/api/v1/spa/vehicles/" + secret)
    assert secret not in caplog.text + str(raised.value)


@pytest.mark.parametrize("status", [307, 308])
def test_api_redirect_does_not_forward_secrets(api, status):
    redirect = response({}, status)
    redirect.headers = {"Location": "https://example.invalid/steal"}
    api.session.request.return_value = redirect
    with pytest.raises(APIError):
        api._request("POST", "/api/v1/user/signin", json={"password": "synthetic"})
    api.session.request.assert_called_once()
    assert api.session.request.call_args.kwargs["allow_redirects"] is False


@pytest.mark.parametrize(
    "identifier,protocol", [("../x", 0), ("x%2Fy", 0), ("x", True), ("x", -1)]
)
def test_cached_arguments_validated_before_io(api, identifier, protocol):
    with pytest.raises(InvalidAPIResponseError):
        api.update_vehicle_with_cached_state(
            Token(), Vehicle(id=identifier, ccu_ccs2_protocol_support=protocol)
        )
    api.session.request.assert_not_called()


def test_parser_failure_does_not_partially_mutate_vehicle(api):
    vehicle = Vehicle(id="synthetic", ccu_ccs2_protocol_support=1)
    api.session.request.return_value = response(
        envelope(
            {
                "state": {
                    "Vehicle": {
                        "Date": "20261013090000",
                        "Electronics": {"Battery": {"Level": 90}},
                    }
                }
            }
        )
    )
    with pytest.raises(InvalidAPIResponseError):
        api.update_vehicle_with_cached_state(Token(), vehicle)
    assert vehicle.last_updated_at is None
    assert vehicle.car_battery_percentage is None


def test_legacy_temperature_does_not_guess_profile_dependent_scale(api):
    api.session.request.return_value = response(
        envelope(
            {
                "vehicleStatusInfo": {
                    "vehicleStatus": {
                        "time": "20261013090000",
                        "airTemp": {
                            "value": "0EH",
                            "unit": 0,
                            "hvacTempType": 1,
                        },
                    },
                }
            }
        )
    )
    vehicle = Vehicle(id="synthetic", ccu_ccs2_protocol_support=0)
    api.update_vehicle_with_cached_state(Token(), vehicle)
    assert vehicle.air_temperature is None


def test_unknown_error_code_does_not_become_success(api):
    api.session.request.return_value = response({"resCode": "unrecognized"})
    with pytest.raises(APIError):
        api.get_vehicles(Token())


def test_authorize_external_redirect_rejected_before_signin(api):
    redirect = response({}, 302)
    redirect.url = api.BASE_URL + "/api/v1/user/oauth2/authorize"
    redirect.headers = {"Location": "https://example.invalid/login"}
    api.session.request.return_value = redirect
    with pytest.raises(AuthenticationError):
        api.login("synthetic", "synthetic")
    api.session.request.assert_called_once()


def test_legacy_zero_and_false_status_are_preserved(api):
    api.session.request.return_value = response(
        envelope(
            {
                "vehicleStatusInfo": {
                    "vehicleStatus": {
                        "time": "20261013090000",
                        "doorLock": False,
                        "engine": False,
                        "battery": {"batSoc": 0},
                        "evStatus": {"batteryStatus": 0, "batteryCharge": False},
                    },
                }
            }
        )
    )
    vehicle = Vehicle(id="synthetic", ccu_ccs2_protocol_support=0)
    api.update_vehicle_with_cached_state(Token(), vehicle)
    assert vehicle.is_locked is False
    assert vehicle.engine_is_running is False
    assert vehicle.car_battery_percentage == 0
    assert vehicle.ev_battery_percentage == 0
    assert vehicle.ev_battery_is_charging is False


def test_empty_account_discovery(api):
    api.session.request.return_value = response(envelope({"vehicles": []}))
    assert api.get_vehicles(Token()) == []
    api.session.request.assert_called_once()


def test_discovery_keeps_multiple_vehicles_separate(api):
    api.session.request.side_effect = [
        response(
            envelope(
                {
                    "vehicles": [
                        {"vehicleId": "first", "nickname": "First", "type": "EV"},
                        {"vehicleId": "second", "nickname": "Second", "type": "EV"},
                    ]
                }
            )
        ),
        response(envelope({"vinInfo": [{"basic": {"ccuCCS2ProtocolSupport": 0}}]})),
        response(envelope({"vinInfo": [{"basic": {"ccuCCS2ProtocolSupport": 1}}]})),
    ]
    vehicles = api.get_vehicles(Token())
    assert [v.id for v in vehicles] == ["first", "second"]
    assert [v.ccu_ccs2_protocol_support for v in vehicles] == [0, 1]


@pytest.mark.parametrize(
    "payload,error",
    [
        ({"error": "invalid_grant"}, AuthenticationError),
        ({"error": "invalid_token"}, AuthenticationError),
        ({"resCode": "4003"}, AuthenticationError),
        ({"error": "invalid_request"}, APIError),
    ],
)
def test_bad_request_classifies_only_known_auth_failures(api, payload, error):
    api.session.request.return_value = response(payload, 400)
    with pytest.raises(error) as raised:
        api.refresh_access_token(Token(refresh_token="synthetic"))
    assert type(raised.value) is error


def test_language_accepts_empty_204_without_decoding_json(api):
    language_response = response(None, 204)
    language_response.json.side_effect = requests.exceptions.JSONDecodeError(
        "empty response", "", 0
    )
    api.session.request.side_effect = [
        response(None),
        language_response,
        response({"redirectUrl": api.REDIRECT_URI + "?code=SYNTHETIC_CODE"}),
        response(
            {
                "access_token": "SYNTHETIC_ACCESS",
                "token_type": "Bearer",
                "refresh_token": "SYNTHETIC_REFRESH",
                "expires_in": 3600,
            }
        ),
        response(envelope({"deviceId": "SYNTHETIC_DEVICE"})),
    ]
    assert api.login("synthetic", "synthetic").device_id == "SYNTHETIC_DEVICE"
    language_response.json.assert_not_called()


def test_requests_json_decode_error_has_response_classification(api):
    bad_response = response(None)
    bad_response.json.side_effect = requests.exceptions.JSONDecodeError(
        "invalid", "UNIQUE_SYNTHETIC_SECRET", 0
    )
    api.session.request.return_value = bad_response
    with pytest.raises(InvalidAPIResponseError) as raised:
        api.get_vehicles(Token())
    assert "UNIQUE_SYNTHETIC_SECRET" not in str(raised.value)
