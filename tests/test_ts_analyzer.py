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
    # correctly-attributed finding (not a dropped tool). Since v1.12.5 an
    # unresolvable description is unknown, not missing, so it isn't flagged
    # (this used to assert a "no description" error, a false positive).
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
    assert not any(i.check == "description" for i in tool.issues)


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


def test_trimmed_template_description_is_resolved(tmp_path):
    # Verified against a real 7/8-tools-missing-description false positive:
    # brave/brave-search-mcp-server declares every tool's description as
    # `` `...multi-line text...`.trim() `` — a real, common idiom for
    # trimming leading/trailing whitespace off an indented template literal
    # — then references it by identifier at the registerTool call site. The
    # `.trim()` call wasn't recognized as anything but a dynamic value.
    write(tmp_path, "tool.ts", """
        export const name = 'brave_place_search';

        export const description = `
            Searches Brave's Place Search API.
        `.trim();

        export const register = (mcpServer) => {
          mcpServer.registerTool(
            name,
            {
              description: description,
              inputSchema: { type: "object", properties: {}, required: [] },
            },
            execute
          );
        };
        """)
    findings, _ = find_ts_tools(tmp_path)
    assert len(findings) == 1
    tool = findings[0]
    assert tool.has_description is True
    assert "Searches Brave's Place Search API" in tool.description_text
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


def test_list_tools_handler_object_values_namespace_import(tmp_path):
    # `tools: Object.values(tools)` where `tools` is `import * as tools from
    # "./tools.js"` and that module exports one `const` object per tool —
    # verified against the real zcaceres/markdownify-mcp (11 tools, all
    # previously invisible: "0 tool(s) found") and flesler/mcp-tasks. Distinct
    # from the already-supported `[...ToolArrayConst]` spread style: there's no
    # array literal anywhere, only a namespace object turned into one via
    # `Object.values`.
    write(tmp_path, "tools.ts", """
        import { ToolSchema } from "@modelcontextprotocol/sdk/types.js";

        export const PdfToMarkdownTool = ToolSchema.parse({
          name: "pdf-to-markdown",
          description: "Convert a PDF file to markdown",
          inputSchema: {
            type: "object",
            properties: {
              filepath: { type: "string", description: "Absolute path of the PDF file" },
            },
            required: ["filepath"],
          },
        });

        export const GetMarkdownFileTool = ToolSchema.parse({
          name: "get-markdown-file",
          description: "",
          inputSchema: { type: "object", properties: {}, required: [] },
        });
        """)
    write(tmp_path, "server.ts", """
        import { ListToolsRequestSchema } from "@modelcontextprotocol/sdk/types.js";
        import * as tools from "./tools.js";

        server.setRequestHandler(ListToolsRequestSchema, async () => {
          return { tools: Object.values(tools) };
        });
        """)
    findings, _ = find_ts_tools(tmp_path)
    assert len(findings) == 2
    by_name = {t.name: t for t in findings}
    assert set(by_name) == {"pdf-to-markdown", "get-markdown-file"}
    assert by_name["pdf-to-markdown"].file == "tools.ts"  # reported at its own definition
    assert any(i.check == "description" for i in by_name["get-markdown-file"].issues)


def test_list_tools_handler_object_values_unresolvable_import_skipped(tmp_path):
    # `Object.values(tools)` where `tools` comes from a bare-specifier package
    # import (not a same-repo relative path) can't be traced to any source this
    # analyzer can read — must be skipped like any other genuinely dynamic
    # value, not crash or guess.
    write(tmp_path, "server.ts", """
        import { ListToolsRequestSchema } from "@modelcontextprotocol/sdk/types.js";
        import * as tools from "some-external-package";

        server.setRequestHandler(ListToolsRequestSchema, async () => ({
          tools: Object.values(tools),
        }));
        """)
    findings, _ = find_ts_tools(tmp_path)
    assert findings == []


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


