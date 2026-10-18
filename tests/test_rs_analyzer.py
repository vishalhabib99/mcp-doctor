from pathlib import Path
from textwrap import dedent

from mcp_doctor.analyzer import analyze_repo
from mcp_doctor.rs_analyzer import RUST_AVAILABLE, find_rust_tools

import pytest

pytestmark = pytest.mark.skipif(not RUST_AVAILABLE, reason="tree_sitter_rust not installed")

CARGO = '[package]\nname = "srv"\n\n[dependencies]\nrmcp = { version = "0.8", features = ["server"] }\n'


def write(tmp_path: Path, name: str, content: str) -> Path:
    p = tmp_path / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(dedent(content))
    return p


def server(tmp_path: Path, code: str, cargo: str = CARGO) -> dict:
    write(tmp_path, "Cargo.toml", cargo)
    write(tmp_path, "src/main.rs", code)
    findings, _ = find_rust_tools(tmp_path)
    return {t.name: t for t in findings}


def test_tool_attribute_with_documented_parameters(tmp_path):
    tools = server(tmp_path, """
        #[derive(Deserialize, JsonSchema)]
        pub struct SumRequest {
            /// the left hand side
            pub a: i32,
            #[schemars(description = "the right hand side")]
            pub b: Option<i32>,
        }

        #[tool_router]
        impl Calculator {
            #[tool(name = "sum", description = "Calculate the sum of two numbers")]
            async fn add(&self, Parameters(SumRequest { a, b }): Parameters<SumRequest>) -> String {
                (a + b.unwrap_or(0)).to_string()
            }
        }
        """)
    tool = tools["sum"]
    assert tool.description_text == "Calculate the sum of two numbers"
    assert tool.param_names == ["a", "b"]
    assert tool.required_param_names == ["a"]
    assert tool.params_tracked
    assert tool.issues == []


def test_name_defaults_to_function_and_description_to_doc_comments(tmp_path):
    tools = server(tmp_path, """
        #[tool_router]
        impl Counter {
            /// Increment the counter by one
            /// and return the new value.
            #[tool]
            fn increment(&self) -> String { String::new() }
        }
        """)
    tool = tools["increment"]
    assert tool.description_text == "Increment the counter by one\nand return the new value."
    assert tool.param_count == 0
    assert tool.params_tracked  # no Parameters<T>: rmcp gives it an empty schema


def test_description_argument_replaces_doc_comments(tmp_path):
    tools = server(tmp_path, """
        impl S {
            /// A long doc comment that rmcp never sends.
            #[rmcp::tool(description = "Ping")]
            fn ping(&self) -> String { String::new() }
        }
        """)
    assert tools["ping"].description_text == "Ping"
    assert [i.check for i in tools["ping"].issues] == ["description"]  # too short


def test_missing_description_and_undocumented_fields(tmp_path):
    tools = server(tmp_path, """
        #[derive(Deserialize, JsonSchema)]
        #[serde(rename_all = "camelCase")]
        pub struct Query {
            pub search_term: String,
            #[serde(default)]
            pub max_results: u32,
            /// skipped by serde, so not in the schema
            #[serde(skip)]
            pub cache: u8,
        }

        impl S {
            #[tool]
            async fn search(&self, Parameters(q): Parameters<Query>) -> String { String::new() }
        }
        """)
    tool = tools["search"]
    assert tool.param_names == ["searchTerm", "maxResults"]
    assert tool.required_param_names == ["searchTerm"]
    checks = sorted(i.check for i in tool.issues)
    assert checks == ["description", "param_docs"]
    assert "2/2 fields of Query" in next(i.message for i in tool.issues if i.check == "param_docs")


def test_flattened_struct_fields_are_read(tmp_path):
    tools = server(tmp_path, """
        #[derive(Deserialize, JsonSchema)]
        pub struct Paging {
            /// page number
            pub page: Option<u32>,
        }
        #[derive(Deserialize, JsonSchema)]
        pub struct ListArgs {
            /// what to list
            pub kind: String,
            #[serde(flatten)]
            pub paging: Paging,
        }
        impl S {
            /// List things of one kind.
            #[tool]
            fn list(&self, Parameters(a): Parameters<ListArgs>) -> String { String::new() }
        }
        """)
    assert tools["list"].param_names == ["kind", "page"]
    assert tools["list"].issues == []


