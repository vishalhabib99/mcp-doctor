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


def test_hand_rolled_jsonrpc_switch_tools_list(tmp_path):
    # kitfunso/hippo-memory: no SDK at all, a JSON-RPC `switch (method)` whose
    # `case 'tools/list'` returns an imported static array nested under
    # `result`. Was 0 tools; the `initialize` case's `tools: {}` capability
    # object must not be mistaken for a tool list.
    write(tmp_path, "src/mcp/tools.ts", """
        export const TOOLS: readonly McpToolDefinition[] = [
          { name: "hippo_recall", description: "Retrieve relevant memories from the store", inputSchema: { type: "object", properties: { query: { type: "string", description: "What to search for" } } } },
          { name: "hippo_status", description: "Show memory store health and counts", inputSchema: { type: "object", properties: {} } },
        ];
        """)
    write(tmp_path, "src/mcp/request.ts", """
        import { TOOLS } from "./tools.js";
        export function handle(method: string, id: number) {
          switch (method) {
            case 'initialize':
              return { jsonrpc: '2.0', id, result: { capabilities: { tools: {} } } };
            case 'tools/list':
              return { jsonrpc: '2.0', id, result: { tools: TOOLS } };
          }
        }
        """)
    findings, _ = find_ts_tools(tmp_path)
    assert sorted(f.name for f in findings) == ["hippo_recall", "hippo_status"]
    assert all(f.file == "src/mcp/tools.ts" for f in findings)


def test_hand_rolled_jsonrpc_if_tools_list_and_runtime_proxy_skipped(tmp_path):
    # `if (method === "tools/list")` form resolves the same way; a proxy that
    # returns another server's list at runtime has nothing static to read and
    # must report nothing rather than guess.
    write(tmp_path, "server.ts", """
        const TOOLS = [
          { name: "get_quote", description: "Get the latest quote for a ticker", inputSchema: { type: "object", properties: {} } },
        ];
        export async function onMessage(msg) {
          if (msg.method === "tools/list") {
            return { result: { tools: TOOLS } };
          }
        }
        """)
    write(tmp_path, "proxy.ts", """
        export async function forward(msg, upstream) {
          switch (msg.method) {
            case "tools/list":
              return { result: { tools: await upstream.listTools() } };
          }
        }
        """)
    findings, _ = find_ts_tools(tmp_path)
    assert [f.name for f in findings] == ["get_quote"]


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
    # `tools` stays unresolved. Since bug #84 the definition object itself is
    # found (the repo has a ListTools handler), so the one real tool is
    # reported once, from its own literal, not from the wrong `tools`.
    assert [(f.name, f.line) for f in findings] == [("a_tool", 8)]


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


def test_class_method_wrapper_with_bound_alias_and_meta_fields(tmp_path):
    # cmer81/open-meteo-mcp, 0 of 17: tools are registered through a private
    # method that saves `server.registerTool.bind(server)` under another name
    # and reads name/description from a metadata const passed in.
    write(tmp_path, "src/tools.ts", """
        export const FORECAST_TOOL: ToolDefinition = {
          name: 'weather_forecast',
          title: 'Weather Forecast',
          description: 'Get a weather forecast for coordinates.',
        };
        export const ELEVATION_TOOL: ToolDefinition = {
          name: 'elevation',
          title: 'Elevation',
          description: 'Get terrain elevation for coordinates.',
        };
        """)
    write(tmp_path, "src/index.ts", """
        import { z } from 'zod';
        import { FORECAST_TOOL, ELEVATION_TOOL } from './tools.js';
        const ForecastSchema = z.object({ latitude: z.number().describe('Latitude.') });
        const ElevationSchema = z.object({ latitude: z.number() });
        class Server {
          private registerReadOnlyTool(server: McpServer, meta: ToolDefinition, schema, handler): void {
            const registerToolUntyped = server.registerTool.bind(server) as (n: string, c: unknown, cb: unknown) => void;
            registerToolUntyped(meta.name, { title: meta.title, description: meta.description, inputSchema: schema },
              async (params) => { try { return await handler(params); } catch (e) { return { isError: true }; } });
          }
          private setup(server: McpServer) {
            this.registerReadOnlyTool(server, FORECAST_TOOL, ForecastSchema, (p) => this.client.forecast(p));
            this.registerReadOnlyTool(server, ELEVATION_TOOL, ElevationSchema, (p) => this.client.elevation(p));
            // Not a wrapper: same-named method on another object is ignored.
            other.registerReadOnlyTool(server, { name: 'not_a_tool', description: 'x' }, ForecastSchema, () => {});
          }
        }
        """)
    findings, _ = find_ts_tools(tmp_path)
    by_name = {f.name: f for f in findings}
    assert sorted(by_name) == ["elevation", "weather_forecast"]
    assert by_name["weather_forecast"].issues == []
    assert {i.check for i in by_name["elevation"].issues} == {"param_docs"}
    assert by_name["weather_forecast"].has_try_except


