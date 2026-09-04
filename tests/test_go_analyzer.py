from pathlib import Path
from textwrap import dedent

from mcp_doctor.analyzer import analyze_repo
from mcp_doctor.go_analyzer import GO_AVAILABLE, find_go_tools

import pytest

pytestmark = pytest.mark.skipif(not GO_AVAILABLE, reason="tree_sitter_go not installed")


def write(tmp_path: Path, name: str, content: str) -> Path:
    p = tmp_path / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(dedent(content))
    return p


def test_struct_tag_inferred_schema_fully_documented(tmp_path):
    write(tmp_path, "server.go", """
        package main

        type greetArgs struct {
            Name string `json:"name" jsonschema:"the person to greet"`
        }

        func main() {
            mcp.AddTool(server, &mcp.Tool{
                Name:        "greet",
                Description: "Says hi to someone",
            }, func(ctx context.Context, req *mcp.CallToolRequest, args greetArgs) (*mcp.CallToolResult, any, error) {
                return nil, nil, nil
            })
        }
        """)
    findings, _ = find_go_tools(tmp_path)
    assert len(findings) == 1
    tool = findings[0]
    assert tool.name == "greet"
    assert tool.param_count == 1
    checks = {i.check for i in tool.issues}
    assert "description" not in checks
    assert "param_docs" not in checks


def test_struct_tag_partially_undocumented(tmp_path):
    write(tmp_path, "server.go", """
        package main

        type searchArgs struct {
            Query string `json:"query" jsonschema:"search terms"`
            Limit int    `json:"limit,omitempty"`
        }

        func main() {
            mcp.AddTool(server, &mcp.Tool{
                Name:        "search",
                Description: "Search for something",
            }, func(ctx context.Context, req *mcp.CallToolRequest, args searchArgs) (*mcp.CallToolResult, any, error) {
                return nil, nil, nil
            })
        }
        """)
    findings, _ = find_go_tools(tmp_path)
    tool = findings[0]
    assert tool.param_count == 2
    param_issue = next(i for i in tool.issues if i.check == "param_docs")
    assert "1/2" in param_issue.message


def test_no_args_tool_has_zero_params(tmp_path):
    write(tmp_path, "server.go", """
        package main

        func main() {
            mcp.AddTool(server, &mcp.Tool{
                Name:        "check_status",
                Description: "Checks the current status",
            }, func(ctx context.Context, req *mcp.CallToolRequest, args any) (*mcp.CallToolResult, any, error) {
                return nil, nil, nil
            })
        }
        """)
    findings, _ = find_go_tools(tmp_path)
    tool = findings[0]
    assert tool.param_count == 0
    assert not any(i.check == "param_docs" for i in tool.issues)


def test_explicit_input_schema_is_checked(tmp_path):
    write(tmp_path, "server.go", """
        package main

        func main() {
            mcp.AddTool(server, &mcp.Tool{
                Name:        "list_alerts",
                Description: "List dependabot alerts in a repository",
                InputSchema: &jsonschema.Schema{
                    Type: "object",
                    Properties: map[string]*jsonschema.Schema{
                        "owner": {Type: "string", Description: "The owner of the repository."},
                        "repo":  {Type: "string"},
                    },
                },
            }, handler)
        }
        """)
    findings, _ = find_go_tools(tmp_path)
    tool = findings[0]
    assert tool.param_count == 2
    param_issue = next(i for i in tool.issues if i.check == "param_docs")
    assert "1/2" in param_issue.message
    assert "input-schema properties" in param_issue.message


def test_missing_description_flagged(tmp_path):
    write(tmp_path, "server.go", """
        package main

        func main() {
            mcp.AddTool(server, &mcp.Tool{
                Name: "no_desc_tool",
            }, func(ctx context.Context, req *mcp.CallToolRequest, args any) (*mcp.CallToolResult, any, error) {
                return nil, nil, nil
            })
        }
        """)
    findings, _ = find_go_tools(tmp_path)
    tool = findings[0]
    assert any(i.check == "description" for i in tool.issues)


