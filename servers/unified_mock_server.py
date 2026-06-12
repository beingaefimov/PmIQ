""" Dynamic CSV-based Mock MCP Server for PM IQ with Declarative ECharts Widget Generation.
Сканирует папку плагина, находит CSV-файлы (мок-данные) в папках скиллов и отдает эти данные как MCP-сервер.
Меню виджетов динамически строится из available_widgets в SKILL.md каждого скилла """

import asyncio
import argparse
import csv
import json
import sys
from pathlib import Path
from typing import List, Dict, Any

import yaml
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
        return list(csv.DictReader(f))

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

def convert_to_markdown_table(data: List[Dict[str, str]]) -> str:
    """ Конвертирует список словарей в Markdown-таблицу """
    if not data:
        return "(нет данных)"
    
    # Получаем заголовки из ключей первой строки
    headers = list(data[0].keys())
    
    # Строим заголовок таблицы
    header_row = "| " + " | ".join(headers) + " |"
    separator_row = "|" + "|".join(["---"] * len(headers)) + "|"
    
    # Строим строки данных
    data_rows = []
    for row in data:
        values = [str(row.get(h, "")) for h in headers]
        data_rows.append("| " + " | ".join(values) + " |")
    
    return "\n".join([header_row, separator_row] + data_rows)

def parse_skill_available_widgets(skill_dir: Path) -> List[Dict[str, str]]:
    """ Парсит SKILL.md и извлекает available_widgets из YAML frontmatter """
    skill_file = skill_dir / "SKILL.md"
    if not skill_file.exists():
        return []
    
    try:
        with open(skill_file, 'r', encoding='utf-8') as f:
            content = f.read()
        
        if not content.startswith('---'):
            return []
        
        parts = content.split('---', 2)
        if len(parts) < 3:
            return []
        
        frontmatter = yaml.safe_load(parts[1]) or {}
        return frontmatter.get("available_widgets", [])
    except Exception as e:
        print(f"[WARNING] Failed to parse SKILL.md in {skill_dir}: {e}", file=sys.stderr, flush=True)
        return []

# Генераторы JSON для виджетов (ECharts JSON & Action Cards)
# Обоснование: LLM 7B-8B не может и не должна генерировать
# сложный JSON для виджетов самостоятельно. Она гарантированно допустит
# синтаксическую ошибку или галлюцинацию в схеме

def generate_echarts_barchart(data: List[Dict[str, str]]) -> Dict[str, Any]:
    names = [row.get("resource_name", "Unknown") for row in data]
    values = []
    colors = []
    for row in data:
        try:
            val = int(row.get("allocation_pct", 0))
        except ValueError:
            val = 0
        values.append(val)
        colors.append("#ee6666" if val > 100 else "#5470c6")

    return {
        "widget_type": "echarts",
        "chart_type": "BarChart",
        "title": "BarChart",    # Технический title - LLM заменит на осмысленный
        "option": {
            "tooltip": {"trigger": "axis", "axisPointer": {"type": "shadow"}},
            "xAxis": {"type": "category", "data": names, "axisLabel": {"rotate": 30}},
            "yAxis": {"type": "value", "name": "Загрузка (%)", "max": 150},
            "series": [{
                "type": "bar",
                "data": [{"value": v, "itemStyle": {"color": c}} for v, c in zip(values, colors)],
                "label": {"show": True, "position": "top", "formatter": "{c}%"}
            }]
        }
    }

def generate_echarts_piechart(data: List[Dict[str, str]]) -> Dict[str, Any]:
    status_counts = {}
    for row in data:
        status = row.get("status", "Unknown")
        status_counts[status] = status_counts.get(status, 0) + 1
    
    return {
        "widget_type": "echarts",
        "chart_type": "PieChart",
        "title": "PieChart",    # Технический title - LLM заменит на осмысленный
        "option": {
            "tooltip": {"trigger": "item"},
            "series": [{
                "type": "pie",
                "radius": "50%",
                "data": [{"value": v, "name": k} for k, v in status_counts.items()],
                "emphasis": {"itemStyle": {"shadowBlur": 10, "shadowOffsetX": 0, "shadowColor": "rgba(0, 0, 0, 0.5)"}}
            }]
        }
    }