def test_account_tool_style_is_recognized(tmp_path):
    # Verified against a real miss: cloudflare/mcp-server-cloudflare registers
    # roughly 60 of its ~140 tools via a `context.accountTool(name, config,
    # handler)` method that wraps `registerTool` internally with the identical
    # `{ description, inputSchema }` config shape (registration-context.ts) —
    # a distinct method name mcp-doctor's REGISTER_METHODS didn't recognize.
    write(tmp_path, "ai-gateway.tools.ts", """
        import { z } from "zod";

        export function registerAIGatewayTools(context) {
          context.accountTool(
            "list_gateways",
            {
              description: "List Gateways",
              inputSchema: z.object({ page: z.number() }),
            },
            async (params, accountId) => {
              try {
                return { content: [{ type: "text", text: "ok" }] };
              } catch (error) {
                return { content: [{ type: "text", text: String(error) }], isError: true };
              }
            }
          );
        }
        """)
    findings, _ = find_ts_tools(tmp_path)
    assert len(findings) == 1
    tool = findings[0]
    assert tool.name == "list_gateways"
    checks = {i.check for i in tool.issues}
    assert "description" not in checks
    assert "error_handling" not in checks
    # page has no .describe(...) — should still be flagged like any other tool
    assert "param_docs" in checks


def test_bare_typed_const_tool_object_is_recognized(tmp_path):
    # Verified against a real miss: browserbase/mcp-server-browserbase defines
    # every tool as a bare `const xTool: Tool<...> = { schema: ..., handle: ... }`
    # object — no wrapping call anywhere — and registers it via a runtime
    # `.forEach()` over a collected array with only property-accessed args
    # (`server.tool(tool.schema.name, ...)`), genuinely unresolvable at that
    # call site. `schema` is itself a separately-declared const object
    # reference, and `handle` references a named `function` declaration
    # (not an inline arrow function) — both had to be resolved.
    write(tmp_path, "navigate.ts", """
        import { z } from "zod";

        const NavigateInputSchema = z.object({
          url: z.string().min(1),
        });

        const navigateSchema = {
          name: "navigate",
          description: "Navigate to a URL",
          inputSchema: NavigateInputSchema,
        };

        async function handleNavigate(context, params) {
          const action = async () => {
            try {
              return { content: [{ type: "text", text: "ok" }] };
            } catch (error) {
              throw new Error(`Failed to navigate: ${error}`);
            }
          };
          return { action, waitForNetwork: false };
        }

        const navigateTool = {
          capability: "core",
          schema: navigateSchema,
          handle: handleNavigate,
        };

        export default navigateTool;
        """)
    findings, _ = find_ts_tools(tmp_path)
    assert len(findings) == 1
    tool = findings[0]
    assert tool.name == "navigate"
    checks = {i.check for i in tool.issues}
    assert "description" not in checks
    assert "param_docs" in checks  # url has no .describe(...)
    # try/catch lives inside the nested `action` closure the handler
    # returns, not in handleNavigate's own top-level statements — the
    # subtree-wide try_statement search should still find it.
    assert "error_handling" not in checks


def test_bare_typed_const_tool_object_dynamic_name_is_skipped(tmp_path):
    write(tmp_path, "dynamic.ts", """
        const dynamicTool = {
          capability: "core",
          schema: buildSchema(),
          handle: someHandler,
        };
        """)
    findings, _ = find_ts_tools(tmp_path)
    assert findings == []


def test_bare_const_array_value_is_not_mistaken_for_a_tool(tmp_path):
    # `const TOOLS = [navigateTool, actTool]` is a variable_declarator too,
    # but its value is an array, not an object with schema/handle fields —
    # must not be treated as (or double-count) a tool definition.
    write(tmp_path, "index.ts", """
        import navigateTool from "./navigate.js";
        import actTool from "./act.js";

        export const TOOLS = [navigateTool, actTool];
        """)
    findings, _ = find_ts_tools(tmp_path)
    assert findings == []