def test_dynamic_tool_name_is_skipped_not_crashed(tmp_path):
    write(tmp_path, "server.go", """
        package main

        func main() {
            for _, t := range tools {
                mcp.AddTool(server, &mcp.Tool{
                    Name:        t.Name,
                    Description: t.Description,
                }, t.Handler)
            }
        }
        """)
    findings, _ = find_go_tools(tmp_path)
    assert findings == []


def test_named_handler_function_is_resolved(tmp_path):
    write(tmp_path, "server.go", """
        package main

        type pingArgs struct {
            Message string `json:"message" jsonschema:"the message to echo"`
        }

        func handlePing(ctx context.Context, req *mcp.CallToolRequest, args pingArgs) (*mcp.CallToolResult, any, error) {
            return nil, nil, nil
        }

        func main() {
            mcp.AddTool(server, &mcp.Tool{
                Name:        "ping",
                Description: "Echoes a message back",
            }, handlePing)
        }
        """)
    findings, _ = find_go_tools(tmp_path)
    tool = findings[0]
    assert tool.param_count == 1
    assert not any(i.check == "param_docs" for i in tool.issues)


def test_same_struct_name_declared_twice_is_not_resolved(tmp_path):
    write(tmp_path, "a.go", """
        package main

        type argsType struct {
            X string `json:"x" jsonschema:"documented in file a"`
        }
        """)
    write(tmp_path, "b.go", """
        package main

        type argsType struct {
            Y string
        }

        func main() {
            mcp.AddTool(server, &mcp.Tool{
                Name:        "ambiguous_tool",
                Description: "Uses an ambiguous struct name",
            }, func(ctx context.Context, req *mcp.CallToolRequest, args argsType) (*mcp.CallToolResult, any, error) {
                return nil, nil, nil
            })
        }
        """)
    findings, _ = find_go_tools(tmp_path)
    tool = findings[0]
    assert tool.param_count == 0
    assert not any(i.check == "param_docs" for i in tool.issues)


def test_go_test_files_are_skipped(tmp_path):
    write(tmp_path, "server.go", """
        package main

        func main() {
            mcp.AddTool(server, &mcp.Tool{
                Name:        "real_tool",
                Description: "A real tool",
            }, handler)
        }
        """)
    write(tmp_path, "server_test.go", """
        package main

        func TestSomething() {
            mcp.AddTool(server, &mcp.Tool{
                Name:        "test_only_tool",
                Description: "Should not be counted",
            }, handler)
        }
        """)
    findings, _ = find_go_tools(tmp_path)
    assert len(findings) == 1
    assert findings[0].name == "real_tool"


def test_analyze_repo_includes_go_tools(tmp_path):
    write(tmp_path, "server.go", """
        package main

        func main() {
            mcp.AddTool(server, &mcp.Tool{
                Name:        "x",
                Description: "Does x thing",
            }, func(ctx context.Context, req *mcp.CallToolRequest, args any) (*mcp.CallToolResult, any, error) {
                return nil, nil, nil
            })
        }
        """)
    (tmp_path / "go.mod").write_text("module x\n\ngo 1.22\n")
    (tmp_path / "README.md").write_text("# x\n\nHas x tool.")
    (tmp_path / "LICENSE").write_text("MIT")
    report = analyze_repo(tmp_path)
    assert len(report.tools) == 1
    assert report.tools[0].name == "x"
    assert not any(i.check == "packaging" for i in report.repo_issues)


