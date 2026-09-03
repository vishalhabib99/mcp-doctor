from pathlib import Path
from textwrap import dedent

from mcp_doctor.analyzer import analyze_repo
from mcp_doctor.ts_analyzer import TS_AVAILABLE, find_ts_tools

import pytest

pytestmark = pytest.mark.skipif(not TS_AVAILABLE, reason="tree_sitter not installed")


def write(tmp_path: Path, name: str, content: str) -> Path:
    p = tmp_path / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(dedent(content))
    return p


def test_register_tool_with_const_config_and_schema(tmp_path):
    write(tmp_path, "server.ts", """
        import { z } from "zod";
        import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";

        const GetSumSchema = z.object({
          a: z.number().describe("First number"),
          b: z.number().describe("Second number"),
        });

        const name = "get-sum";
        const config = {
          description: "Returns the sum of two numbers",
          inputSchema: GetSumSchema,
        };

        export const registerGetSumTool = (server) => {
          server.registerTool(name, config, async (args) => {
            const sum = args.a + args.b;
            return { content: [{ type: "text", text: String(sum) }] };
          });
        };
        """)
    findings, _ = find_ts_tools(tmp_path)
    assert len(findings) == 1
    tool = findings[0]
    assert tool.name == "get-sum"
    checks = {i.check for i in tool.issues}
    assert "description" not in checks
    assert "param_docs" not in checks
    assert "error_handling" in checks  # no try/catch in the handler


def test_lowlevel_tool_with_bare_shape_and_missing_docs(tmp_path):
    write(tmp_path, "server.ts", """
        import { z } from "zod";

        server.tool(
          "do_thing",
          "Does a thing",
          { x: z.string(), y: z.number().describe("the y value") },
          async (args) => {
            try {
              return { content: [{ type: "text", text: "ok" }] };
            } catch (e) {
              throw e;
            }
          }
        );
        """)
    findings, _ = find_ts_tools(tmp_path)
    assert len(findings) == 1
    tool = findings[0]
    assert tool.param_count == 2
    param_issue = next(i for i in tool.issues if i.check == "param_docs")
    assert "1/2" in param_issue.message
    assert not any(i.check == "error_handling" for i in tool.issues)


def test_missing_description_flagged(tmp_path):
    write(tmp_path, "server.ts", """
        server.registerTool("no_desc_tool", { description: "", inputSchema: z.object({}) }, async () => {
          return { content: [] };
        });
        """)
    findings, _ = find_ts_tools(tmp_path)
    tool = findings[0]
    assert any(i.check == "description" for i in tool.issues)


def test_dynamic_tool_name_is_skipped_not_crashed(tmp_path):
    write(tmp_path, "server.ts", """
        for (const t of tools) {
          server.registerTool(t.name, t.config, t.handler);
        }
        """)
    findings, _ = find_ts_tools(tmp_path)
    assert findings == []


def test_override_name_with_literal_fallback_uses_the_fallback(tmp_path):
    write(tmp_path, "server.ts", """
        server.tool(
          toolName || "web_search_exa",
          "Search the web",
          { query: z.string().describe("Search query") },
          async ({ query }) => {
            return { content: [] };
          },
        );
        """)
    findings, _ = find_ts_tools(tmp_path)
    assert len(findings) == 1
    assert findings[0].name == "web_search_exa"


def test_fastmcp_add_tool_single_object_style(tmp_path):
    write(tmp_path, "server.ts", """
        import { z } from "zod";

        server.addTool({
          name: "firecrawl_scrape",
          annotations: { title: "Scrape a URL", readOnlyHint: true },
          description: "Retrieve and extract content from one supplied URL.",
          parameters: z.object({
            url: z.string().describe("The URL to scrape"),
            maxAge: z.number().describe("Cache age in ms"),
          }),
          execute: async (args, { session, log }) => {
            log.info("scraping", { url: args.url });
            return String(args.url);
          },
        });
        """)
    findings, _ = find_ts_tools(tmp_path)
    assert len(findings) == 1
    tool = findings[0]
    assert tool.name == "firecrawl_scrape"
    checks = {i.check for i in tool.issues}
    assert "description" not in checks
    assert "param_docs" not in checks
    assert "error_handling" in checks  # no try/catch in execute