def test_cjk_description_is_not_falsely_flagged_as_too_short(tmp_path):
    # Same real bug as the Python/Go analyzers (found on
    # xpzouying/xiaohongshu-mcp, 15.6k★): a complete, well-formed Chinese
    # description is only 9 raw characters, under the 10-char threshold
    # calibrated for English character density. Fixed via
    # description_display_width (wcwidth-style: a Wide/Fullwidth char
    # counts as 2 columns), shared across all three language analyzers.
    write(tmp_path, "server.ts", """
        server.registerTool("check_login", { description: "检查小红书登录状态", inputSchema: z.object({}) }, async () => {
          return { content: [] };
        });
        """)
    findings, _ = find_ts_tools(tmp_path)
    tool = findings[0]
    assert not any(i.check == "description" for i in tool.issues)


def test_tool_definition_objects_registered_by_a_loop_are_recognized(tmp_path):
    # Verified against a real miss: bruchris/canvas-lms-mcp (scan request #3)
    # returns `ToolDefinition[]` arrays of plain objects and registers them in
    # a loop, `server.registerTool(tool.name, ...)`, whose property-accessed
    # args can't be resolved there. mcp-doctor found 0 of its 165 tools.
    write(tmp_path, "src/tools/assignments.ts", """
        import { z } from 'zod'
        import type { ToolDefinition } from './types'

        export function assignmentTools(canvas): ToolDefinition[] {
          return [
            {
              name: 'list_assignments',
              description: 'List assignments in a course, optionally filtered by bucket.',
              inputSchema: {
                course_id: z.number().describe('Canvas course ID'),
                bucket: z.string().optional(),
              },
              handler: async (params) => canvas.assignments.list(params.course_id),
            },
            {
              name: 'get_assignment',
              description: 'Get one assignment by ID.',
              inputSchema: { course_id: z.number().describe('Canvas course ID') },
              handler: async (params) => canvas.assignments.get(params.course_id),
            },
          ]
        }
        """)
    write(tmp_path, "src/tools/index.ts", """
        export function registerAllTools(server, canvas) {
          for (const tool of getAllTools(canvas)) {
            server.registerTool(
              tool.name,
              { title: tool.title, description: tool.description, inputSchema: tool.inputSchema },
              buildHandler(tool),
            )
          }
        }
        """)
    findings, _ = find_ts_tools(tmp_path)
    by_name = {f.name: f for f in findings}
    assert set(by_name) == {"list_assignments", "get_assignment"}
    listing = by_name["list_assignments"]
    assert listing.param_count == 2
    assert {i.check for i in listing.issues} == {"param_docs"}  # bucket has no .describe
    assert by_name["get_assignment"].issues == []
    # The catch lives in the loop's shared wrapper, not each handler.
    assert all(i.check != "error_handling" for f in findings for i in f.issues)


def test_tool_definition_object_already_registered_directly_is_not_double_counted(tmp_path):
    write(tmp_path, "server.ts", """
        import { z } from 'zod'

        const echoTool = {
          name: 'echo',
          description: 'Echo the text back unchanged.',
          inputSchema: { text: z.string().describe('Text to echo') },
          handler: async ({ text }) => ({ content: [{ type: 'text', text }] }),
        }

        server.registerTool('echo', { description: echoTool.description, inputSchema: echoTool.inputSchema }, echoTool.handler)
        """)
    findings, _ = find_ts_tools(tmp_path)
    assert [f.name for f in findings] == ["echo"]


def test_object_with_tool_keys_passed_to_an_unrelated_call_is_not_a_tool(tmp_path):
    # Only objects sitting directly in an array or a const count; a same-shaped
    # object passed as a call argument (a log line, a test double) doesn't.
    write(tmp_path, "log.ts", """
        logger.info({ name: 'startup', description: 'booted', inputSchema: {}, handler: 'none' })
        """)
    findings, _ = find_ts_tools(tmp_path)
    assert findings == []