def test_handler_wrapped_in_helper_call_is_unwrapped(tmp_path):
    # A common real pattern (every tool in xpzouying/xiaohongshu-mcp does
    # this): the handler isn't passed directly, it's wrapped in a call to a
    # cross-cutting helper — panic recovery, logging, etc. — with the real
    # func_literal as one of that call's own arguments.
    write(tmp_path, "server.go", """
        package main

        type echoArgs struct {
            Message string `json:"message" jsonschema:"the message to echo"`
        }

        func withPanicRecovery[T any](name string, handler func(context.Context, *mcp.CallToolRequest, T) (*mcp.CallToolResult, any, error)) func(context.Context, *mcp.CallToolRequest, T) (*mcp.CallToolResult, any, error) {
            return handler
        }

        func main() {
            mcp.AddTool(server, &mcp.Tool{
                Name:        "echo",
                Description: "Echoes a message back",
            }, withPanicRecovery("echo", func(ctx context.Context, req *mcp.CallToolRequest, args echoArgs) (*mcp.CallToolResult, any, error) {
                return nil, nil, nil
            }))
        }
        """)
    findings, _ = find_go_tools(tmp_path)
    assert len(findings) == 1
    tool = findings[0]
    assert tool.param_count == 1
    assert not any(i.check == "param_docs" for i in tool.issues)


def test_handler_wrapped_in_helper_with_no_func_literal_is_not_crashed(tmp_path):
    write(tmp_path, "server.go", """
        package main

        func main() {
            mcp.AddTool(server, &mcp.Tool{
                Name:        "mystery",
                Description: "Uses a fully dynamic handler reference",
            }, wrapHandler(someDynamicLookup(toolID)))
        }
        """)
    findings, _ = find_go_tools(tmp_path)
    assert len(findings) == 1
    assert findings[0].param_count == 0


def test_mark3labs_builder_style_fully_documented(tmp_path):
    write(tmp_path, "server.go", """
        package main

        func main() {
            s.AddTool(mcp.NewTool("get_weather",
                mcp.WithDescription("Get the current weather for a city"),
                mcp.WithString("city",
                    mcp.Required(),
                    mcp.Description("The city to get weather for"),
                ),
                mcp.WithNumber("days",
                    mcp.Description("Number of forecast days"),
                ),
            ), weatherHandler)
        }
        """)
    findings, _ = find_go_tools(tmp_path)
    assert len(findings) == 1
    tool = findings[0]
    assert tool.name == "get_weather"
    assert tool.param_count == 2
    checks = {i.check for i in tool.issues}
    assert "description" not in checks
    assert "param_docs" not in checks


def test_mark3labs_builder_style_partially_undocumented(tmp_path):
    write(tmp_path, "server.go", """
        package main

        func main() {
            s.AddTool(mcp.NewTool("search",
                mcp.WithDescription("Search for something"),
                mcp.WithString("query", mcp.Required()),
                mcp.WithString("mode", mcp.Description("Search mode")),
            ), searchHandler)
        }
        """)
    findings, _ = find_go_tools(tmp_path)
    tool = findings[0]
    assert tool.param_count == 2
    param_issue = next(i for i in tool.issues if i.check == "param_docs")
    assert "1/2" in param_issue.message
    assert "builder options" in param_issue.message


def test_mark3labs_tool_name_resolved_via_const(tmp_path):
    write(tmp_path, "server.go", """
        package main

        const (
            ToolPing = "ping"
        )

        func main() {
            s.AddTool(mcp.NewTool(ToolPing,
                mcp.WithDescription("Pings the server"),
            ), pingHandler)
        }
        """)
    findings, _ = find_go_tools(tmp_path)
    assert len(findings) == 1
    assert findings[0].name == "ping"


def test_mark3labs_missing_description_flagged(tmp_path):
    write(tmp_path, "server.go", """
        package main

        func main() {
            s.AddTool(mcp.NewTool("no_desc"), handler)
        }
        """)
    findings, _ = find_go_tools(tmp_path)
    tool = findings[0]
    assert any(i.check == "description" for i in tool.issues)


def test_mark3labs_dynamic_name_is_skipped_not_crashed(tmp_path):
    write(tmp_path, "server.go", """
        package main

        func main() {
            for _, spec := range toolSpecs {
                s.AddTool(mcp.NewTool(spec.Name,
                    mcp.WithDescription(spec.Description),
                ), spec.Handler)
            }
        }
        """)
    findings, _ = find_go_tools(tmp_path)
    assert findings == []