def test_method_not_forwarding_a_param_as_name_is_not_a_wrapper(tmp_path):
    write(tmp_path, "src/index.ts", """
        class Server {
          private setupFixed(server: McpServer, extra) {
            const reg = server.registerTool.bind(server);
            reg('status', { description: 'Report server status.', inputSchema: {} }, async () => ({}));
          }
          private run() { this.setupFixed(server, 'ignored'); }
        }
        """)
    findings, _ = find_ts_tools(tmp_path)
    assert "ignored" not in [f.name for f in findings]


def test_list_tools_handler_mapping_a_registry_through_a_helper(tmp_path):
    # chrisryugj/korean-law-mcp reported 0 of 99: the handler calls a local
    # `listTools()`, which returns `{ tools: exposed.map(t => ({ name: t.name, ... })) }`
    # over a filtered array of `{ name, description, schema: z.object(...), handler }`.
    write(tmp_path, "tools/search.ts", """
        import { z } from "zod";
        export const SearchLawSchema = z.object({ query: z.string().describe("Law name keyword") });
        """)
    write(tmp_path, "tool-registry.ts", """
        import { ListToolsRequestSchema } from "@modelcontextprotocol/sdk/types.js";
        import { z } from "zod";
        import { SearchLawSchema } from "./tools/search.js";

        export const allTools = [
          { name: "search_law", description: "Search laws by keyword.", schema: SearchLawSchema, handler: searchLaw },
          { name: "get_law_text", description: "Get a law's full text.", schema: z.object({ mst: z.string() }), handler: getLawText },
        ];
        const exposedTools = allTools.filter(t => EXPOSED.has(t.name));

        function listTools() {
          return { tools: exposedTools.map(tool => ({ name: tool.name, description: tool.description, inputSchema: toJson(tool.schema) })) };
        }

        export function registerTools(server) {
          server.setRequestHandler(ListToolsRequestSchema, async () => listTools());
        }
        """)
    findings, _ = find_ts_tools(tmp_path)
    by_name = {f.name: f for f in findings}
    assert set(by_name) == {"search_law", "get_law_text"}
    assert by_name["search_law"].issues == []
    assert any("no .describe" in i.message for i in by_name["get_law_text"].issues)


def test_list_tools_map_that_does_not_project_tools_is_not_read_as_a_registry(tmp_path):
    # A `.map` whose callback doesn't build a `{ name, ... }` object isn't a
    # one-tool-per-element projection, so the array it maps over isn't tools.
    write(tmp_path, "server.ts", """
        import { ListToolsRequestSchema } from "@modelcontextprotocol/sdk/types.js";
        const loaders = [{ name: "not_a_tool", description: "A loader config." }];
        server.setRequestHandler(ListToolsRequestSchema, async () => ({ tools: loaders.map(l => l.load()) }));
        """)
    findings, _ = find_ts_tools(tmp_path)
    assert findings == []


def test_mjs_and_cjs_servers_are_scanned(tmp_path):
    # Bug #83, from xixihhhh/clipforge (0 of 30 found): the whole server is
    # one .mjs file, which the file filter skipped. Test and fixture files stay out.
    write(tmp_path, "mcp/server.mjs", """
        server.registerTool("make_clip", { description: "Render a short clip from a prompt.", inputSchema: {} }, async () => {
          return { content: [] };
        });
        """)
    write(tmp_path, "legacy/server.cjs", """
        server.registerTool("list_clips", { description: "List rendered clips, newest first.", inputSchema: {} }, async () => {
          return { content: [] };
        });
        """)
    write(tmp_path, "src/__fixtures__/fake-server.mjs", """
        server.registerTool("echo", { description: "Echo for the client tests.", inputSchema: {} }, async () => {
          return { content: [] };
        });
        """)
    write(tmp_path, "mcp/server.test.mjs", """
        server.registerTool("fixture_tool", { description: "Only for tests.", inputSchema: {} }, async () => {
          return { content: [] };
        });
        """)
    findings, _ = find_ts_tools(tmp_path)
    assert sorted(f.name for f in findings) == ["list_clips", "make_clip"]


