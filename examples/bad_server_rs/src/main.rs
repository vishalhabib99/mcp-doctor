use rmcp::{handler::server::wrapper::Parameters, tool, tool_router};
use schemars::JsonSchema;
use serde::Deserialize;

const API_KEY: &str = "sk-live-9f8e7d6c5b4a3210";

#[derive(Deserialize, JsonSchema)]
pub struct Args {
    pub q: String,
    pub n: u32,
}

pub struct Bad;

#[tool_router]
impl Bad {
    #[tool]
    async fn search(&self, Parameters(a): Parameters<Args>) -> String {
        a.q
    }

    #[tool(description = "Run")]
    async fn run(&self, Parameters(a): Parameters<Args>) -> String {
        a.q
    }
}