def test_server_tool_struct_literal_is_recognized(tmp_path):
    # Verified against a real miss: hashicorp/terraform-mcp-server builds
    # every tool as mark3labs/mcp-go's own `server.ServerTool{Tool: ...,
    # Handler: ...}` struct, returned from a per-tool factory function and
    # registered elsewhere via `AddTool(tool.Tool, tool.Handler)` in a loop —
    # never a literal `AddTool(mcp.NewTool(...), handler)` call site.
    write(tmp_path, "get_state_version.go", """
        package tools

        func GetStateVersion(logger *log.Logger) server.ServerTool {
            return server.ServerTool{
                Tool: mcp.NewTool(
                    "get_state_version",
                    mcp.WithDescription("Retrieves a Terraform state version"),
                    mcp.WithString("state_version_id",
                        mcp.Description("Optional StateVersion id to fetch exact version"),
                    ),
                    mcp.WithString("workspace_id"),
                ),
                Handler: func(ctx context.Context, request mcp.CallToolRequest) (*mcp.CallToolResult, error) {
                    return getStateVersionWithIDHandler(ctx, request, logger)
                },
            }
        }
        """)
    findings, _ = find_go_tools(tmp_path)
    assert len(findings) == 1
    tool = findings[0]
    assert tool.name == "get_state_version"
    assert tool.param_count == 2
    checks = {i.check for i in tool.issues}
    assert "description" not in checks
    param_issue = next(i for i in tool.issues if i.check == "param_docs")
    assert "1/2" in param_issue.message


def test_server_tool_struct_with_dynamic_name_is_skipped(tmp_path):
    write(tmp_path, "server.go", """
        package tools

        func BuildTool(spec ToolSpec) server.ServerTool {
            return server.ServerTool{
                Tool: mcp.NewTool(spec.Name, mcp.WithDescription(spec.Description)),
                Handler: spec.Handler,
            }
        }
        """)
    findings, _ = find_go_tools(tmp_path)
    assert findings == []


def test_same_const_name_declared_twice_with_different_values_is_not_resolved(tmp_path):
    write(tmp_path, "a.go", """
        package main

        const ToolName = "from_file_a"
        """)
    write(tmp_path, "b.go", """
        package main

        const ToolName = "from_file_b"

        func main() {
            s.AddTool(mcp.NewTool(ToolName,
                mcp.WithDescription("Ambiguous tool name"),
            ), handler)
        }
        """)
    findings, _ = find_go_tools(tmp_path)
    assert findings == []


def test_factory_wrapped_tool_with_literal_name_is_detected(tmp_path):
    # github/github-mcp-server's own shape: a project-local generic `NewTool`
    # helper, not a literal `.AddTool(...)` call, wrapping the mcp.Tool literal.
    write(tmp_path, "server.go", """
        package main

        type searchArgs struct {
            Query string `json:"query" jsonschema:"search terms"`
        }

        func registerSearch() inventory.ServerTool {
            return NewTool(
                ToolsetMetadataSearch,
                mcp.Tool{
                    Name:        "search_repositories",
                    Description: "Search GitHub repositories",
                },
                scopes.PublicRead(scopes.Repo),
                func(ctx context.Context, deps ToolDependencies, req *mcp.CallToolRequest, args searchArgs) (*mcp.CallToolResult, any, error) {
                    return nil, nil, nil
                },
            )
        }
        """)
    findings, _ = find_go_tools(tmp_path)
    assert len(findings) == 1
    tool = findings[0]
    assert tool.name == "search_repositories"
    assert tool.param_count == 1
    checks = {i.check for i in tool.issues}
    assert "description" not in checks
    assert "param_docs" not in checks


