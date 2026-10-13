import datetime as dt
from unittest.mock import Mock

import pytest

from hyundai_kia_connect_api.ApiImpl import ApiImpl
from hyundai_kia_connect_api.Token import Token
from hyundai_kia_connect_api.Vehicle import Vehicle
from hyundai_kia_connect_api.VehicleManager import VehicleManager


class DummyApi(ApiImpl):
    def __init__(self):
        super().__init__()
        self.login_calls = 0

    def login(self, username, password, token=None, otp_handler=None, pin=None):
        self.login_calls += 1
        return Token(
            username=username,
            password=password,
            valid_until=dt.datetime.now(dt.UTC) + dt.timedelta(minutes=1),
        )

    def get_vehicles(self, token):
        return []

    def refresh_vehicles(self, token, vehicles):
        return vehicles


def test_check_and_refresh_token_handles_min_datetime(monkeypatch):
    dummy_api = DummyApi()
    monkeypatch.setattr(
        VehicleManager,
        "get_implementation_by_region_brand",
        lambda *args, **kwargs: dummy_api,
    )
    manager = VehicleManager(
        region=3,
        brand=1,
        username="user",
        password="pass",
        pin="1234",
        geocode_api_enable=False,
    )
    manager.token = Token(valid_until=dt.datetime.min)
    assert manager.check_and_refresh_token() is True
    assert dummy_api.login_calls == 1


@pytest.mark.parametrize("enabled", [True, False])
@pytest.mark.parametrize("supports_force_refresh", [True, False])
def test_stale_vehicle_respects_force_refresh_capability(
    monkeypatch, enabled, supports_force_refresh
):
    api = Mock(spec=ApiImpl)
    api.supports_force_refresh = supports_force_refresh
    monkeypatch.setattr(
        VehicleManager, "get_implementation_by_region_brand", lambda *args: api
    )
    manager = VehicleManager(
        region=3,
        brand=1,
        username="user",
        password="pass",
        pin="1234",
        geocode_api_enable=True,
    )
    vehicle = Vehicle(id="synthetic", enabled=enabled)
    vehicle.last_updated_at = dt.datetime.now(dt.UTC) - dt.timedelta(days=1)
    manager.vehicles[vehicle.id] = vehicle

    manager.check_and_force_update_vehicle(600, vehicle.id)

    if enabled and supports_force_refresh:
        api.force_refresh_vehicle_state.assert_called_once_with(manager.token, vehicle)
        api.update_vehicle_with_cached_state.assert_not_called()
    elif enabled:
        api.force_refresh_vehicle_state.assert_not_called()
        api.update_vehicle_with_cached_state.assert_called_once_with(
            manager.token, vehicle
        )
        api.update_geocoded_location.assert_called_once()
    else:
        api.force_refresh_vehicle_state.assert_not_called()
        api.update_vehicle_with_cached_state.assert_not_called()
        api.update_geocoded_location.assert_not_called()
