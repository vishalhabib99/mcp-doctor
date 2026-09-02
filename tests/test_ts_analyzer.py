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