def test_factory_wrapped_tool_with_dynamic_name_is_skipped(tmp_path):
    # The same factory called from a shared single-field-update helper (the
    # real `prUpdateTool` shape) — Name comes from a function parameter, not a
    # literal, so it can't be attributed to any one real tool and is skipped
    # rather than guessed at.
    write(tmp_path, "server.go", """
        package main

        func prUpdateTool(name string) inventory.ServerTool {
            return NewTool(
                ToolsetMetadataPullRequests,
                mcp.Tool{
                    Name:        name,
                    Description: "Updates a field on a pull request",
                },
                scopes.RequireAll(scopes.Repo),
                handler,
            )
        }
        """)
    findings, _ = find_go_tools(tmp_path)
    assert findings == []


def test_translation_helper_literal_fallback_resolved_as_description(tmp_path):
    # `t(key, defaultValue string) string` — verified against
    # github-mcp-server's own TranslationHelperFunc signature. The literal
    # fallback is the real text a model sees whenever the key isn't translated.
    write(tmp_path, "server.go", """
        package main

        func main() {
            s.AddTool(mcp.NewTool("list_labels",
                mcp.WithDescription(t("TOOL_LIST_LABELS_DESCRIPTION", "List labels in a repository")),
            ), handler)
        }
        """)
    findings, _ = find_go_tools(tmp_path)
    tool = findings[0]
    assert tool.description_text == "List labels in a repository"
    assert not any(i.check == "description" for i in tool.issues)


def test_translation_helper_dynamic_fallback_not_guessed(tmp_path):
    # When the fallback itself is a variable (built from runtime branching,
    # as in github-mcp-server's search_issues), it's genuinely unresolvable —
    # correctly left blank rather than fabricated.
    write(tmp_path, "server.go", """
        package main

        func registerSearchIssues() inventory.ServerTool {
            return NewTool(
                ToolsetMetadataIssues,
                mcp.Tool{
                    Name:        "search_issues",
                    Description: t("TOOL_SEARCH_ISSUES_DESCRIPTION", toolDescription),
                },
                scopes.PublicRead(scopes.Repo),
                handler,
            )
        }
        """)
    findings, _ = find_go_tools(tmp_path)
    tool = findings[0]
    assert tool.description_text == ""
    assert any(i.check == "description" for i in tool.issues)


def test_plus_concatenated_description_is_resolved(tmp_path):
    # A long description wrapped across multiple `+`-joined string literals —
    # verified against skyhook-io/radar's real tool descriptions, which use
    # this exact style for every tool and were all false-flagged as having
    # no description before this was resolved.
    write(tmp_path, "server.go", """
        package main

        func main() {
            addToolWithRegistry(reg, server, &mcp.Tool{
                Name: "get_dashboard",
                Description: "Use for inventory-style cluster health triage, like " +
                    "`kubectl get all` plus detected problems in one call. " +
                    "Returns resource counts and failing pods.",
            }, handler)
        }
        """)
    findings, _ = find_go_tools(tmp_path)
    tool = findings[0]
    assert tool.description_text == (
        "Use for inventory-style cluster health triage, like "
        "`kubectl get all` plus detected problems in one call. "
        "Returns resource counts and failing pods."
    )
    assert not any(i.check == "description" for i in tool.issues)


def test_concatenation_with_non_literal_operand_not_guessed(tmp_path):
    # A description built from a local variable, then concatenated with a
    # trailing literal — the variable's own value isn't tracked, so the
    # whole chain is genuinely unresolvable rather than partially guessed.
    write(tmp_path, "server.go", """
        package main

        func main() {
            addToolWithRegistry(reg, server, &mcp.Tool{
                Name: "diagnose",
                Description: diagnoseDescription + " Extra trailing detail.",
            }, handler)
        }
        """)
    findings, _ = find_go_tools(tmp_path)
    tool = findings[0]
    assert tool.description_text == ""
    assert any(i.check == "description" for i in tool.issues)