def test_definition_objects_registered_in_a_loop_need_no_handler(tmp_path):
    # Bug #84, from the 2026-10 census (tableau-mcp, agenticmail, anki-mcp-server,
    # circleci and others, 0 found before): tools registered only through
    # runtime values, with definition objects that use `schema`/`paramsSchema`,
    # an `as const` name, no handler key, or sit in `new SomeTool({...})`.
    write(tmp_path, "src/tools.ts", """
        export const TOOLS = [
          { name: "send_email" as const, description: "Send an email to one or more people.", schema: { to: z.string() } },
          { name: "list_inbox", description: "List the newest messages in the inbox.", parameters: { type: "object", properties: {} } },
        ];
        export const listViews = new WebTool({
          name: "list-views",
          description: "List the views on a Tableau site.",
          paramsSchema: { filter: z.string().optional() },
          callback: async () => ({}),
        });
        """)
    write(tmp_path, "src/server.ts", """
        import { TOOLS } from "./tools.js";
        for (const tool of TOOLS) {
          server.registerTool(tool.name, { description: tool.description, inputSchema: tool.schema }, run);
        }
        """)
    findings, _ = find_ts_tools(tmp_path)
    assert sorted(f.name for f in findings) == ["list-views", "list_inbox", "send_email"]


def test_loop_registration_skips_resources_and_api_parameters(tmp_path):
    # Same-shaped objects that aren't tools: a resource definition
    # (git-mcp-server), zodios query parameters (tableau-mcp), an OpenAPI
    # parameter (daiso-mcp), and anything in a `parameters:` array.
    write(tmp_path, "src/defs.ts", """
        export const workingDir = {
          name: "git-working-directory", description: "The session's working directory.",
          uriTemplate: "git://working-directory", paramsSchema: Params,
        };
        export const paging = [
          { name: "pageSize", type: "Query", schema: z.number(), description: "Page size." },
        ];
        export const specParams = [
          { name: "q", in: "query", schema: { type: "string" }, description: "Search text." },
        ];
        export const endpoint = {
          path: "/views",
          parameters: [{ name: "filter", schema: z.string(), description: "Filter expression." }],
        };
        """)
    # Benchmarks, evals and Storybook stories restate tool lists without
    # serving them (DollhouseMCP, help-scout-mcp-server, director).
    write(tmp_path, "scripts/benchmark-tokens.ts", """
        const TOOLS = [{ name: "browse", description: "Browse the collection.", inputSchema: {} }];
        """)
    write(tmp_path, "src/components/tool-sheet.stories.tsx", """
        export const storyTools = [{ name: "fetch", description: "Fetch a URL.", schema: {} }];
        """)
    write(tmp_path, "src/server.ts", """
        server.setRequestHandler(ListToolsRequestSchema, async () => ({ tools }));
        """)
    findings, _ = find_ts_tools(tmp_path)
    assert findings == []


def test_handlerless_definition_needs_dynamic_registration(tmp_path):
    # Without a loop or ListTools handler, the old strict shape applies: a
    # `{ name, description, schema }` object alone could be a docs fixture
    # or an OpenAI function definition.
    write(tmp_path, "src/openai.ts", """
        export const functions = [
          { name: "get_weather", description: "Get the weather for a city.", schema: { city: z.string() } },
        ];
        """)
    write(tmp_path, "src/server.ts", """
        server.registerTool("ping", { description: "Check the server is up.", inputSchema: {} }, async () => ({ content: [] }));
        """)
    findings, _ = find_ts_tools(tmp_path)
    assert [f.name for f in findings] == ["ping"]


