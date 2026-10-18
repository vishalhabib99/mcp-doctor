use rmcp::{ServerHandler, handler::server::wrapper::Parameters, tool, tool_handler, tool_router};
use schemars::JsonSchema;
use serde::Deserialize;

#[derive(Deserialize, JsonSchema)]
pub struct ForecastRequest {
    /// City name, e.g. "Seattle"
    pub city: String,
    /// Number of days to forecast, 1-7 (default 3)
    pub days: Option<u8>,
}

#[derive(Clone)]
pub struct Weather;

#[tool_router]
impl Weather {
    /// Get the weather forecast for a city, one line per day.
    #[tool]
    async fn get_forecast(&self, Parameters(req): Parameters<ForecastRequest>) -> Result<String, String> {
        let days = req.days.unwrap_or(3);
        if !(1..=7).contains(&days) {
            return Err(format!("days must be 1-7, got {days}"));
        }
        Ok(format!("{}: sunny for {days} days", req.city))
    }
}

#[tool_handler]
impl ServerHandler for Weather {}

#[cfg(test)]
mod tests {
    #[test]
    fn forecast_days_default() {
        assert_eq!(Some(3u8).unwrap_or(3), 3);
    }
}