def test_bare_tool_in_typed_slice_literal_is_detected(tmp_path):
    # `[]*mcp.Tool{ {Name: ..., Description: ...}, ... }` — Go elides the
    # repeated `&mcp.Tool{...}` on each element. Verified against
    # clidey/whodb, where every tool group is defined this way and
    # registered elsewhere by a switch on tool.Name, never a literal
    # AddTool call — these were entirely invisible before.
    write(tmp_path, "server.go", """
        package main

        func toolDefinitions() []*mcp.Tool {
            return []*mcp.Tool{
                {Name: "list_things", Description: "List all the things."},
                {Name: "get_thing", Description: "Get one thing by id."},
            }
        }
        """)
    findings, _ = find_go_tools(tmp_path)
    names = {t.name: t for t in findings}
    assert set(names) == {"list_things", "get_thing"}
    assert names["list_things"].description_text == "List all the things."
    assert not any(i.check == "description" for i in names["list_things"].issues)
    # No handler is available at this site, so params are left unchecked,
    # not guessed — a 0 param_count here means "not checked", not "none".
    assert names["list_things"].param_count == 0


def test_tool_slice_description_resolves_through_const(tmp_path):
    # Long descriptions are commonly pulled into their own package-level
    # const (verified against whodb's descPlatformX constants) — a bare
    # identifier reference in the Description field, not a literal.
    write(tmp_path, "server.go", """
        package main

        const descListThings = `List all the things, with full detail.`

        func toolDefinitions() []*mcp.Tool {
            return []*mcp.Tool{
                {Name: "list_things", Description: descListThings},
            }
        }
        """)
    findings, _ = find_go_tools(tmp_path)
    tool = findings[0]
    assert tool.description_text == "List all the things, with full detail."
    assert not any(i.check == "description" for i in tool.issues)


def test_closure_factory_call_in_slice_is_resolved(tmp_path):
    # `read := func(name, description string) *mcp.Tool { return &mcp.Tool{
    # Name: name, Description: description} }`, then `read("x", "y")` used
    # positionally inside a `[]*mcp.Tool{...}` slice — verified against
    # whodb's `read(...)` helper, which alone defines 35 of its tools this
    # way. Only literal-string call args are resolved.
    write(tmp_path, "server.go", """
        package main

        func toolDefinitions() []*mcp.Tool {
            read := func(name, description string) *mcp.Tool {
                return &mcp.Tool{Name: name, Description: description, Annotations: readOnly(description)}
            }
            return []*mcp.Tool{
                read("list_things", "List all the things."),
                read("get_thing", "Get one thing by id."),
            }
        }
        """)
    findings, _ = find_go_tools(tmp_path)
    names = {t.name: t for t in findings}
    assert set(names) == {"list_things", "get_thing"}
    assert names["get_thing"].description_text == "Get one thing by id."
    assert not any(i.check == "description" for i in names["get_thing"].issues)


def test_closure_factory_call_with_non_literal_arg_not_guessed(tmp_path):
    # Same closure shape as above, but called with a variable rather than a
    # literal — genuinely unresolvable without tracking that variable's
    # value, so correctly left unresolved rather than guessed at.
    write(tmp_path, "server.go", """
        package main

        func toolDefinitions() []*mcp.Tool {
            read := func(name, description string) *mcp.Tool {
                return &mcp.Tool{Name: name, Description: description}
            }
            return []*mcp.Tool{
                read(dynamicName, "List all the things."),
            }
        }
        """)
    findings, _ = find_go_tools(tmp_path)
    assert findings == []


def test_closure_with_ambiguous_name_in_file_not_resolved(tmp_path):
    # Two differently-shaped `read := func(...)` closures in the same file —
    # same "don't guess on an ambiguous name" rule used by the const/struct
    # registries elsewhere in this module.
    write(tmp_path, "server.go", """
        package main

        func toolDefinitionsA() []*mcp.Tool {
            read := func(name, description string) *mcp.Tool {
                return &mcp.Tool{Name: name, Description: description}
            }
            return []*mcp.Tool{read("a", "desc a")}
        }

        func toolDefinitionsB() []*mcp.Tool {
            read := func(description, name string) *mcp.Tool {
                return &mcp.Tool{Name: name, Description: description}
            }
            return []*mcp.Tool{read("desc b", "b")}
        }
        """)
    findings, _ = find_go_tools(tmp_path)
    assert findings == []