def test_loop_registration_keeps_out_template_names_and_unimported_exports(tmp_path):
    # Bug #85, from fr0ster/mcp-abap-adt: v1.15.2 reported `GetVersions` for
    # `name: \`Get${row.display}Versions\`` (one tool per object type, built at
    # runtime) and counted exported TOOL_DEFINITIONs that nothing imports.
    write(tmp_path, "src/versions.ts", """
        export const versionTools = ROWS.map((row) => ({
          name: `Get${row.display}Versions`,
          description: "List the versions of an object.",
          inputSchema: { type: "object", properties: {} },
        }));
        export const extra = [
          { name: `GetVersionSource`, description: "Read one version's source.", inputSchema: { type: "object", properties: {} } },
        ];
        """)
    write(tmp_path, "src/handlers/handleGetUnitTest.ts", """
        export const TOOL_DEFINITION = {
          name: "GetUnitTest",
          description: "Read a unit test run.",
          inputSchema: { type: "object", properties: {} },
        };
        """)
    write(tmp_path, "src/server.ts", """
        for (const tool of tools) server.registerTool(tool.name, { description: tool.description }, run);
        """)
    findings, _ = find_ts_tools(tmp_path)
    assert [f.name for f in findings] == ["GetVersionSource"]


def test_loop_registration_resolves_constant_prefixes_and_used_exports(tmp_path):
    # Bug #85, the shapes that stay: a `${TOOL_PREFIX}init` name whose prefix
    # is a constant (CSCSoftware/AiDex; v1.15.2 reported plain `init`), an
    # exported definition its own file returns (mcp-atom-of-thoughts), and one
    # another file loads with a dynamic import() (mcp-klever-vm).
    write(tmp_path, "src/constants.ts", """
        export const TOOL_PREFIX = 'aidex_';
        """)
    write(tmp_path, "src/tools.ts", """
        import { TOOL_PREFIX } from './constants.js';
        export function registerTools() {
          return [
            { name: `${TOOL_PREFIX}init`, description: "Index a project for search.", inputSchema: { type: "object", properties: {} } },
          ];
        }
        export const AOT_FAST_TOOL = { name: "AoT-fast", description: "Fast atom-of-thoughts pass.", inputSchema: { type: "object", properties: {} } };
        export function getTools() { return [AOT_FAST_TOOL]; }
        """)
    write(tmp_path, "src/utils/project-init.ts", """
        export const projectInitToolDefinition = {
          name: 'init_project', description: "Create a new project from the template.", inputSchema: { type: "object", properties: {} },
        };
        """)
    write(tmp_path, "src/server.ts", """
        const { projectInitToolDefinition } = await import('./utils/project-init.js');
        server.setRequestHandler(ListToolsRequestSchema, async () => ({ tools: [...registerTools(), projectInitToolDefinition] }));
        """)
    findings, _ = find_ts_tools(tmp_path)
    assert sorted(f.name for f in findings) == ["AoT-fast", "aidex_init", "init_project"]


def test_module_per_tool_loaded_at_runtime(tmp_path):
    # Bug #87, from postmanlabs/postman-mcp-server (0 of 213 found): every
    # src/tools/*.ts exports `method`, `description`, `parameters`; index.ts
    # loads them with readdir + import() and registers
    # `server.registerTool(tool.method, { description: tool.description,
    # inputSchema: tool.parameters.shape }, ...)`. A top-level file that only
    # re-exports a sub-folder's module counts; the sub-folder's modules that
    # are imported by name (sub-tools, helpers) don't.
    write(tmp_path, "src/tools/createCollection.ts", """
        export const method = 'createCollection';
        export const description = 'Create a collection in a workspace.';
        export const parameters = z.object({ workspace: z.string().describe('Workspace id.') });
        export async function handler() {}
        """)
    write(tmp_path, "src/tools/getCollection.ts", """
        export { method, description, parameters, handler } from './getCollection/index.js';
        """)
    write(tmp_path, "src/tools/getCollection/index.ts", """
        import { handler as mapHandler } from './getCollectionMap.js';
        export const method = 'getCollection';
        export const description = 'Get a collection, or its map.';
        export const parameters = z.object({ id: z.string() });
        """)
    write(tmp_path, "src/tools/getCollection/getCollectionMap.ts", """
        export const method = 'getCollectionMap';
        export const description = 'Sub-tool: the map variant.';
        export const parameters = z.object({});
        export async function handler() {}
        """)
    write(tmp_path, "src/index.ts", """
        const tools = await loadAllTools();
        for (const tool of tools) {
          server.registerTool(tool.method, { description: tool.description, inputSchema: tool.parameters.shape }, run);
        }
        """)
    findings, _ = find_ts_tools(tmp_path)
    assert sorted((f.name, f.file) for f in findings) == [
        ("createCollection", "src/tools/createCollection.ts"),
        ("getCollection", "src/tools/getCollection.ts"),
    ]
    create = next(f for f in findings if f.name == "createCollection")
    assert create.param_count == 1 and create.has_docstring_params