def test_fastmcp_add_tool_with_const_schema_and_missing_docs(tmp_path):
    write(tmp_path, "server.ts", """
        import { z } from "zod";

        const MapSchema = z.object({
          search: z.string(),
        });

        server.addTool({
          name: "firecrawl_map",
          description: "",
          parameters: MapSchema,
          execute: async (args) => {
            try {
              return String(args.search);
            } catch (e) {
              throw e;
            }
          },
        });
        """)
    findings, _ = find_ts_tools(tmp_path)
    tool = findings[0]
    checks = {i.check for i in tool.issues}
    assert "description" in checks
    assert "param_docs" in checks  # search has no .describe(...)
    assert "error_handling" not in checks


def test_add_tool_dynamic_config_is_skipped_not_crashed(tmp_path):
    write(tmp_path, "server.ts", """
        for (const t of tools) {
          registrar.addTool(t);
        }
        """)
    findings, _ = find_ts_tools(tmp_path)
    assert findings == []


def test_cross_file_member_expression_name_and_description(tmp_path):
    write(tmp_path, "tools/get-figma-data-tool.ts", """
        import { z } from "zod";

        const parametersSchema = z.object({
          fileKey: z.string().describe("The Figma file key"),
        });

        export const getFigmaDataTool = {
          name: "get_figma_data",
          description: "Get comprehensive Figma file data",
          parametersSchema,
          handler: async () => {},
        } as const;
        """)
    write(tmp_path, "server.ts", """
        import { getFigmaDataTool } from "./tools/get-figma-data-tool.js";

        server.registerTool(
          getFigmaDataTool.name,
          {
            title: "Get Figma Data",
            description: getFigmaDataTool.description,
            inputSchema: getFigmaDataTool.parametersSchema,
          },
          async (params) => getFigmaDataTool.handler(params),
        );
        """)
    findings, _ = find_ts_tools(tmp_path)
    assert len(findings) == 1
    tool = findings[0]
    assert tool.name == "get_figma_data"
    checks = {i.check for i in tool.issues}
    assert "description" not in checks
    assert "param_docs" not in checks


def test_cross_file_dynamic_description_is_skipped_not_crashed(tmp_path):
    # `fooTool.getDescription(x)` — a genuine runtime call, not a property
    # access. Must not be resolved to a false description; still a real,
    # correctly-attributed finding (not a dropped tool).
    write(tmp_path, "tools/download-tool.ts", """
        function getDescription(dir) {
          return dir ? `Download to ${dir}` : "Download images";
        }

        export const downloadTool = {
          name: "download_figma_images",
          getDescription,
        } as const;
        """)
    write(tmp_path, "server.ts", """
        import { downloadTool } from "./tools/download-tool.js";

        server.registerTool(
          downloadTool.name,
          {
            description: downloadTool.getDescription(options.imageDir),
            inputSchema: z.object({}),
          },
          async () => {},
        );
        """)
    findings, _ = find_ts_tools(tmp_path)
    assert len(findings) == 1
    tool = findings[0]
    assert tool.name == "download_figma_images"
    assert any(i.check == "description" for i in tool.issues)