def test_mcp_ts_core_tool_definitions_are_recognized(tmp_path):
    # Verified against a real miss: cyanheads/clinicaltrialsgov-mcp-server
    # (0 of 7 found). `@cyanheads/mcp-ts-core`'s `tool(name, { description,
    # input, handler })` style is used by 30+ public servers.
    write(tmp_path, "src/tools/get-study-count.tool.ts", """
        import { tool, z } from '@cyanheads/mcp-ts-core';

        export const getStudyCount = tool('clinicaltrials_get_study_count', {
          description: `Get the total study count matching a query, without fetching study data.`,
          annotations: { readOnlyHint: true },
          input: z.object({
            query: z.string().optional().describe('Free-text search across all fields.'),
            phase: z.string().optional(),
          }),
          async handler(input, ctx) {
            if (!input.query) throw new Error('blank');
            return { total: 1 };
          },
        });
        """)
    findings, _ = find_ts_tools(tmp_path)
    assert [f.name for f in findings] == ["clinicaltrials_get_study_count"]
    tool = findings[0]
    assert tool.param_count == 2
    # The framework catches what handlers throw, so no error_handling warning.
    assert {i.check for i in tool.issues} == {"param_docs"}


def test_sdk_two_arg_tool_call_is_not_mistaken_for_mcp_ts_core(tmp_path):
    # The official SDK's `server.tool(name, callback)` also takes two args.
    write(tmp_path, "server.ts", """
        server.tool('ping', async () => ({ content: [{ type: 'text', text: 'pong' }] }));
        """)
    findings, _ = find_ts_tools(tmp_path)
    assert findings == []


def test_description_built_at_runtime_is_not_reported_missing(tmp_path):
    # cyanheads/pubmed-mcp-server: `description: buildFulltextDescription({...})`
    # can't be resolved statically, but the tool clearly has one.
    write(tmp_path, "fetch.tool.ts", """
        import { tool, z } from '@cyanheads/mcp-ts-core';

        export const fetchFulltext = tool('pubmed_fetch_fulltext', {
          description: buildFulltextDescription({ europePmc: true }),
          input: z.object({ pmid: z.string().describe('PubMed ID') }),
          async handler(input) { return {}; },
        });

        export const noDesc = tool('pubmed_no_desc', {
          description: '',
          input: z.object({}),
          async handler() { return {}; },
        });
        """)
    findings, _ = find_ts_tools(tmp_path)
    by_name = {f.name: f for f in findings}
    assert by_name["pubmed_fetch_fulltext"].issues == []
    # A description that really is empty is still flagged.
    assert {i.check for i in by_name["pubmed_no_desc"].issues} == {"description"}


def test_file_name_containing_test_is_not_skipped_as_a_test(tmp_path):
    # cyanheads/pentest-mcp-server names every tool file `pentest-*.tool.ts`;
    # a substring check read "pentest" as a test file and found 0 tools.
    tool_src = """
        import { tool, z } from '@cyanheads/mcp-ts-core';
        export const encode = tool('{name}', {
          description: 'Encode a payload with the chosen encoding.',
          input: z.object({ payload: z.string().describe('Text to encode') }),
          async handler(input) { return {}; },
        });
        """
    write(tmp_path, "src/tools/pentest-encode.tool.ts", tool_src.replace("{name}", "pentest_encode"))
    write(tmp_path, "src/tools/latest.ts", tool_src.replace("{name}", "latest_release"))
    # Real test files are still skipped.
    for name in ("encode.test.ts", "encode.spec.ts", "encode-test.ts"):
        write(tmp_path, f"src/tools/{name}", tool_src.replace("{name}", "from_" + name.split(".")[0]))
    # A camelCase Test/Spec suffix is a test file when it holds test-framework code.
    for name, marker in (("testUtils.ts", "beforeEach(() => {});"),
                         ("setupTests.ts", "import '@testing-library/jest-dom';"),
                         ("encodeSpec.ts", "describe('encode', () => {});")):
        write(tmp_path, f"src/tools/{name}", marker + tool_src.replace("{name}", "from_" + name.split(".")[0]))
    findings, _ = find_ts_tools(tmp_path)
    assert sorted(f.name for f in findings) == ["latest_release", "pentest_encode"]