def generate_echarts_scatterchart(data: List[Dict[str, str]]) -> Dict[str, Any]:
    prob_map = {"Low": 1, "Medium": 2, "High": 3}
    impact_map = {"Low": 1, "Medium": 2, "High": 3}
    
    scatter_data = []
    for row in data:
        p = prob_map.get(row.get("probability", "Medium"), 2)
        i = impact_map.get(row.get("impact", "Medium"), 2)
        scatter_data.append({
            "value": [p, i],
            "name": row.get("risk_id", "Unknown"),
            "description": row.get("description", "")
        })

    return {
        "widget_type": "echarts",
        "chart_type": "ScatterChart",
        "title": "ScatterChart",    # Технический title - LLM заменит на осмысленный
        "option": {
            "tooltip": {
                "formatter": "function(params) { return '<b>' + params.data.name + '</b><br/>' + params.data.description; }"
            },
            "xAxis": {"type": "value", "name": "Вероятность", "min": 0, "max": 4, "interval": 1},
            "yAxis": {"type": "value", "name": "Воздействие", "min": 0, "max": 4, "interval": 1},
            "series": [{
                "type": "scatter",
                "symbolSize": 20,
                "data": scatter_data,
                "itemStyle": {"color": "#fac858"}
            }]
        }
    }

def generate_action_card(data: List[Dict[str, str]]) -> Dict[str, Any]:
    """ Генерирует интерактивную карточку с кнопкой (пример) """
    high_risks = [r for r in data if r.get("impact") == "High" and r.get("status") == "Active"]
    if not high_risks:
        return None
    
    top_risk = high_risks[0]
    return {
        "widget_type": "action_card",
        "chart_type": "ActionCard",
        "title": "Интерактивная карточка",    # Технический title - LLM заменит на осмысленный
        "message": f"Риск {top_risk.get('risk_id')}: {top_risk.get('description')}",
        "button": {
            "text": "Создать план реагирования",
            "action": "create_mitigation_plan",
            "payload": {"risk_id": top_risk.get("risk_id"), "owner": top_risk.get("owner")}
        }
    }

# TODO: Маппинг типов виджетов в функции-генераторы
WIDGET_GENERATORS = {
    "BarChart": generate_echarts_barchart,
    "PieChart": generate_echarts_piechart,
    "ScatterChart": generate_echarts_scatterchart,
    "ActionCard": generate_action_card
}

# TODO: Меню виджетов
def build_dynamic_widget_menu(plugin_dir: Path, tools_list: List[Tool]) -> Dict[str, List[Dict]]:
    """ Строит меню виджетов на основе available_widgets в SKILL.md """
    widget_menu = {}
    skills_dir = plugin_dir / "skills"
    
    if not skills_dir.exists():
        return widget_menu
    
    for skill_dir in skills_dir.iterdir():
        if not skill_dir.is_dir():
            continue
        
        available_widgets = parse_skill_available_widgets(skill_dir)
        
        if not available_widgets:
            continue
        
        # Находим CSV файлы в этой папке скилла
        for csv_file in skill_dir.glob("*.csv"):
            tool_name = csv_file.stem
            
            # Проверяем, что этот инструмент объявлен в .mcp.json
            if any(t.name == tool_name for t in tools_list):
                # Строим меню виджетов для этого инструмента
                widget_configs = []
                for widget_def in available_widgets:
                    widget_type = widget_def.get("type")
                    widget_description = widget_def.get("description", "")
                    
                    if widget_type in WIDGET_GENERATORS:
                        widget_configs.append({
                            "type": widget_type,
                            "description": widget_description,
                            "func": WIDGET_GENERATORS[widget_type]
                        })
                    else:
                        print(f"[WARNING] Unknown widget type '{widget_type}' in {skill_dir}", file=sys.stderr, flush=True)
                
                if widget_configs:
                    widget_menu[tool_name] = widget_configs
    
    return widget_menu

