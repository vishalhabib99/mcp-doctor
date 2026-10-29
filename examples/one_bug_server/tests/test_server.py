from server import get_forecast


def test_get_forecast_valid_days():
    assert "3-day forecast" in get_forecast("Seattle", 3)


def test_get_forecast_invalid_days_returns_error_string():
    assert "error" in get_forecast("Seattle", 30)


def test_get_forecast_units():
    assert "metric" in get_forecast("Seattle", 3, "metric")