def test_low_level_server_list_tools_handler(tmp_path):
    write(tmp_path, "shared/tools.ts", """
        export const TOOL_NAMES = {
          BROWSER: {
            GET_TABS: "get_windows_and_tabs",
          },
        };

        export const TOOL_SCHEMAS = [
          {
            name: TOOL_NAMES.BROWSER.GET_TABS,
            description: "Get all currently open browser windows and tabs",
            inputSchema: {
              type: "object",
              properties: {
                verbose: { type: "boolean", description: "Include full tab metadata" },
              },
              required: [],
            },
          },
          {
            name: "chrome_navigate",
            description: "",
            inputSchema: { type: "object", properties: {}, required: [] },
          },
        ];
        """)
    write(tmp_path, "server.ts", """
        import { ListToolsRequestSchema, CallToolRequestSchema } from "@modelcontextprotocol/sdk/types.js";
        import { TOOL_SCHEMAS } from "./shared/tools.js";

        async function listDynamicTools() {
          return [];
        }

        export const setupTools = (server) => {
          server.setRequestHandler(ListToolsRequestSchema, async () => {
            const dynamicTools = await listDynamicTools();
            return { tools: [...TOOL_SCHEMAS, ...dynamicTools] };
          });
          server.setRequestHandler(CallToolRequestSchema, async (request) => handle(request));
        };
        """)
    findings, _ = find_ts_tools(tmp_path)
    assert len(findings) == 2
    by_name = {t.name: t for t in findings}
    assert set(by_name) == {"get_windows_and_tabs", "chrome_navigate"}

    tabs_tool = by_name["get_windows_and_tabs"]
    assert tabs_tool.file == "shared/tools.ts"  # reported at its own definition, not the call site
    assert tabs_tool.param_count == 1
    checks = {i.check for i in tabs_tool.issues}
    assert "description" not in checks
    assert "param_docs" not in checks
    assert "error_handling" not in checks  # no per-tool handler to inspect for this style

    nav_tool = by_name["chrome_navigate"]
    assert any(i.check == "description" for i in nav_tool.issues)


def test_low_level_server_list_tools_deduped_across_call_sites(tmp_path):
    # The same static tool array wired into two transport entrypoints (a real
    # pattern: separate stdio/HTTP servers sharing one tool list) must be
    # reported once per tool, not once per call site.
    write(tmp_path, "shared/tools.ts", """
        export const TOOL_SCHEMAS = [
          { name: "chrome_screenshot", description: "Take a screenshot", inputSchema: { type: "object", properties: {}, required: [] } },
        ];
        """)
    write(tmp_path, "stdio-server.ts", """
        import { ListToolsRequestSchema } from "@modelcontextprotocol/sdk/types.js";
        import { TOOL_SCHEMAS } from "./shared/tools.js";
        server.setRequestHandler(ListToolsRequestSchema, async () => ({ tools: [...TOOL_SCHEMAS] }));
        """)
    write(tmp_path, "http-server.ts", """
        import { ListToolsRequestSchema } from "@modelcontextprotocol/sdk/types.js";
        import { TOOL_SCHEMAS } from "./shared/tools.js";
        server.setRequestHandler(ListToolsRequestSchema, async () => ({ tools: [...TOOL_SCHEMAS] }));
        """)
    findings, _ = find_ts_tools(tmp_path)
    assert len(findings) == 1
    assert findings[0].name == "chrome_screenshot"


def test_list_tools_handler_with_filter_and_template_description(tmp_path):
    # Two real patterns found dogfooding wonderwhy-er/DesktopCommanderMCP:
    # (1) the base tools array is filtered before being returned, which must
    #     not make the whole list look dynamic; (2) a description built as a
    #     template literal with one interpolated suffix must not be discarded
    #     entirely just because it isn't a fully static string.
    write(tmp_path, "server.ts", """
        import { ListToolsRequestSchema } from "@modelcontextprotocol/sdk/types.js";

        const CMD_PREFIX = `Use with care.`;

        function shouldIncludeTool(name) {
          return true;
        }

        server.setRequestHandler(ListToolsRequestSchema, async () => {
          const allTools = [
            {
              name: "read_file",
              description: `Read a file from disk. ${CMD_PREFIX}`,
              inputSchema: { type: "object", properties: {}, required: [] },
            },
          ];
          const filteredTools = allTools.filter(tool => shouldIncludeTool(tool.name));
          return { tools: filteredTools };
        });
        """)
    findings, _ = find_ts_tools(tmp_path)
    assert len(findings) == 1
    tool = findings[0]
    assert tool.name == "read_file"
    assert not any(i.check == "description" for i in tool.issues)