def test_unreadable_description_and_parameters_are_not_guessed(tmp_path):
    tools = server(tmp_path, """
        const SHARED: &str = "Runs the shared query against the index.";
        impl<T: Tool> S<T> {
            #[tool(description = include_str!("doc.md"))]
            fn from_file(&self, Parameters(p): Parameters<T::Parameter>) -> String { String::new() }

            #[tool(description = SHARED)]
            fn from_const(&self, Parameters(p): Parameters<serde_json::Map<String, Value>>) -> String { String::new() }
        }
        """)
    from_file = tools["from_file"]
    assert from_file.has_description and from_file.description_text == ""
    assert not from_file.params_tracked and from_file.issues == []
    assert tools["from_const"].description_text == "Runs the shared query against the index."
    assert not tools["from_const"].params_tracked


def test_description_set_at_runtime_is_not_reported_missing(tmp_path):
    # genomoncology/biomcp and portofcontext/pctx leave #[tool] without a
    # description and write one into the router's tool list at runtime.
    code = """
        impl S {
            #[tool]
            fn search(&self, Parameters(q): Parameters<Query>) -> String { String::new() }
        }
        #[derive(Deserialize, JsonSchema)]
        pub struct Query { pub term: String }
        """
    tools = server(tmp_path, code + """
        pub fn apply<S>(router: &mut ToolRouter<S>) {
            for entry in TOOLS {
                let route = router.map.get_mut(entry.name).unwrap();
                route.attr.description = Some(Cow::Borrowed(entry.description));
            }
        }
        """)
    assert [i.check for i in tools["search"].issues] == ["param_docs"]  # field docs are still read
    tools = server(tmp_path, code)
    assert sorted(i.check for i in tools["search"].issues) == ["description", "param_docs"]


def test_tool_attribute_needs_rmcp_in_cargo(tmp_path):
    # `#[tool]` is only rmcp's when the crate depends on rmcp; table form counts too.
    code = """
        impl S {
            /// Does a thing for the caller.
            #[tool]
            fn thing(&self) -> String { String::new() }
        }
        """
    assert server(tmp_path, code, cargo='[package]\nname = "x"\n') == {}
    assert "thing" in server(tmp_path, code, cargo='[package]\nname = "x"\n\n[dependencies.rmcp]\nversion = "0.8"\n')


def test_cfg_test_code_and_test_files_are_skipped(tmp_path):
    write(tmp_path, "tests/it.rs", """
        impl S {
            #[tool(description = "test only server tool")]
            fn in_tests_dir(&self) -> String { String::new() }
        }
        """)
    tools = server(tmp_path, """
        #[cfg(test)]
        mod tests {
            impl S {
                #[tool(description = "test only server tool")]
                fn in_test_module(&self) -> String { String::new() }
            }
        }
        """)
    assert tools == {}


def test_json_tool_list_nested_in_vec_macro(tmp_path):
    # joaoh82/rustunnel and jahala/tilth: `vec![serde_json::json!({...}), ...]`,
    # where the inner json! is only tokens to tree-sitter, not a macro node.
    tools = server(tmp_path, r'''
        pub fn tool_list() -> Vec<Value> {
            vec![
                serde_json::json!({
                    "name": "create_tunnel",
                    "description": "Open a tunnel to a locally running service \
                        and get a public URL.",
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "local_port": { "type": "integer", "description": "Local port" },
                            "region": { "type": "string" },
                            "health_check": health_check_schema
                        },
                        "required": ["local_port"]
                    }
                }),
                json!({"name": "list_tunnels", "inputSchema": {"type": "object"}}),
            ]
        }

        fn server_info() -> Value {
            json!({"name": "rustunnel", "version": "1.0"})
        }

        fn anthropic_tool() -> Value {
            json!({"name": "expand", "description": "Not an MCP tool", "input_schema": {}})
        }
        ''', cargo='[package]\nname = "x"\n')
    assert set(tools) == {"create_tunnel", "list_tunnels"}
    create = tools["create_tunnel"]
    assert create.description_text == "Open a tunnel to a locally running service and get a public URL."
    assert create.param_names == ["local_port", "region", "health_check"]
    assert create.required_param_names == ["local_port"]
    assert "1/3 input-schema properties" in create.issues[0].message
    assert [i.check for i in tools["list_tunnels"].issues] == ["description"]