def test_camel_case_test_suffix_without_test_code_is_source(tmp_path):
    # fr0ster/mcp-abap-adt: ABAP unit tests are the domain, so 17 tools live in
    # files like handlers/unit_test/high/handleCreateUnitTest.ts and were skipped.
    write(tmp_path, "src/groups.ts", """
        import { TOOL_DEFINITION as CreateUnitTest_Tool } from '../src/handlers/unit_test/handleCreateUnitTest.js';
        """)
    write(tmp_path, "src/handlers/unit_test/handleCreateUnitTest.ts", """
        export const TOOL_DEFINITION = {
          name: 'CreateUnitTest',
          description: 'Create an ABAP unit test class.',
          inputSchema: { type: 'object', properties: { class_name: { type: 'string', description: 'Class name.' } } },
        } as const;
        """)
    findings, _ = find_ts_tools(tmp_path)
    assert [f.name for f in findings] == ["CreateUnitTest"]


def test_exported_tool_shaped_const_counts_without_handler(tmp_path):
    # fr0ster/mcp-abap-adt: 300+ `export const TOOL_DEFINITION = {...} as const`,
    # imported under aliases and registered in a loop; 0 found before.
    write(tmp_path, "src/groups/index.ts", """
        import { TOOL_DEFINITION as GetClass_Tool } from '../handlers/handleGetClass';
        import {
          TOOL_DEFINITION as GetTable_Tool,
        } from '../handlers/handleGetTable.js';
        export const tools = [{ toolDefinition: GetClass_Tool }, { toolDefinition: GetTable_Tool }];
        """)
    # Exported but never imported by source code (only a test uses it): dead, not counted.
    write(tmp_path, "src/handlers/handleGetUnused.ts", """
        export const TOOL_DEFINITION = {
          name: 'GetUnused',
          description: 'A definition no group registers.',
          inputSchema: { type: 'object', properties: {} },
        } as const;
        """)
    write(tmp_path, "src/__tests__/unused.test.ts", """
        import { TOOL_DEFINITION } from '../handlers/handleGetUnused';
        """)
    write(tmp_path, "src/handlers/handleGetClass.ts", """
        export const TOOL_DEFINITION = {
          name: 'GetClass',
          description: 'Retrieve ABAP class source code.',
          inputSchema: {
            type: 'object',
            properties: { class_name: { type: 'string' } },
            required: ['class_name'],
          },
        } as const;
        export async function handleGetClass(context, args) { return {}; }
        """)
    write(tmp_path, "src/handlers/handleGetTable.ts", """
        export const TOOL_DEFINITION = {
          name: 'GetTable',
          description: 'Retrieve an ABAP table definition.',
          inputSchema: { type: 'object', properties: { table_name: { type: 'string', description: 'Table name.' } } },
        } satisfies ToolDefinition;
        """)
    # Not exported and no handler: could be anything (a fixture, a config), not counted.
    write(tmp_path, "src/handlers/local.ts", """
        const draft = {
          name: 'NotATool',
          description: 'Local object with the same keys.',
          inputSchema: { type: 'object', properties: {} },
        };
        """)
    findings, _ = find_ts_tools(tmp_path)
    by_name = {f.name: f for f in findings}
    assert sorted(by_name) == ["GetClass", "GetTable"]
    # Raw JSON Schema params are read: the undescribed one is flagged.
    assert {i.check for i in by_name["GetClass"].issues} == {"param_docs"}
    assert by_name["GetTable"].issues == []