def test_list_tools_handler_zod_to_json_schema_unwrapped(tmp_path):
    # `inputSchema: zodToJsonSchema(SomeArgsSchema)` — the zod-to-json-schema
    # package, used to keep one Zod source of truth while serving raw JSON
    # Schema over the low-level SDK. Must still check param docs by unwrapping
    # to the underlying Zod schema, not go blind because it's not a literal.
    write(tmp_path, "schemas.ts", """
        import { z } from "zod";

        export const ReadFileArgsSchema = z.object({
          path: z.string().describe("Path to the file"),
          offset: z.number().optional(),
        });
        """)
    write(tmp_path, "server.ts", """
        import { ListToolsRequestSchema } from "@modelcontextprotocol/sdk/types.js";
        import { zodToJsonSchema } from "zod-to-json-schema";
        import { ReadFileArgsSchema } from "./schemas.js";

        server.setRequestHandler(ListToolsRequestSchema, async () => ({
          tools: [
            {
              name: "read_file",
              description: "Read a file from disk.",
              inputSchema: zodToJsonSchema(ReadFileArgsSchema),
            },
          ],
        }));
        """)
    findings, _ = find_ts_tools(tmp_path)
    assert len(findings) == 1
    tool = findings[0]
    assert tool.param_count == 2
    param_issue = next(i for i in tool.issues if i.check == "param_docs")
    assert "1/2" in param_issue.message
    assert "Zod schema properties" in param_issue.message


def test_analyze_repo_includes_ts_tools(tmp_path):
    write(tmp_path, "server.ts", """
        server.registerTool("x", { description: "Does x thing", inputSchema: z.object({}) }, async () => {
          try {
            return { content: [] };
          } catch (e) {
            throw e;
          }
        });
        """)
    (tmp_path / "package.json").write_text('{"name": "x"}')
    (tmp_path / "README.md").write_text("# x\n\nHas x tool.")
    (tmp_path / "LICENSE").write_text("MIT")
    report = analyze_repo(tmp_path)
    assert len(report.tools) == 1
    assert report.tools[0].name == "x"


def test_define_tool_with_direct_object_literal(tmp_path):
    write(tmp_path, "server.ts", """
        import { zod } from "./third_party";
        import { defineTool } from "./ToolDefinition";

        export const selectPage = defineTool({
          name: "select_page",
          description: "Select a page as a context for future tool calls.",
          schema: {
            pageId: zod.number().describe("The ID of the page to select."),
          },
          handler: async (request, response, context) => {
            try {
              return {};
            } catch (e) {
              throw e;
            }
          },
        });
        """)
    findings, _ = find_ts_tools(tmp_path)
    assert len(findings) == 1
    tool = findings[0]
    assert tool.name == "select_page"
    checks = {i.check for i in tool.issues}
    assert "description" not in checks
    assert "param_docs" not in checks
    assert "error_handling" not in checks


def test_define_tool_with_factory_function_and_missing_error_handling(tmp_path):
    write(tmp_path, "server.ts", """
        import { defineTool } from "./ToolDefinition";

        export const listPages = defineTool(args => {
          return {
            name: "list_pages",
            description: "Get a list of pages open in the browser.",
            schema: {},
            handler: async (_request, response) => {
              response.setIncludePages(true);
            },
          };
        });
        """)
    findings, _ = find_ts_tools(tmp_path)
    assert len(findings) == 1
    tool = findings[0]
    assert tool.name == "list_pages"
    assert any(i.check == "error_handling" for i in tool.issues)


def test_define_page_tool_wrapper_is_recognized(tmp_path):
    write(tmp_path, "server.ts", """
        import { definePageTool } from "./ToolDefinition";

        export const closePage = definePageTool({
          name: "close_page",
          description: "",
          schema: { pageId: zod.number() },
          handler: async () => ({}),
        });
        """)
    findings, _ = find_ts_tools(tmp_path)
    assert len(findings) == 1
    tool = findings[0]
    assert tool.name == "close_page"
    assert any(i.check == "description" for i in tool.issues)
    param_issue = next(i for i in tool.issues if i.check == "param_docs")
    assert "1/1" in param_issue.message


def test_define_tool_dynamic_factory_body_is_skipped_not_crashed(tmp_path):
    write(tmp_path, "server.ts", """
        import { defineTool } from "./ToolDefinition";

        export const conditionalTool = defineTool(args => {
          if (args?.slim) {
            return buildSlimTool();
          }
          return buildFullTool();
        });
        """)
    findings, _ = find_ts_tools(tmp_path)
    assert findings == []


