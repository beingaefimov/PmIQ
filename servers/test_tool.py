""" Тестировщик MCP-инструментов сервера unified_mock_server.py.
Использование: python3 test_tool.py <plugin_name> <tool_name> [json_arguments] """

import asyncio
import json
import sys
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

async def test_tool(plugin_name: str, tool_name: str, arguments: dict):
    server_params = StdioServerParameters(
        command="python3",
        args=["unified_mock_server.py", "--plugin", plugin_name])
    print(f"Запуск сервера для плагина: {plugin_name}")
    async with stdio_client(server_params) as (read, write):
        async with ClientSession(read, write) as session:
            print("Инициализация...")
            await session.initialize()
            print(f"Вызов инструмента: {tool_name}")
            print(f"Аргументы: {json.dumps(arguments, ensure_ascii=False)}")
            print("=" * 60)
            try:
                result = await session.call_tool(tool_name, arguments)
                for item in result.content:
                    if hasattr(item, 'text'):
                        data = json.loads(item.text)
                        print(json.dumps(data, ensure_ascii=False, indent=2))
            except Exception as e:
                print(f"Ошибка при вызове: {e}", file=sys.stderr)
                import traceback
                traceback.print_exc()

if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Использование: python3 test_tool.py <plugin> <tool> [json_args]")
        print("Пример: python3 test_tool.py pm-resource-and-procurement get_resource_histogram '{}'")
        sys.exit(1)
    plugin = sys.argv[1]
    tool = sys.argv[2]
    args = json.loads(sys.argv[3]) if len(sys.argv) > 3 else {}
    asyncio.run(test_tool(plugin, tool, args))