def test_module_per_tool_with_nested_metadata_and_namespace_imports(tmp_path):
    # vercel/next-devtools-mcp: `import * as browserEval from "./tools/browser-eval.js"`
    # and a ListTools handler mapping `tool.metadata.name` / `.description`.
    write(tmp_path, "src/tools/browser-eval.ts", """
        export const inputSchema = { action: z.string().describe('What to do.') };
        export const metadata = { name: 'browser_eval', description: 'Drive a browser for this project.' };
        """)
    write(tmp_path, "src/index.ts", """
        import * as browserEval from "./tools/browser-eval.js";
        const tools = [browserEval];
        server.setRequestHandler(ListToolsRequestSchema, async () => ({
          tools: tools.map((tool) => ({
            name: tool.metadata.name,
            description: tool.metadata.description,
            inputSchema: toolInputSchema(tool.inputSchema),
          })),
        }));
        """)
    findings, _ = find_ts_tools(tmp_path)
    assert [f.name for f in findings] == ["browser_eval"]


def test_module_per_tool_needs_a_runtime_registration(tmp_path):
    # Without a registration reading `tool.method`, modules that happen to
    # export `method` and `description` are not tools.
    write(tmp_path, "src/http/routes.ts", """
        export const method = 'GET';
        export const description = 'List users.';
        """)
    write(tmp_path, "src/server.ts", """
        server.registerTool("ping", { description: "Check the server is up.", inputSchema: {} }, async () => ({ content: [] }));
        """)
    findings, _ = find_ts_tools(tmp_path)
    assert [f.name for f in findings] == ["ping"]


def test_module_per_tool_ignores_namespace_roots_and_function_locals(tmp_path):
    # Found by the 501-repo before/after for bug #87: Adyen/adyen-mcp's
    # `{ name: constants.LIST_TERMINALS_NAME, description: constants.X }` reads
    # a module, not a runtime tool object; 1mcp-app/agent's exported function
    # holds a local `const name = arg.name || 'argument'`.
    write(tmp_path, "src/constants.ts", """
        export const LIST_TERMINALS_NAME = 'list_terminals';
        export const LIST_TERMINALS_DESCRIPTION = 'List the payment terminals.';
        """)
    write(tmp_path, "src/tools.ts", """
        import * as constants from './constants.js';
        export const listTerminals = { name: constants.LIST_TERMINALS_NAME, description: constants.LIST_TERMINALS_DESCRIPTION };
        """)
    write(tmp_path, "src/prompt.ts", """
        export function ask(arg) {
          const name = arg.name || 'argument';
          const description = 'Prompt text.';
          return { name, description };
        }
        """)
    write(tmp_path, "src/server.ts", """
        for (const tool of tools) {
          server.registerTool(tool.name, { description: tool.description }, run);
        }
        """)
    findings, _ = find_ts_tools(tmp_path)
    assert [f.name for f in findings] == []


def test_enum_member_tool_names_and_router_schema_objects(tmp_path):
    # Bug #89, two 0-tool repos from the 2026-10 census: etsd-tech/mcp-pointer
    # names its tool with a string enum member (`MCPToolName.GET_POINTED_ELEMENT`),
    # and alioshr/memory-bank-mcp registers `router.setTool({ schema: { name,
    # description, inputSchema }, handler })`, listed by a ListTools handler.
    write(tmp_path, "src/mcp-service.ts", """
        enum MCPToolName {
          GET_POINTED_ELEMENT = 'get-pointed-element',
        }
        class Service {
          start() { this.server.setRequestHandler(ListToolsRequestSchema, this.handleListTools.bind(this)); }
          private async handleListTools() {
            return { tools: [
              { name: MCPToolName.GET_POINTED_ELEMENT, description: 'Get the element the user pointed at.',
                inputSchema: { type: 'object', properties: {} } },
            ] };
          }
        }
        """)
    write(tmp_path, "src/routes.ts", """
        router.setTool({
          schema: { name: "list_projects", description: "List all projects in the memory bank.",
                    inputSchema: { type: "object", properties: {} } },
          handler: listProjects,
        });
        router.setTool({
          options: { name: "not_a_tool", description: "Router options.", inputSchema: {} },
        });
        """)
    findings, _ = find_ts_tools(tmp_path)
    assert sorted(f.name for f in findings) == ["get-pointed-element", "list_projects"]