def test_description_built_with_string_concatenation(tmp_path):
    write(tmp_path, "server.ts", """
        import { defineTool } from "./ToolDefinition";

        export const installPwa = defineTool({
          name: "install_pwa",
          description:
            "Installs a Progressive Web App identified by its manifest ID. " +
            "This installs through the PWA CDP domain without a user gesture.",
          schema: {},
          handler: async () => ({}),
        });
        """)
    findings, _ = find_ts_tools(tmp_path)
    assert len(findings) == 1
    tool = findings[0]
    assert not any(i.check == "description" for i in tool.issues)
    assert tool.description_len > 10


def test_shared_const_schema_describe_is_resolved_through_identifier(tmp_path):
    write(tmp_path, "server.ts", """
        import { zod } from "./third_party";
        import { defineTool } from "./ToolDefinition";

        const manifestIdSchema = zod
          .string()
          .describe("The manifest ID of the web app.");

        export const installPwa = defineTool({
          name: "install_pwa",
          description: "Installs a PWA.",
          schema: {
            manifestId: manifestIdSchema,
            installUrl: zod.string().describe("The install URL."),
          },
          handler: async () => ({}),
        });
        """)
    findings, _ = find_ts_tools(tmp_path)
    assert len(findings) == 1
    tool = findings[0]
    assert not any(i.check == "param_docs" for i in tool.issues)


def test_shared_const_schema_without_describe_still_flagged(tmp_path):
    write(tmp_path, "server.ts", """
        import { zod } from "./third_party";
        import { defineTool } from "./ToolDefinition";

        const undocumentedSchema = zod.string();

        export const installPwa = defineTool({
          name: "install_pwa",
          description: "Installs a PWA.",
          schema: {
            manifestId: undocumentedSchema,
          },
          handler: async () => ({}),
        });
        """)
    findings, _ = find_ts_tools(tmp_path)
    tool = findings[0]
    param_issue = next(i for i in tool.issues if i.check == "param_docs")
    assert "1/1" in param_issue.message


def test_shorthand_tools_property_is_resolved(tmp_path):
    write(tmp_path, "server.ts", """
        const tools = [
          { name: "a_tool", description: "Does a thing", inputSchema: { type: "object", properties: {} } },
        ];

        server.setRequestHandler(ListToolsRequestSchema, async () => {
          return { tools };
        });
        """)
    findings, _ = find_ts_tools(tmp_path)
    assert len(findings) == 1
    assert findings[0].name == "a_tool"


def test_same_name_declared_twice_in_one_file_is_not_resolved(tmp_path):
    # `tools` here refers to two unrelated local variables in two different
    # functions — the name-based registry can't tell them apart, so it must
    # not silently resolve to whichever one it happened to see last.
    write(tmp_path, "server.ts", """
        function unrelated() {
          const tools = this.repository.getAITools();
          return tools;
        }

        const realTools = [
          { name: "a_tool", description: "Does a thing", inputSchema: { type: "object", properties: {} } },
        ];

        server.setRequestHandler(ListToolsRequestSchema, async () => {
          const tools = realTools;
          return { tools };
        });
        """)
    findings, _ = find_ts_tools(tmp_path)
    assert findings == []


def test_tool_inside_a_plain_tests_directory_is_excluded(tmp_path):
    # A plain `tests/` directory (pytest-style, not Jest's `__tests__/`) with a
    # filename that itself contains neither "test" nor "spec" — verified
    # against a real miss: mcp-use/mcp-use's `tests/servers/simple_server.ts`,
    # a genuine integration-test fixture that slipped past both checks. Same
    # directory-based exclusion the Python analyzer already applies.
    write(tmp_path, "tests/servers/simple_server.ts", """
        server.setRequestHandler(ListToolsRequestSchema, async () => ({
          tools: [
            { name: "add", description: "Add two numbers", inputSchema: { type: "object", properties: {} } },
          ],
        }));
        """)
    findings, _ = find_ts_tools(tmp_path)
    assert findings == []