def test_mcp_framework_tool_classes_are_recognized(tmp_path):
    # mcp-framework (~60 public servers) defines each tool as a class with no
    # registration call anywhere; all of them scanned as 0 tools. Both schema
    # forms: the older per-field `{ type, description }` and `z.object(...)`.
    write(tmp_path, "src/tools/BalanceTool.ts", """
        import { MCPTool } from "mcp-framework";
        import { z } from "zod";

        class BalanceTool extends MCPTool<BalanceInput> {
          name = "hledger_balance";
          description = "Get account balances using the hledger balance command";
          schema = {
            period: { type: z.string().optional(), description: "Time period to report on" },
            depth: { type: z.number().optional() },
          };
          async execute(input) { return run(input); }
        }
        export default BalanceTool;
        """)
    write(tmp_path, "src/tools/SearchTool.ts", """
        import { MCPTool } from "mcp-framework";
        import { z } from "zod";

        const schema = z.object({
          query: z.string().describe("Search text"),
          limit: z.number().optional(),
        });

        export default class SearchTool extends MCPTool {
          name = "search_docs";
          protected description: string = "Search the documentation for a phrase.";
          schema = schema;
          async execute(input) { return search(input.query); }
        }
        """)
    findings, _ = find_ts_tools(tmp_path)
    by_name = {f.name: f for f in findings}
    assert set(by_name) == {"hledger_balance", "search_docs"}
    balance = by_name["hledger_balance"]
    assert balance.param_count == 2
    assert [i.check for i in balance.issues] == ["param_docs"]  # depth has no description
    search = by_name["search_docs"]
    assert search.param_count == 2
    assert [i.check for i in search.issues] == ["param_docs"]  # limit has no .describe
    # The framework's toolCall wraps execute() in its own try/catch.
    assert all(i.check != "error_handling" for f in findings for i in f.issues)


def test_mcp_framework_tool_via_repo_local_base_class(tmp_path):
    # futuur/Futuur-MCP: 11 of 18 tools extend an abstract FutuurBaseTool,
    # which extends MCPTool; the base class itself is not a tool.
    write(tmp_path, "src/tools/FutuurBaseTool.ts", """
        import { MCPTool } from "mcp-framework";
        export abstract class FutuurBaseTool<I> extends MCPTool<I> {
          protected client = makeClient();
        }
        """)
    write(tmp_path, "src/tools/PlaceBetTool.ts", """
        import { FutuurBaseTool } from "./FutuurBaseTool";
        class PlaceBetTool extends FutuurBaseTool<PlaceBetInput> {
          name = "place_bet";
          description = "";
          schema = {};
          async execute(input) { return this.client.bet(input); }
        }
        """)
    findings, _ = find_ts_tools(tmp_path)
    assert [f.name for f in findings] == ["place_bet"]
    assert [i.check for i in findings[0].issues] == ["description"]


def test_unrelated_class_with_a_name_field_is_not_a_tool(tmp_path):
    write(tmp_path, "src/models.ts", """
        class Base {}
        class User extends Base {
          name = "alice";
          description = "A user of the app";
        }
        """)
    findings, _ = find_ts_tools(tmp_path)
    assert findings == []


def test_tool_object_with_run_handler_in_exported_const_is_recognized(tmp_path):
    # Verified against a real miss: hustcc/mcp-echarts, 0 of 18 tools found.
    # Each tool is `export const x = { name, description, inputSchema, run }`,
    # registered by a loop: `server.tool(name, description, inputSchema.shape, run)`.
    write(tmp_path, "src/tools/bar.ts", """
        import { z } from 'zod'
        export const generateBarChartTool = {
          name: 'generate_bar_chart',
          description: 'Generate a bar chart.',
          inputSchema: z.object({ title: z.string().describe('Chart title') }),
          run: async (params) => render(params),
        }
        """)
    write(tmp_path, "src/index.ts", """
        import { tools } from './tools'
        for (const tool of tools) {
          const { name, description, inputSchema, run } = tool
          server.tool(name, description, inputSchema.shape, run)
        }
        """)
    findings, _ = find_ts_tools(tmp_path)
    assert [f.name for f in findings] == ["generate_bar_chart"]


