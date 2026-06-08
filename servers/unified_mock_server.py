""" Dynamic CSV-based Mock MCP Server for PM IQ.
Сканирует папку плагина, находит CSV-файлы (мок-данные) в папках скиллов и отдает эти данные как MCP-сервер """

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
    """ Возвращает путь к директории плагина относительно расположения этого скрипта """
    current_dir = Path(__file__).resolve().parent
    return current_dir.parent / "plugins" / plugin_name

def load_csv_as_dicts(csv_path: Path) -> List[Dict[str, str]]:
    """ Читает CSV и возвращает список словарей """
    if not csv_path.exists():
        return []
    
    with open(csv_path, 'r', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        return list(reader)

def filter_data(data: List[Dict[str, str]], arguments: Dict[str, Any]) -> List[Dict[str, str]]:
    """ Фильтрует данные по совпадению ключей из arguments (case-insensitive) """
    if not arguments:
        return data
    filtered = []
    for row in data:
        match = True
        for key, value in arguments.items():
            # Ищем ключ в строке CSV без учета регистра
            csv_key = next((k for k in row.keys() if k.lower() == key.lower()), None)
            if csv_key:
                if str(row[csv_key]).strip().lower() != str(value).strip().lower():
                    match = False
                    break
            else:
                # Если ключа нет в CSV, считаем что фильтр не пройден (или можно игнорировать)
                match = False
                break
        if match:
            filtered.append(row)
    return filtered

def main():
    parser = argparse.ArgumentParser(description="PM IQ Dynamic CSV Mock MCP Server")
    parser.add_argument("--plugin", required=True, help="Name of the plugin to serve (e.g., pm-project-core)")
    args = parser.parse_args()

    plugin_dir = get_plugin_dir(args.plugin)
    if not plugin_dir.exists():
        print(f"Error: Plugin directory not found: {plugin_dir}", file=sys.stderr)
        sys.exit(1)

    app = Server(f"pm-iq-mock-{args.plugin}")
    
    # Cобираем инструменты из .mcp.json
    mcp_config_path = plugin_dir / ".mcp.json"
    if not mcp_config_path.exists():
        print(f"Error: .mcp.json not found in {plugin_dir}", file=sys.stderr)
        sys.exit(1)

    with open(mcp_config_path, 'r', encoding='utf-8') as f:
        mcp_config = json.load(f)

    # Извлекаем список инструментов
    tools_list = []
    mcp_servers = mcp_config.get("mcpServers", {})
    for server_name, server_config in mcp_servers.items():
        for tool_def in server_config.get("tools", []):
            # Если tool_def это строка, создаем базовый Tool объект
            if isinstance(tool_def, str):
                tools_list.append(Tool(
                    name=tool_def,
                    description=f"Dynamic tool for {tool_def} (data sourced from CSV)",
                    inputSchema={"type": "object", "properties": {}, "required": []}
                ))
            else:
                tools_list.append(Tool(**tool_def))

    # Cканируем папки скиллов и сопоставляем инструменты с CSV файлами
    # Словарь: { "tool_name": Path_to_csv }
    tool_to_csv_map = {}
    skills_dir = plugin_dir / "skills"
    if skills_dir.exists():
        for skill_dir in skills_dir.iterdir():
            if skill_dir.is_dir():
                # Ищем CSV файл, имя которого совпадает с именем инструмента
                for csv_file in skill_dir.glob("*.csv"):
                    tool_name = csv_file.stem # имя файла без .csv
                    # Проверяем, что этот инструмент действительно объявлен в .mcp.json
                    if any(t.name == tool_name for t in tools_list):
                        tool_to_csv_map[tool_name] = csv_file

    @app.list_tools()
    async def list_tools() -> List[Tool]:
        return tools_list

    @app.call_tool()
    async def call_tool(name: str, arguments: dict):
        # Ищем CSV для этого инструмента
        csv_path = tool_to_csv_map.get(name)
        
        if not csv_path:
            return [TextContent(type="text", text=json.dumps({"error": f"No mock data file found for tool '{name}'"}, ensure_ascii=False))]
        
        # Читаем и фильтруем данные
        raw_data = load_csv_as_dicts(csv_path)
        filtered_data = filter_data(raw_data, arguments)
        
        # Формируем ответ
        response = {
            "tool": name,
            "source_file": csv_path.name,
            "arguments_used": arguments,
            "records_found": len(filtered_data),
            "data": filtered_data
        }
        
        return [TextContent(type="text", text=json.dumps(response, ensure_ascii=False, indent=2))]

    async def run():
        async with stdio_server() as (read_stream, write_stream):
            await app.run(read_stream, write_stream, app.create_initialization_options())

    asyncio.run(run())

if __name__ == "__main__":
    main()