def main():
    parser = argparse.ArgumentParser(description="PM IQ Dynamic CSV Mock MCP Server")
    parser.add_argument("--plugin", required=True, help="Name of the plugin to serve")
    args = parser.parse_args()

    plugin_dir = get_plugin_dir(args.plugin)
    if not plugin_dir.exists():
        print(f"Error: Plugin directory not found: {plugin_dir}", file=sys.stderr)
        sys.exit(1)

    app = Server(f"pm-iq-mock-{args.plugin}")
    
    # Cобираем инструменты из .mcp.json
    mcp_config_path = plugin_dir / ".mcp.json"
    with open(mcp_config_path, 'r', encoding='utf-8') as f:
        mcp_config = json.load(f)

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
                    tool_name = csv_file.stem
                    # Проверяем, что этот инструмент действительно объявлен в .mcp.json
                    if any(t.name == tool_name for t in tools_list):
                        tool_to_csv_map[tool_name] = csv_file

    dynamic_widget_menu = build_dynamic_widget_menu(plugin_dir, tools_list)
    print(f"[DEBUG] Dynamic widget menu built for {len(dynamic_widget_menu)} tools: {list(dynamic_widget_menu.keys())}", file=sys.stderr, flush=True)

    @app.list_tools()
    async def list_tools() -> List[Tool]:
        return tools_list

    @app.call_tool()
    async def call_tool(name: str, arguments: dict):
        print(f"\n[DEBUG] Tool called: {name} with args: {arguments}", file=sys.stderr, flush=True)
        
        try:
            # Ищем CSV для этого инструмента
            csv_path = tool_to_csv_map.get(name)
            if not csv_path:
                error_msg = {"error": f"No mock data file found for tool '{name}'"}
                print(f"[DEBUG] Error: {error_msg}", file=sys.stderr, flush=True)
                return [TextContent(type="text", text=json.dumps(error_msg, ensure_ascii=False))]
            
            print(f"[DEBUG] Loading CSV: {csv_path}", file=sys.stderr, flush=True)
            raw_data = load_csv_as_dicts(csv_path)
            print(f"[DEBUG] Loaded {len(raw_data)} rows", file=sys.stderr, flush=True)
            
            filtered_data = filter_data(raw_data, arguments)
            print(f"[DEBUG] Filtered to {len(filtered_data)} rows", file=sys.stderr, flush=True)
            
            response = {
                "data": convert_to_markdown_table(filtered_data),
                "widgets": []
            }

            widget_configs = dynamic_widget_menu.get(name, [])
            print(f"[DEBUG] Widget configs for {name}: {[c['type'] for c in widget_configs]}", file=sys.stderr, flush=True)
            
            for config in widget_configs:
                try:
                    widget_data = config["func"](filtered_data)
                    if widget_data:
                        # Добавляем описание из SKILL.md, чтобы LLM могла принять решение
                        widget_data["description"] = config.get("description", "")
                        response["widgets"].append(widget_data)
                        print(f"[DEBUG] Widget {config['type']} generated with description", file=sys.stderr, flush=True)
                except Exception as e:
                    print(f"[ERROR] Failed to generate widget {config['type']}: {e}", file=sys.stderr, flush=True)
                    import traceback
                    traceback.print_exc(file=sys.stderr)

            print(f"[DEBUG] Final response: {len(response['widgets'])} widgets", file=sys.stderr, flush=True)
            return [TextContent(type="text", text=json.dumps(response, ensure_ascii=False, indent=2))]
            
        except Exception as e:
            print(f"[FATAL ERROR] in call_tool: {e}", file=sys.stderr, flush=True)
            import traceback
            traceback.print_exc(file=sys.stderr)
            return [TextContent(type="text", text=json.dumps({"error": str(e)}, ensure_ascii=False))]
    
    async def run():
        async with stdio_server() as (read_stream, write_stream):
            await app.run(read_stream, write_stream, app.create_initialization_options())

    asyncio.run(run())

if __name__ == "__main__":
    main()