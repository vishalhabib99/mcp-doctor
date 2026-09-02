from pathlib import Path
from textwrap import dedent

from mcp_doctor.analyzer import analyze_repo
from mcp_doctor.ts_analyzer import TS_AVAILABLE, find_ts_tools

import pytest

pytestmark = pytest.mark.skipif(not TS_AVAILABLE, reason="tree_sitter not installed")


def write(tmp_path: Path, name: str, content: str) -> Path:
    p = tmp_path / name
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
