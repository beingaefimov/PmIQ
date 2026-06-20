""" Mock-сервер, читающий .mcp.json плагина и отдающий данные из CSV-файлов
из skills/*/. Возвращает JSON с Markdown-таблицей для LLM + _raw_data """

from __future__ import annotations

import asyncio
import argparse
import csv
import json
import sys
from pathlib import Path
from typing import List, Dict, Any

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

def get_plugin_dir(plugin_name: str) -> Path:
    current_dir = Path(__file__).resolve().parent
    return current_dir.parent / "plugins" / plugin_name

def load_csv_as_dicts(csv_path: Path) -> List[Dict[str, str]]:
    if not csv_path.exists():
        return []
    with open(csv_path, "r", encoding="utf-8") as f:
        return list(csv.DictReader(f))

def filter_data(
    data: List[Dict[str, str]],
    arguments: Dict[str, Any],
    tool_name: str = "") -> tuple[List[Dict[str, str]], List[str]]:
    
    if not data:
        return data, []
    warnings: List[str] = []
    csv_keys = list(data[0].keys())
    # Специальная логика для нечеткого поиска проектов
    if tool_name == "identify_project" and "query" in arguments:
        search_term = str(arguments["query"]).strip().lower()
        if not search_term:
            return data, []
        filtered = []
        for row in data:
            # Ищем вшивание по именам, алиасам и описанию
            search_space = " ".join([
                row.get("project_name", ""),
                row.get("aliases", ""),
                row.get("description", "")]).lower()
            # Разбиваем алиасы по точке с запятой для точного совпадения отдельных слов
            aliases = [a.strip().lower() for a in row.get("aliases", "").split(";") if a.strip()]
            # Если есть точное совпадение алиаса или подстрока в общем пуле
            if search_term in search_space or search_term in aliases:
                filtered.append(row)
        return filtered, warnings
    # Стандартная логика точного фильтра для остальных инструментов
    for key in arguments:
        matched = next((k for k in csv_keys if k.lower() == key.lower()), None)
        if not matched:
            warnings.append(f"Filter key '{key}' not found in CSV columns {csv_keys}. Filter ignored.")
    filtered = []
    for row in data:
        match = True
        for key, value in arguments.items():
            csv_key = next((k for k in row.keys() if k.lower() == key.lower()), None)
            if csv_key:
                if str(row[csv_key]).strip().lower() != str(value).strip().lower():
                    match = False
                    break
        if match:
            filtered.append(row)
    return filtered, warnings

def convert_to_markdown_table(data: List[Dict[str, str]]) -> str:
    if not data:
        return "(нет данных)"
    headers = list(data[0].keys())
    header_row = "| " + " | ".join(headers) + " |"
    separator  = "|" + "|".join(["---"] * len(headers)) + "|"
    rows = ["| " + " | ".join(str(row.get(h, "") if row.get(h, "") is not None else "").replace("|", " ") for h in headers) + " |"
            for row in data]
    return "\n".join([header_row, separator] + rows)

def main():
    parser = argparse.ArgumentParser(description="Intent-Based Mock MCP Server")
    parser.add_argument("--plugin", required=True, help="Plugin name to serve")
    args = parser.parse_args()
    plugin_dir = get_plugin_dir(args.plugin)
    if not plugin_dir.exists():
        print(f"Error: Plugin directory not found: {plugin_dir}", file=sys.stderr)
        sys.exit(1)
    app = Server(f"pm-iq-mock-{args.plugin}")

    mcp_config_path = plugin_dir / ".mcp.json"
    with open(mcp_config_path, "r", encoding="utf-8") as f:
        mcp_config = json.load(f)
    tools_list: List[Tool] = []
    for server_cfg in mcp_config.get("mcpServers", {}).values():
        for tool_def in server_cfg.get("tools", []):
            if isinstance(tool_def, str):
                tools_list.append(Tool(
                    name=tool_def,
                    description=f"Dynamic tool for {tool_def} (CSV-backed)",
                    inputSchema={"type": "object", "properties": {}, "required": []}))
            else:
                tools_list.append(Tool(**tool_def))

    tool_to_csv: Dict[str, Path] = {}
    skills_dir = plugin_dir / "skills"
    if skills_dir.exists():
        for skill_dir in skills_dir.iterdir():
            if skill_dir.is_dir():
                for csv_file in skill_dir.glob("*.csv"):
                    if any(t.name == csv_file.stem for t in tools_list):
                        tool_to_csv[csv_file.stem] = csv_file

    @app.list_tools()
    async def list_tools() -> List[Tool]:
        return tools_list

    @app.call_tool()
    async def call_tool(name: str, arguments: dict):
        print(f"\n[DEBUG] Tool called: {name} with args: {arguments}",
              file=sys.stderr, flush=True)
        try:
            csv_path = tool_to_csv.get(name)
            if not csv_path:
                error = {"error": f"No mock data file for tool '{name}'"}
                return [TextContent(type="text", text=json.dumps(error, ensure_ascii=False))]
            raw_data = load_csv_as_dicts(csv_path)
            filtered_data, filter_warnings = filter_data(raw_data, arguments, tool_name=name)
            for w in filter_warnings:
                print(f"[WARNING] filter_data: {w}", file=sys.stderr, flush=True)
            response: Dict[str, Any] = {
                "data": convert_to_markdown_table(filtered_data),
                "filter_warnings": filter_warnings,
                "_raw_data": filtered_data,
            }
            print(f"[DEBUG] Response: {len(filtered_data)} rows (raw data mode)",
                  file=sys.stderr, flush=True)
            return [TextContent(
                type="text",
                text=json.dumps(response, ensure_ascii=False, indent=2))]
        except Exception as e:
            import traceback
            traceback.print_exc(file=sys.stderr)
            return [TextContent(
                type="text",
                text=json.dumps({"error": str(e)}, ensure_ascii=False))]

    async def run():
        async with stdio_server() as (read_stream, write_stream):
            await app.run(read_stream, write_stream, app.create_initialization_options())

    asyncio.run(run())

if __name__ == "__main__":
    main()