def test_tool_object_without_any_handler_key_is_not_counted_on_its_own(tmp_path):
    # name + description + inputSchema alone is a JSON tool listing shape, used
    # in docs and tests too; it only counts via a ListTools handler.
    write(tmp_path, "src/fixtures.ts", """
        export const sample = {
          name: 'not_a_tool',
          description: 'A fixture.',
          inputSchema: { type: 'object' },
        }
        """)
    findings, _ = find_ts_tools(tmp_path)
    assert findings == []


def test_list_tools_handler_returning_a_const_tool_object_is_recognized(tmp_path):
    # Verified against a real miss: hustcc/mcp-mermaid, 0 of 1 tools found.
    # The ListTools handler returns `{ tools: [tool] }` with the object in a
    # const in another file.
    write(tmp_path, "src/tools/index.ts", """
        export const tool = {
          name: 'generate_mermaid_diagram',
          description: 'Generate a mermaid diagram.',
          inputSchema: { type: 'object', properties: {} },
        }
        """)
    write(tmp_path, "src/server.ts", """
        import { ListToolsRequestSchema } from '@modelcontextprotocol/sdk/types.js'
        import { tool } from './tools'
        server.server.setRequestHandler(ListToolsRequestSchema, async () => ({
          tools: [tool],
        }))
        """)
    findings, _ = find_ts_tools(tmp_path)
    assert [f.name for f in findings] == ["generate_mermaid_diagram"]


def test_tools_registered_through_a_local_wrapper_function_are_recognized(tmp_path):
    # Verified against a real miss: strausmann/mcp-dockhand, 0 of 357 tools.
    # A local `registerTool(server, name, schema, callback)` forwards to
    # `server.tool(name, describeTool(name), schema, ...)` inside its own
    # try/catch, and every tool is a call to it.
    write(tmp_path, "src/utils/tool-helper.ts", """
        export function registerTool(server, name, schema, callback) {
          const description = describeTool(name)
          ;(server as any).tool(name, description, schema, async (args) => {
            try {
              return await callback(args)
            } catch (error) {
              return errorResponse(error)
            }
          })
        }
        """)
    write(tmp_path, "src/tools/backups.ts", """
        import { z } from 'zod'
        import { registerTool } from '../utils/tool-helper.js'
        export function registerBackupTools(server, client) {
          registerTool(server, 'list_backups', {}, async () => client.get('/api/backups'))
          registerTool(server, 'get_backup',
            { id: z.string().describe('Backup ID') },
            async ({ id }) => client.get(`/api/backups/${id}`))
          registerTool(server, 'delete_backup',
            { id: z.string() },
            async ({ id }) => client.delete(`/api/backups/${id}`))
        }
        """)
    findings, _ = find_ts_tools(tmp_path)
    by_name = {f.name: f for f in findings}
    assert sorted(by_name) == ["delete_backup", "get_backup", "list_backups"]
    # Description is built at runtime (unknown, not missing); the wrapper's
    # try/catch covers every tool; the undocumented schema field is still flagged.
    assert all(f.has_description and f.has_try_except for f in findings)
    assert {i.check for i in by_name["get_backup"].issues} == set()
    assert any("describe" in i.message for i in by_name["delete_backup"].issues)


def test_wrapper_forwarding_a_description_argument_checks_it(tmp_path):
    write(tmp_path, "src/index.ts", """
        const addTool = (server, name, description, shape, handler) => {
          server.tool(name, description, shape, handler)
        }
        addTool(server, 'ping', '', {}, async () => ok())
        """)
    findings, _ = find_ts_tools(tmp_path)
    assert [f.name for f in findings] == ["ping"]
    assert not findings[0].has_description  # literal empty string: really missing
    assert any(i.check == "error_handling" for i in findings[0].issues)


def test_function_registering_a_literal_name_is_not_a_wrapper(tmp_path):
    write(tmp_path, "src/index.ts", """
        export function setup(server, name) {
          server.tool('fixed_tool', 'A fixed tool for the test.', {}, async () => ok())
        }
        setup(server, 'not_a_tool')
        """)
    findings, _ = find_ts_tools(tmp_path)
    assert [f.name for f in findings] == ["fixed_tool"]