def test_same_const_name_in_many_files_resolves_per_file(tmp_path):
    # Bug #90, from cablate/mcp-google-map (1 of 18 found): every tool file
    # declares `const NAME = "maps_..."` and exports `{ NAME, DESCRIPTION, SCHEMA }`;
    # config.ts lists `{ name: AirQuality.NAME, ... }`. The repo-wide first
    # `NAME` won, so all 18 resolved to one name and de-duplicated to 1.
    for cls, tool in (("AirQuality", "maps_air_quality"), ("Geocode", "maps_geocode")):
        write(tmp_path, f"src/tools/{cls}.ts", f"""
            const NAME = "{tool}";
            const DESCRIPTION = "Description of {tool}.";
            const SCHEMA = {{ q: z.string().describe("Query.") }};
            export const {cls} = {{ NAME, DESCRIPTION, SCHEMA }};
            """)
    write(tmp_path, "src/config.ts", """
        import { AirQuality } from "./tools/AirQuality.js";
        import { Geocode } from "./tools/Geocode.js";
        export const tools = [
          { name: AirQuality.NAME, description: AirQuality.DESCRIPTION, schema: AirQuality.SCHEMA },
          { name: Geocode.NAME, description: Geocode.DESCRIPTION, schema: Geocode.SCHEMA },
        ];
        """)
    write(tmp_path, "src/server.ts", """
        tools.forEach((tool) => server.registerTool(tool.name, { description: tool.description, inputSchema: z.object(tool.schema) }, run));
        """)
    findings, _ = find_ts_tools(tmp_path)
    assert sorted(f.name for f in findings) == ["maps_air_quality", "maps_geocode"]


def test_deeply_nested_file_does_not_crash(tmp_path):
    # Bug #92: the tree walk was recursive, so a deeply nested file (generated
    # data, opentabs-dev/opentabs) overflowed the recursion limit.
    nested = "[" * 3000 + "]" * 3000
    write(tmp_path, "server.ts", f"""
        const data = {nested};
        server.tool("ping", "Checks the server is up", {{}}, async () => ({{ content: [] }}));
        """)
    findings, _ = find_ts_tools(tmp_path)
    assert [t.name for t in findings] == ["ping"]


def test_very_long_string_concat_chain_does_not_crash(tmp_path):
    chain = " + ".join(['"x"'] * 5000)
    write(tmp_path, "server.ts", f"""
        server.tool("big", {chain}, {{}}, async () => ({{ content: [] }}));
        """)
    findings, _ = find_ts_tools(tmp_path)
    assert [t.name for t in findings] == ["big"]
    assert "description" not in {i.check for i in findings[0].issues}


def test_loop_registered_definitions_use_the_key_the_loop_passes(tmp_path):
    # Bug #93: PaddleHQ/paddle-mcp-server registers `this.tool(tool.method, ...)`
    # and keeps a human title in `name`. The title was reported as the tool name,
    # so 90 valid names became "invalid".
    write(tmp_path, "tools.ts", """
        export const tools = [
          {
            method: "activate_subscription",
            name: "Activate a trialing subscription",
            description: "Activates a trialing subscription now.",
            parameters: z.object({ id: z.string().describe("Subscription ID") }),
          },
        ];
        """)
    write(tmp_path, "toolkit.ts", """
        import { tools } from "./tools";
        export class Toolkit {
          register(server) {
            tools.forEach((tool) => {
              server.tool(tool.method, tool.description, tool.parameters.shape, async (arg) => run(tool.method, arg));
            });
          }
        }
        """)
    result = analyze_repo(tmp_path)
    assert [t.name for t in result.tools] == ["activate_subscription"]
    assert not [i for i in result.repo_issues if i.check == "tool_name"]


def test_loop_registered_definitions_still_use_name_by_default(tmp_path):
    write(tmp_path, "tools.ts", """
        export const tools = [
          {
            method: "unused_method_key",
            name: "get_thing",
            description: "Gets a thing by ID.",
            inputSchema: { type: "object", properties: { id: { type: "string", description: "Thing ID" } } },
            handler: async () => ({ content: [] }),
          },
        ];
        """)
    write(tmp_path, "server.ts", """
        import { tools } from "./tools";
        for (const tool of tools) server.registerTool(tool.name, { description: tool.description }, tool.handler);
        """)
    result = analyze_repo(tmp_path)
    assert [t.name for t in result.tools] == ["get_thing"]