def test_tool_constructor_and_project_helper(tmp_path):
    # rmcp's Tool::new(name, description, schema), and yvgude/lean-ctx's
    # `tool_def(name, description, json!(...))` helper with the same order.
    tools = server(tmp_path, """
        fn defs() -> Vec<Tool> {
            vec![
                Tool::new("echo", "Echo the message back to the caller.", json_object(json!({
                    "type": "object",
                    "properties": {"message": {"type": "string", "description": "Text to echo"}}
                }))),
                tool_def(
                    "ctx_read",
                    "Read a file through the cache.",
                    json!({"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}),
                ),
                Tool::new(name.to_string(), description, schema),
                make_widget("not", "a tool", 3),
            ]
        }
        """)
    assert set(tools) == {"echo", "ctx_read"}
    assert tools["echo"].issues == []
    assert tools["ctx_read"].required_param_names == ["path"]
    assert [i.check for i in tools["ctx_read"].issues] == ["param_docs"]


def test_repo_checks_for_a_rust_server(tmp_path):
    write(tmp_path, "Cargo.toml", CARGO)
    write(tmp_path, "LICENSE", "MIT")
    write(tmp_path, "README.md", "ping")
    write(tmp_path, "src/main.rs", """
        impl S {
            /// Check the server is alive.
            #[tool]
            fn ping(&self) -> String { String::new() }
        }

        #[cfg(test)]
        mod tests {
            #[test]
            fn key_fixture() {
                let api_key = "sk-test-1234567890abcdef";
            }
        }
        """)
    report = analyze_repo(tmp_path)
    assert [t.name for t in report.tools] == ["ping"]
    checks = {i.check for i in report.repo_issues}
    assert "tests" not in checks  # inline #[cfg(test)] module
    assert "packaging" not in checks  # Cargo.toml
    assert "secrets" not in checks  # the key is in test code


@pytest.mark.parametrize("line", [
    'fn main() { let api_key = "sk-live-1234567890abcdef"; }',
    'const API_KEY: &str = "sk-live-1234567890abcdef";',
    "static TOKEN: &'static str = \"ghp_1234567890abcdefXYZ\";",
])
def test_secret_in_rust_source_is_flagged(tmp_path, line):
    write(tmp_path, "Cargo.toml", CARGO)
    write(tmp_path, "src/main.rs", line + "\n")
    report = analyze_repo(tmp_path)
    assert any(i.check == "secrets" for i in report.repo_issues)


def test_example_servers():
    examples = Path(__file__).resolve().parents[1] / "examples"
    good = analyze_repo(examples / "good_server_rs")
    assert [t.name for t in good.tools] == ["get_forecast"] and good.repo_issues == []
    assert all(not t.issues for t in good.tools)
    bad = analyze_repo(examples / "bad_server_rs")
    assert {i.check for t in bad.tools for i in t.issues} == {"description", "param_docs"}
    assert "secrets" in {i.check for i in bad.repo_issues}


def test_grammar_the_runtime_cannot_load_skips_rust(tmp_path, monkeypatch):
    # Pyodide's tree-sitter rejects ABI-15 grammars ("Incompatible Language
    # version 15"); that took down the whole /check/ scan of any repo with .rs files.
    import mcp_doctor.rs_analyzer as rs

    def incompatible(_ptr):
        raise ValueError("Incompatible Language version 15. Must be between 13 and 14")

    monkeypatch.setattr(rs, "Language", incompatible)
    write(tmp_path, "Cargo.toml", CARGO)
    write(tmp_path, "src/main.rs", 'const API_KEY: &str = "sk-live-1234567890abcdef";\n')
    assert rs.find_rust_tools(tmp_path) == ([], [])
    assert rs.test_line_ranges(b"fn main() {}") == []
    assert analyze_repo(tmp_path).tools == []


def test_tool_name_from_the_impls_own_name_method(tmp_path):
    # yvgude/lean-ctx: `tool_def(self.name(), "...", json!(...))` in an
    # `impl McpTool for X` whose `fn name` returns a literal.
    tools = server(tmp_path, """
        impl McpTool for CtxMemoryTool {
            fn name(&self) -> &'static str {
                "ctx_memory"
            }
            fn tool_def(&self) -> Tool {
                tool_def(self.name(), "Store and retrieve durable facts.", json!({"type": "object"}))
            }
        }
        impl McpTool for Computed {
            fn name(&self) -> &'static str { self.inner.name() }
            fn tool_def(&self) -> Tool {
                tool_def(self.name(), "Not resolvable statically.", json!({"type": "object"}))
            }
        }
        """)
    assert set(tools) == {"ctx_memory"}
