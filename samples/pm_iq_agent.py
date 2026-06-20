from __future__ import annotations

import asyncio
import json
import re
import sys
import fnmatch
import yaml
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from openai import OpenAI
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from widget_renderer import render_widget
from agent_utils import (
    parse_llm_response,
    detect_loop,
    detect_hallucinations,
    expand_widget_descriptors,
    extract_plan_block,
    parse_plan_steps,
    is_consecutive_duplicate,
    make_call_signature,
)

# "concentrator" — исходный ReAct-цикл с историей диалога + концентратор знаний.
# "stateless" — альтернативный цикл: повторный роутинг каждую итерацию,
#               накопление фактических данных вместо истории, анти-луп
#               через стек последних 2 вызовов
LOOP_MODE = "stateless"

# Маппинг внешних имён режимов для обратной совместимости
_MODE_DISPATCH = {
    "concentrator": "history",
    "history":      "history",
    "stateless":    "stateless"}

PM_IQ_ROOT = Path(__file__).parent.parent

LLM_BASE_URL = "http://localhost:3101/v1"
LLM_MODEL_NAME = "local-model"
_MAX_TOKENS = 8096
_TEMPERATURE = 0.1
_MAX_ITERATIONS_HISTORY = 16
_MAX_ITERATIONS_STATELESS = 16
# В stateless-режиме: размер стека последних вызовов для анти-loop
_STATELESS_RECENT_STACK_SIZE = 2

class PmIqStructureParser:
    """ Парсер каталога плагинов и их скиллов из файловой системы """
    
    def __init__(self, root_path: Path):
        self.root_path = root_path
        self.plugins_dir = root_path / "plugins"
        self.plugins: List[Dict[str, Any]] = []

    def _parse_skill(self, skill_dir: Path) -> Optional[Dict[str, Any]]:
        skill_file = skill_dir / "SKILL.md"
        if not skill_file.exists():
            return None
        with open(skill_file, "r", encoding="utf-8") as f:
            content = f.read()
        if not content.startswith("---"):
            return None
        parts = content.split("---", 2)
        if len(parts) < 3:
            return None
        frontmatter = yaml.safe_load(parts[1]) or {}
        full_instructions = f"{parts[1].strip()}\n---\n{parts[2].strip()}"
        return {
            "name": frontmatter.get("name", skill_dir.name),
            "description": frontmatter.get("description", ""),
            "instructions": full_instructions,
            "path": skill_dir,
            "available_widgets": frontmatter.get("available_widgets", [])}

    def _parse_plugin(self, plugin_dir: Path) -> Dict[str, Any]:
        plugin_info = {
            "name": plugin_dir.name,
            "path": plugin_dir,
            "skills": [],
            "mcp_config": None}
        mcp_config_file = plugin_dir / ".mcp.json"
        if mcp_config_file.exists():
            with open(mcp_config_file, "r", encoding="utf-8") as f:
                plugin_info["mcp_config"] = json.load(f)
        skills_dir = plugin_dir / "skills"
        if skills_dir.exists():
            for skill_dir in sorted(skills_dir.iterdir()):
                if skill_dir.is_dir():
                    skill_info = self._parse_skill(skill_dir)
                    if skill_info:
                        plugin_info["skills"].append(skill_info)
        return plugin_info

    def scan_plugins(self) -> List[Dict[str, Any]]:
        if not self.plugins_dir.exists():
            raise ValueError(f"Директория плагинов не найдена: {self.plugins_dir}")
        for plugin_dir in sorted(self.plugins_dir.iterdir()):
            if not plugin_dir.is_dir() or plugin_dir.name.startswith("."):
                continue
            plugin_info = self._parse_plugin(plugin_dir)
            if plugin_info:
                self.plugins.append(plugin_info)
        return self.plugins

class PmIqAgent:
    def __init__(self):
        self.llm_client = OpenAI(base_url=LLM_BASE_URL, api_key="not-needed")
        self.structure = PmIqStructureParser(PM_IQ_ROOT)
        self.mcp_sessions: Dict[str, ClientSession] = {}
        self.all_tools: List[Dict[str, Any]] = []
        self._context_managers: List[tuple] = []
        self.data_cache: Dict[str, List[Dict[str, str]]] = {}
        # Для history-режима
        self._tool_call_history: Dict[tuple, str] = {}
        self._tool_call_counts: Dict[tuple, int] = {}
        # Для stateless-режима - стек последних вызовов (tool_name, args)
        self._recent_calls_stack: List[Tuple[str, Dict[str, Any]]] = []
        # Концентратор знаний (используется только в history-режиме).
        # Импортируем лениво, чтобы не тащить зависимость в stateless-режим
        from concentrator import SkillConcentrator
        self.concentrator = SkillConcentrator(
            llm_client=self.llm_client,
            llm_model_name=LLM_MODEL_NAME,
            structure=self.structure)

    async def initialize(self):
        print("Сканирование структуры...")
        self.structure.scan_plugins()
        print(f"Найдено {len(self.structure.plugins)} плагинов")
        print("\nПодключение к MCP серверам (согласно .mcp.json)...")
        await self._connect_to_mcp_servers()
        print(f"Загружено {len(self.all_tools)} инструментов (внутренний реестр)")

    async def close(self):
        """ Подавляет шумные BaseExceptionGroup / GeneratorExit от anyio/mcp """
        for stdio_cm, session in self._context_managers:
            try:
                await session.__aexit__(None, None, None)
            except Exception:
                pass
            try:
                await stdio_cm.__aexit__(None, None, None)
            except Exception:
                pass

    async def _connect_to_mcp_servers(self):
        for plugin in self.structure.plugins:
            if not plugin["mcp_config"]:
                continue
            mcp_servers = plugin["mcp_config"].get("mcpServers", {})
            for server_name, server_config in mcp_servers.items():
                try:
                    session = await self._connect_to_server(server_name, server_config)
                    if session:
                        self.mcp_sessions[server_name] = session
                        tools_result = await session.list_tools()
                        self.all_tools.extend([
                            {**tool.model_dump(), "server": server_name, "plugin": plugin["name"]}
                            for tool in tools_result.tools])
                except Exception as e:
                    print(f"Ошибка подключения к {server_name}: {e}")

    async def _connect_to_server(self, server_name: str, config: dict) -> ClientSession:
        command = config.get("command", "python3")
        args = config.get("args", [])
        env = config.get("env", {})
        server_params = StdioServerParameters(command=command, args=args, env=env)
        stdio_cm = stdio_client(server_params)
        read, write = await stdio_cm.__aenter__()
        session = ClientSession(read, write)
        await session.__aenter__()
        await session.initialize()
        self._context_managers.append((stdio_cm, session))
        return session

    async def run(self, query: str, mode: Optional[str] = None) -> str:
        effective_mode = _MODE_DISPATCH.get(mode, None) if mode else LOOP_MODE
        if effective_mode is None:
            raise ValueError(
                f"Неизвестный режим: {mode!r}. "
                f"Допустимые значения: {list(_MODE_DISPATCH.keys())}")
        if effective_mode == "history":
            return await self.run_history_loop(query)
        elif effective_mode == "stateless":
            return await self.run_stateless_loop(query)
        else:
            raise ValueError(f"Неизвестный внутренний режим: {effective_mode!r}")

    async def _call_llm(self, messages: List[Dict[str, str]],
        temperature: float = _TEMPERATURE,
        max_tokens: int = _MAX_TOKENS) -> str:
        loop = asyncio.get_event_loop()

        def sync_call():
            return self.llm_client.chat.completions.create(
                model=LLM_MODEL_NAME,
                messages=messages,
                temperature=temperature,
                max_tokens=max_tokens)
        response = await loop.run_in_executor(None, sync_call)
        return response.choices[0].message.content

    async def route_query(self, query: str,
        accumulated_data: str = "") -> List[str]:
        """ Выбор 1–3 релевантных плагинов по вопросу.
        В stateless-режиме в accumulated_data передаётся накопленный блок
        фактических данных, чтобы маршрутизация учитывала уже собранное """
        plugins_summary = []
        for p in self.structure.plugins:
            skills_desc = "\n    ".join(
                [f"- {s['name']}: {s['description']}" for s in p["skills"]])
            plugins_summary.append(f"### {p['name']}\n    {skills_desc}")

        data_section = ""
        if accumulated_data:
            data_section = (
                "\n\nУже известные данные компании:\n"
                f"{accumulated_data}\n")

        prompt = (
            "Вы — маршрутизатор запросов по управлению проектами.\n"
            "Проанализируйте запрос пользователя и выберите от 1 до 3 НАИБОЛЕЕ "
            "РЕЛЕВАНТНЫХ доменов-плагинов.\n\n"
            "КРИТИЧЕСКОЕ УКАЗАНИЕ:\n"
            "- Если запрос упоминает НЕСКОЛЬКО аспектов (например, задержки И "
            "бюджет, расписание И риски), вы ОБЯЗАНЫ выбрать ВСЕ релевантные "
            "плагины, а не только один.\n"
            "- Внимательно читайте описания скиллов. Каждое описание объясняет, "
            "какие типы запросов обрабатывает скилл.\n"
            "- Сопоставляйте интент пользователя с теми скиллами, чьи описания "
            "покрывают релевантные аспекты.\n\n"
            f"Доступные домены со скиллами:\n{chr(10).join(plugins_summary)}\n"
            f"{data_section}\n"
            f"Запрос пользователя: {query}\n\n"
            "Ответьте ТОЛЬКО валидным JSON-массивом имён плагинов.\n"
            'Пример: ["pm-project-core", "pm-value-and-performance"]\n'
            "Не добавляйте markdown-форматирование и пояснения."
        )

        raw = await self._call_llm(
            [{"role": "user", "content": prompt}], temperature=0.0)
        raw = raw.strip()
        print("=" * 80)
        print("СЫРОЙ ОТВЕТ ОТ LLM (роутер):")
        print("=" * 80)
        print(raw)
        print("=" * 80 + "\n")
        raw = raw.replace("```json", "").replace("```", "").strip()
        try:
            selected = json.loads(raw)
            if isinstance(selected, str):
                selected = [selected]
        except json.JSONDecodeError:
            print(f"Ошибка парсинга JSON от роутера. Fallback на pm-project-core.")
            return ["pm-project-core"]

        # Маппим скиллы на плагины, если LLM вернула имя скилла
        valid_plugins: List[str] = []
        for item in selected:
            if any(plug["name"] == item for plug in self.structure.plugins):
                valid_plugins.append(item)
            else:
                for plug in self.structure.plugins:
                    if any(skill["name"] == item for skill in plug["skills"]):
                        valid_plugins.append(plug["name"])
                        print(f"LLM вернула имя скилла '{item}', "
                              f"маппим на плагин '{plug['name']}'")
                        break
        valid_plugins = list(dict.fromkeys(valid_plugins))
        if not valid_plugins:
            valid_plugins = ["pm-project-core"]
        # Если в запросе есть упоминание проекта, 
        # принудительно добавляем pm-project-core,
        # чтобы агент получил identify_project
        project_triggers = ["проект", "project", "внедрени", "стройк", "мобилк", "цод", "миграц", "фаза"]
        if any(trigger in query.lower() for trigger in project_triggers):
            if "pm-project-core" not in valid_plugins:
                valid_plugins.append("pm-project-core")
                print(f"[HARD ROUTE] Обнаружен контекст проекта. Принудительно добавлен pm-project-core")
        print(f"Маршрутизация: выбраны плагины {valid_plugins}")
        return valid_plugins

    async def _execute_tool_call(
        self,
        tool_name: str,
        action_input: Dict[str, Any]) -> Tuple[str, Optional[List[Dict[str, str]]]]:
        """ Вызывает MCP-инструмент. Возвращает (observation_text, raw_data).
        raw_data может быть None, если ответ не содержит _raw_data """
        tool = next((t for t in self.all_tools if t["name"] == tool_name), None)
        if tool is None:
            # Fuzzy-матчинг на опечатки
            from difflib import get_close_matches
            matches = get_close_matches(
                tool_name, [t["name"] for t in self.all_tools], n=1, cutoff=0.7)
            if matches:
                tool = next(t for t in self.all_tools if t["name"] == matches[0])
                print(f"Автоисправление (fuzzy): '{tool_name}' -> '{tool['name']}'")
                tool_name = tool["name"]
        if tool is None:
            available = ", ".join(t["name"] for t in self.all_tools)
            return (f"Error: Tool '{tool_name}' not found. "
                    f"Available tools: {available}", None)
        try:
            session = self.mcp_sessions[tool["server"]]
            print(f"Вызов MCP-сервера: {tool['server']}, инструмент: {tool_name}")
            print(f"Аргументы: {json.dumps(action_input, ensure_ascii=False)}")
            result = await session.call_tool(tool_name, action_input)
            observation = "\n".join([
                str(item.text) if hasattr(item, "text") else str(item)
                for item in result.content])
        except Exception as e:
            import traceback
            traceback.print_exc()
            return f"Error executing tool: {e}", None
        raw_data: Optional[List[Dict[str, str]]] = None
        try:
            obs_json = json.loads(observation)
            if isinstance(obs_json, dict) and "_raw_data" in obs_json:
                raw_data = obs_json.pop("_raw_data")
                observation = json.dumps(obs_json, ensure_ascii=False, indent=2)
                print(f"[CACHE] Извлечено {len(raw_data)} строк для {tool_name}")
        except json.JSONDecodeError:
            pass
        return observation, raw_data

    def _cache_tool_data(self, tool_name: str, raw_data: Optional[List[Dict[str, str]]]):
        if raw_data is None:
            return
        # Кэшируем только если ещё нет (в stateless-режиме повторные вызовы
        # того же инструмента с теми же args блокируются анти-loop, но
        # разные args могут быть - данные могут обновляться или иметь иной смысл в контексте).
        # Здесь используем простое поведение: последний выигрывает
        self.data_cache[tool_name] = raw_data

    def _build_intent_registry(self) -> Dict[str, Dict[str, Any]]:
        registry: Dict[str, Dict[str, Any]] = {}
        for plugin in self.structure.plugins:
            for skill in plugin.get("skills", []):
                for widget_def in skill.get("available_widgets", []):
                    w_type = widget_def.get("type", "")
                    for intent in widget_def.get("intents", []):
                        intent_name = intent.get("name")
                        if intent_name:
                            tool_name = None
                            skill_path = skill.get("path")
                            if skill_path:
                                csvs = list(skill_path.glob("*.csv"))
                                if csvs:
                                    tool_name = csvs[0].stem
                            registry[intent_name] = {
                                "widget_type": w_type,
                                "config": intent.get("config", {}),
                                "tool_name": tool_name,
                                "description": intent.get("description", "").lower()}
        return registry

    def _render_system_widgets(self, final_answer: str) -> str:
        """ Заменяет плейсхолдеры <!-- SYSTEM_WIDGET: ... --> на ECharts JSON """
        intent_registry = self._build_intent_registry()
        result_parts: List[str] = []
        last_end = 0
        start_marker = "<!-- SYSTEM"
        end_marker = "-->"
        start_idx = final_answer.find(start_marker)
        while start_idx != -1:
            result_parts.append(final_answer[last_end:start_idx])
            end_idx = final_answer.find(end_marker, start_idx)
            if end_idx == -1:
                break
            inner_text = final_answer[start_idx + len(start_marker): end_idx]
            inner_text = inner_text.replace("_ ", "_").replace(" _", "_")
            try:
                filter_idx = inner_text.find("| FILTER:")
                if filter_idx == -1:
                    raise ValueError("Missing | FILTER: delimiter")
                intent_part = inner_text[:filter_idx].strip()
                rest_part = inner_text[filter_idx + len("| FILTER:"):].strip()
                if ":" in intent_part:
                    intent_name = intent_part.split(":")[-1].strip()
                else:
                    intent_name = intent_part.strip()
                title_idx = rest_part.find("| TITLE:")
                if title_idx == -1:
                    raise ValueError("Missing | TITLE: delimiter")
                filter_str = rest_part[:title_idx].strip()
                title = rest_part[title_idx + len("| TITLE:"):].strip()
                filter_rules = json.loads(filter_str)
                for key, value in list(filter_rules.items()):
                    if isinstance(value, str) and "|" in value:
                        filter_rules[key] = [v.strip() for v in value.split("|")]
                if intent_name not in intent_registry:
                    from difflib import get_close_matches
                    matches = get_close_matches(
                        intent_name, list(intent_registry.keys()), n=1, cutoff=0.7)
                    if matches:
                        correct = matches[0]
                        print(f"[WIDGET-FUZZY] Intent '{intent_name}' -> '{correct}'")
                        intent_name = correct
                    else:
                        result_parts.append(
                            f"<!-- ERROR: Unknown intent '{intent_name}'. "
                            f"Available: {', '.join(intent_registry.keys())} -->")
                        last_end = end_idx + len(end_marker)
                        start_idx = final_answer.find(start_marker, last_end)
                        continue
                reg = intent_registry[intent_name]
                # Защита: особые виджеты нельзя рендерить через плейсхолдер
                if reg["widget_type"] in ("action_card", "ActionCard"):
                    result_parts.append(
                        f"<!-- ERROR: intent '{intent_name}' имеет тип "
                        f"'{reg['widget_type']}' — это ОСОБЫЙ виджет. "
                        f"Его НЕЛЬЗЯ рендерить через <!-- SYSTEM_WIDGET -->. -->")
                    last_end = end_idx + len(end_marker)
                    start_idx = final_answer.find(start_marker, last_end)
                    continue
                tool_name = reg["tool_name"]
                if not tool_name or tool_name not in self.data_cache:
                    result_parts.append(
                        f"<!-- ERROR: No cached data for tool '{tool_name}' -->")
                else:
                    raw_data = self.data_cache[tool_name]
                    description = reg["description"]
                    filtered = raw_data
                    if 'period начинается со слова "month"' in description:
                        filtered = [r for r in filtered
                                    if r.get("period", "").lower().startswith("month")]
                    elif "forecast" in description and "eac" in description:
                        filtered = [r for r in filtered
                                    if r.get("scenario", "").strip() not in ("", "actual")]
                    if filter_rules:
                        temp_filtered = []
                        for row in filtered:
                            match = True
                            for key, value in filter_rules.items():
                                row_val = str(row.get(key, ""))
                                if isinstance(value, dict):
                                    continue
                                if isinstance(value, list):
                                    if not any(fnmatch.fnmatch(row_val, str(v))
                                               for v in value):
                                        match = False
                                        break
                                elif isinstance(value, str) and "*" in value:
                                    if not fnmatch.fnmatch(row_val, value):
                                        match = False
                                        break
                                else:
                                    if row_val.lower() != str(value).lower():
                                        match = False
                                        break
                            if match:
                                temp_filtered.append(row)
                        filtered = temp_filtered
                    descriptor = {
                        "widget_type": reg["widget_type"],
                        "intent": intent_name,
                        "title": title or "Chart",
                        "config": reg["config"],
                        "data_rows": filtered}
                    echarts_json = render_widget(descriptor)
                    if echarts_json:
                        result_parts.append(
                            f"```json\n{json.dumps(echarts_json, ensure_ascii=False, indent=2)}\n```")
                    else:
                        result_parts.append(
                            f"<!-- ERROR: render_widget returned None for "
                            f"{intent_name}. Filter was: {filter_rules} -->")
            except Exception as e:
                result_parts.append(
                    f"<!-- SYSTEM{inner_text}--> [WIDGET PARSER ERROR: {e}]")
            last_end = end_idx + len(end_marker)
            start_idx = final_answer.find(start_marker, last_end)
        result_parts.append(final_answer[last_end:])
        return "".join(result_parts)

    def _clean_system_widget_artifacts(self, text: str) -> str:
        """ Удаляет артефакты LLM: пустые блоки ```, 
        которые модель часто лепит сразу после плейсхолдеров <!-- SYSTEM_WIDGET --> """
        # Ищем паттерн: плейсхолдер, за которым сразу идут пустые кавычки (с пробелами или переносами строк).
        # Заменяем это только на сам плейсхолдер, вырезая кавычки
        text = re.sub(
            r"(<!--\s*SYSTEM_WIDGET:.*?-->)(\s*\n?\s*```\s*\n?\s*```)",
            r"\1",
            text,
            flags=re.DOTALL | re.IGNORECASE)
        # На всякий случай: удаляем вообще любые пустые блоки кода в тексте (если модель напишет где-то еще)
        text = re.sub(r"```\s*```", "", text)
        return text
    
    def _check_widgets_data_presence(self, final_answer: str) -> List[Dict[str, Any]]:
        """ Проверяет, что для каждого плейсхолдера <!-- SYSTEM_WIDGET -->
        в Final Answer есть данные в data_cache (т.е. питающий инструмент
        был вызван).
        Возвращает список описаний отсутствующих данных:
            [{
                "intent": "evm_curves",
                "tool_name": "calculate_evm",
                "title": "..."
            }, ...]
        Пустой список = все виджеты обеспечены данными """
        registry = self._build_intent_registry()
        missing: List[Dict[str, Any]] = []
        # Ищем все плейсхолдеры <!-- SYSTEM_WIDGET: ... | FILTER: ... | TITLE: ... -->
        # (упрощённый парсинг - только intent и title)
        pattern = re.compile(
            r"<!--\s*SYSTEM_WIDGET\s*:\s*(?P<intent>[^|]+?)\s*\|\s*"
            r"FILTER:\s*(?P<filter>[^|]+?)\s*\|\s*"
            r"TITLE:\s*(?P<title>.*?)\s*-->",
            re.DOTALL)
        for m in pattern.finditer(final_answer):
            intent_name = m.group("intent").strip()
            # Нормализация (как в _render_system_widgets)
            intent_name = intent_name.replace("_ ", "_").replace(" _", "_")
            if intent_name not in registry:
                # Fuzzy-матчинг на опечатки - берём ближайший
                from difflib import get_close_matches
                matches = get_close_matches(
                    intent_name, list(registry.keys()), n=1, cutoff=0.7)
                if matches:
                    intent_name = matches[0]
                else:
                    continue  # Неизвестный intent пропустим, рендер сам сообщит
            reg = registry[intent_name]
            # Особые виджеты (action_card) через плейсхолдер - это ошибка формата,
            # но не наша забота сейчас, пропускаем
            if reg["widget_type"] in ("action_card", "ActionCard"):
                continue
            tool_name = reg["tool_name"]
            if not tool_name or tool_name not in self.data_cache:
                missing.append({
                    "intent": intent_name,
                    "tool_name": tool_name or "(не определён)",
                    "title": m.group("title").strip()})
        return missing

    def _strip_invalid_widget_placeholders(self, text: str) -> str:
        """ Удаляет из Final Answer плейсхолдеры <!-- SYSTEM_WIDGET -->,
        которые не удалось отрендерить (неизвестный intent, нет данных и т.д.).
        Также вычищает HTML-комментарии-ошибки вида
        `<!-- ERROR: ... -->` и `<!-- Widget '...' опущен: ... -->`.
        Это робастная страховка: если модель вставила плейсхолдер с именем
        инструмента вместо intent-а (например, `get_ncr_status` вместо
        `schedule_variance_by_phase`), или если концентратор явно решил
        «без виджетов», но модель всё равно вставила плейсхолдер - пользователь
        не должен видеть технические ошибки """
        # Удаляем плейсхолдеры SYSTEM_WIDGET с неизвестными/невалидными intent-ами.
        # Парсим вручную (как в _render_system_widgets), проверяем по реестру
        registry = self._build_intent_registry()
        result_parts: List[str] = []
        last_end = 0
        start_marker = "<!-- SYSTEM"
        end_marker = "-->"
        start_idx = text.find(start_marker)
        while start_idx != -1:
            end_idx = text.find(end_marker, start_idx)
            if end_idx == -1:
                break
            inner = text[start_idx + len(start_marker): end_idx]
            inner_norm = inner.replace("_ ", "_").replace(" _", "_")
            # Извлекаем intent
            intent_name = ""
            filter_idx = inner_norm.find("| FILTER:")
            if filter_idx != -1:
                intent_part = inner_norm[:filter_idx].strip()
                if ":" in intent_part:
                    intent_name = intent_part.split(":")[-1].strip()
                else:
                    intent_name = intent_part.strip()
            # Проверяем: известен ли intent?
            is_known = intent_name in registry
            if not is_known and intent_name:
                from difflib import get_close_matches
                matches = get_close_matches(
                    intent_name, list(registry.keys()), n=1, cutoff=0.7)
                if matches:
                    is_known = True
            if is_known:
                # Оставляем - пусть _render_system_widgets обработает
                result_parts.append(text[last_end:start_idx])
                result_parts.append(text[start_idx:end_idx + len(end_marker)])
                last_end = end_idx + len(end_marker)
            else:
                # Неизвестный intent - вырезаем целиком, оставляем пустое место
                print(f"[CLEANUP] Вырезан невалидный плейсхолдер виджета: "
                      f"intent='{intent_name}'")
                result_parts.append(text[last_end:start_idx])
                last_end = end_idx + len(end_marker)
            start_idx = text.find(start_marker, last_end)
        result_parts.append(text[last_end:])
        text = "".join(result_parts)
        # Вычищаем ERROR-комментарии, которые мог оставить _render_system_widgets
        text = re.sub(
            r"<!--\s*ERROR:[^>]*?-->\s*",
            "",
            text,
            flags=re.DOTALL)
        # Вычищаем «Widget '...' опущен»-комментарии
        text = re.sub(
            r"<!--\s*Widget\s+'[^']*'\s+опущен:[^>]*?-->\s*",
            "",
            text,
            flags=re.DOTALL)
        # Убираем лишние пустые строки, которые могли появиться
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()

    def _postprocess_final_answer(self, text: str) -> str:
        """ Общая постобработка Final Answer для обоих режимов:
        1) рендер системных плейсхолдеров в ECharts JSON,
        2) раскрытие JSON-дескрипторов модельных виджетов (action_card),
        3) вычистка невалидных/ошибочных плейсхолдеров (робастная страховка) """
        text = self._clean_system_widget_artifacts(text)
        text = self._render_system_widgets(text)
        text = expand_widget_descriptors(text, render_widget)
        text = self._strip_invalid_widget_placeholders(text)
        return text

    def _build_react_prompt_history(
        self,
        query: str,
        active_tools: List[Dict[str, Any]],
        active_plugin_names: List[str],
        ctx) -> str:
        """ ReAct-промпт для history-режима на основе концентрированного контекста """
        from concentrator import SkillConcentrator, is_special_widget

        plugins_str = ", ".join(active_plugin_names)
        system_widgets, model_widgets = SkillConcentrator.split_widgets_by_type(
            ctx.planned_widgets)

        # Системные виджеты
        if system_widgets:
            sys_lines = []
            for w in system_widgets:
                sys_lines.append(
                    f"#### Intent: {w['intent']} (тип: {w['widget_type']})\n"
                    f"- Предлагаемый заголовок: {w.get('title_hint', '')}\n"
                    f"- Обоснование: {w.get('rationale', '')}\n"
                    f"- Config (ДОСЛОВНО, не модифицировать):\n"
                    f"{json.dumps(w['config'], ensure_ascii=False, indent=2)}")
            system_widgets_section = (
                "Эти виджеты рендерятся ИЗ данных таблицы `data`.\n"
                "В Final Answer вставьте плейсхолдер (один на виджет):\n"
                "  <!-- SYSTEM_WIDGET: intent | FILTER: {\"key\": \"value\"} | TITLE: ... -->\n"
                "НЕ генерируйте для них JSON с data_rows!\n\n"
                "ВАЖНО: Final Answer НЕ должен состоять только из плейсхолдера виджета. "
                "Сначала дайте текстовый ответ пользователю (анализ данных, конкретные "
                "выводы с именами/числами из Observation), затем — плейсхолдер виджета.\n\n"
                + "\n\n".join(sys_lines))
        else:
            # Если концентратор явно решил, что виджетов нет,
            # не показываем модели каталог доступных intents и не просим её
            # выбирать виджет. Финальный ответ должен быть только текстовым.
            # Если модель всё же вставит плейсхолдер - он будет вычищен
            # постобработкой
            system_widgets_section = (
                "Концентратор знаний не запланировал ни одного виджета для этого "
                "вопроса. Финальный ответ должен быть **только текстовым** — "
                "БЕЗ плейсхолдеров `<!-- SYSTEM_WIDGET -->` и БЕЗ JSON-блоков "
                "`action_card`. Дайте развёрнутый текстовый ответ пользователю "
                "с анализом данных, конкретными выводами (имена/числа из "
                "Observation) и рекомендациями.")

        # Модельные виджеты
        if model_widgets:
            required_mw = [w for w in model_widgets if w.get("required", True)]
            available_mw = [w for w in model_widgets if not w.get("required", True)]

            def render_widget_block(w: Dict[str, Any]) -> str:
                desc = w.get("description", "")
                skill = w.get("source_skill", "")
                config = w.get("config", {})
                if config:
                    config_line = (
                        "- Config (ДОСЛОВНО):\n"
                        f"{json.dumps(config, ensure_ascii=False, indent=2)}")
                else:
                    config_line = (
                        f"- Config: см. в ЗНАНИЯХ выше (YAML-блок available_widgets "
                        f"скилла {skill}). Найдите по имени intent '{w.get('intent', '')}'.")
                return (
                    f"##### Intent: {w['intent']} (тип: {w['widget_type']})\n"
                    f"- Описание (для чего): {desc}\n"
                    f"- Из скилла: {skill}\n"
                    f"- Предлагаемый заголовок: {w.get('title_hint', '')}\n"
                    f"- Обоснование: {w.get('rationale', '')}\n"
                    f"{config_line}")

            parts = [(
                "Эти виджеты ТРЕБУЮТ LLM-генерации содержимого (message, button и прочее).\n"
                "ИХ НЕЛЬЗЯ рендерить через <!-- SYSTEM_WIDGET --> placeholder!\n"
                "В Final Answer сгенерируйте полный JSON-блок ОБЯЗАТЕЛЬНО обёрнутый\n"
                "в ```json ... ``` ограждения.\n\n"
                "Шаблон JSON-блока для каждого виджета:\n"
                "```json\n"
                "{\n"
                '  "widget_type": "action_card",\n'
                '  "intent": "<имя_intent>",\n'
                '  "title": "<заголовок на языке вопроса пользователя>",\n'
                '  "message": "<текст на основе данных из Observation>",\n'
                '  "button": {"text": "<текст кнопки>", "action": "<действие>"}\n'
                "}\n"
                "```\n"
                "Поля message и button заполняете ВЫ на основе данных из Observation.\n\n"
                "КРИТИЧЕСКИЕ ПРАВИЛА ДЛЯ action_card:\n"
                "1. Каждый JSON-блок ОБЯЗАТЕЛЬНО оборачивайте в ```json ... ```.\n"
                "2. Final Answer НЕ должен состоять только из JSON-блоков. Сначала\n"
                "   дайте текстовый ответ пользователю (анализ данных, выводы),\n"
                "   затем — JSON-блоки виджетов.\n"
                "3. ПОЛЕ message ДОЛЖНО БЫТЬ УНИКАЛЬНЫМ для каждого виджета."
            )]
            if required_mw:
                req_lines = [render_widget_block(w) for w in required_mw]
                parts.append("### ОБЯЗАТЕЛЬНО СГЕНЕРИРОВАТЬ\n"
                             + "\n\n".join(req_lines))
            if available_mw:
                avail_lines = [render_widget_block(w) for w in available_mw]
                parts.append("### ДОСТУПНО (сгенерируйте если релевантные вопросу)\n"
                             + "\n\n".join(avail_lines))
            model_widgets_section = "\n\n".join(parts)
        else:
            model_widgets_section = "(Нет запланированных модельных виджетов.)"

        plan_section = ctx.plan if ctx.plan else "(Предплана нет. Анализируйте с нуля.)"
        planned_tools_hint = ""
        if ctx.planned_tools:
            planned_tools_hint = (
                f"\nПРЕДЛАГАЕМЫЙ ПОРЯДОК ИНСТРУМЕНТОВ (отклоняйтесь только с "
                f"обоснованием в Thought): {' -> '.join(ctx.planned_tools)}")
        knowledge_section = ctx.concentrated_knowledge if ctx.concentrated_knowledge else (
            "(Концентрированных знаний нет. Опирайтесь на инструменты и общие "
            "принципы PMBOK 8.)")

        return f"""**КРИТИЧЕСКОЕ ПРЕДУПРЕЖДЕНИЕ: Вы работаете с РЕАЛЬНЫМИ данными из корпоративных систем (Jira, SAP, Workday и т.п.).**
**НИКОГДА не выдумывайте, не изменяйте данные. Используйте ТОЛЬКО данные, предоставленные в Observation.**
**Если вы не уверены, попросите уточнения вместо того, чтобы гадать.**

Вы — экспертный Помощник по Управлению Проектами, придерживающийся принципов PMBOK 8.
В данный момент вы работаете в доменах: {plugins_str}

## ЗНАНИЯ
{knowledge_section}

## ЗАПЛАНИРОВАННЫЕ ВИДЖЕТЫ — СИСТЕМНЫЕ (рендер через <!-- SYSTEM_WIDGET --> placeholder)
{system_widgets_section}

## ЗАПЛАНИРОВАННЫЕ ВИДЖЕТЫ — МОДЕЛЬНЫЕ (LLM генерирует полный JSON в Final Answer)
{model_widgets_section}

## ПЛАН ВЫПОЛНЕНИЯ
{plan_section}{planned_tools_hint}

**ВАЖНО: План выше — это ИНСТРУКЦИЯ, что делать, а НЕ отчёт о сделанном.**
Вы ЕЩЁ НЕ ВЫЗЫВАЛИ ни одного инструмента. Данные для виджетов появятся ТОЛЬКО после того, как вы вызовете инструмент через `Action:` и получите `Observation:` от системы.
**ЗАПРЕЩЕНО** выдавать Final Answer с `<!-- SYSTEM_WIDGET -->` плейсхолдерами, если вы не получили ни одного Observation от системы.
Начинайте с Шага 1 плана: `Thought: ... Action: <tool> Action Input: {{}}`.

КРИТИЧЕСКИ ВАЖНЫЕ ПРАВИЛА:

0. **ПРАВИЛО ИДЕНТИФИКАЦИИ ПРОЕКТА (АБСОЛЮТНЫЙ ПРИОРИТЕТ):**
   Если в вопросе пользователя УПОМИНАЕТСЯ любой проект (даже приблизительно, например "в Стройке", "по мобильному приложению"), вы ДОЛЖНЫ:
   - ВАШ ПЕРВЫЙ ВЫЗОВ ИНСТРУМЕНТА ОБЯЗАТЕЛЬНО ДОЛЖЕН БЫТЬ: `identify_project`
   - Action Input: {{"query": "<вставьте сюда как пользователь назвал проект>"}}
   - Дождаться Observation с точным `project_id`.
   - ЗАПРЕЩЕНО вызывать `get_risk_register`, `get_resource_histogram` и ЛЮБЫЕ другие инструменты, пока вы не получите `project_id` от `identify_project`!
   - Используйте полученный `project_id` (например, "Construction_Phase1") как фильтр в следующем инструменте (например, Action Input: {{"project_name": "Construction_Phase1"}}).

1. **ЦЕЛОСТНОСТЬ ДАННЫХ:**
   - Observation содержит JSON с основным полем `data` — это Markdown-таблица.
   - Используйте ТОЛЬКО данные из поля `data`. НИКОГДА не выдумывайте имена/числа.
   - **МАППИНГ СУЩНОСТЕЙ:** сопоставляйте разговорные названия с точными системными именами.
   - В Final Answer и в `data_rows` виджетов ВСЕГДА используйте ТОЛЬКО точные системные имена.
   - **ИМЕНА ИНСТРУМЕНТОВ:** вызывайте ТОЛЬКО инструменты, упомянутые в ЗНАНИЯХ, ПЛАНЕ или ПРЕДЛАГАЕМОМ ПОРЯДКЕ.
   - **СИНТАКСИС ФИЛЬТРОВ:** в плейсхолдере `FILTER: {{}}` передавайте ТОЛЬКО простые значения или массивы строк. ЗАПРЕЩЕНО `{{"$regex": "..."}}` или `{{"$gt": 10}}`.

2. **ОТОБРАЖЕНИЕ ВИДЖЕТОВ:**
   **ВИДЖЕТЫ — ЭТО НЕ ИНСТРУМЕНТЫ. Их нельзя вызывать через `Action:`.**
   **ВИДЖЕТ ТИП А: СИСТЕМНЫЕ ВИДЖЕТЫ** — вставляйте плейсхолдер:
   <!-- SYSTEM_WIDGET: имя_intent | FILTER: {{"key": "value"}} | TITLE: Заголовок -->
   Где `имя_intent` — это ИМЯ INTENT-а из секций ЗАПЛАНИРОВАННЫЕ ВИДЖЕТЫ выше
   (например `risk_matrix_priority`, `evm_curves`, `cpi_spi_trend`).
   **ИМЯ INTENT-А — НЕ ИМЯ ИНСТРУМЕНТА!** Не подставляйте в плейсхолдер имена
   вроде `get_risk_register`, `calculate_evm`, `get_ncr_status` — это инструменты,
   а не виджеты. Если сомневаетесь — НЕ вставляйте плейсхолдер, дайте только текст.
   **ВИДЖЕТ ТИП Б: МОДЕЛЬНЫЕ ВИДЖЕТЫ (action_card)** — генерируйте полный JSON в Final Answer.

3. **ПРОВЕРКА ФАКТОВ:** перед Final Answer проверьте каждое имя и число по `data`.

4. **ЛОГИКА РАБОТЫ (ИЗБЕГАТЬ ЗАЦИКЛИВАНИЯ):**
   - В КАЖДОМ ответе ТОЛЬКО ОДИН блок: либо `Thought + Action + Action Input`, либо `Thought + Final Answer`.
   - **ЗАПРЕЩЕНО вызывать один инструмент с теми же аргументами более одного раза подряд.**
   - После Observation: данных недостаточно → новый Thought + ДРУГОЙ инструмент; данных достаточно → Final Answer.

5. **ОТКЛОНЕНИЕ ОТ ПЛАНА:** если Observation противоречит плану, ОТКЛОНЯЙТЕСЬ, но объясните в Thought.

6. **МНОЖЕСТВЕННЫЕ АСПЕКТЫ:** вызовите инструменты из ВСЕХ релевантных доменов.

7. **ВЫХОД ИЗ ТУПИКА:** при ошибке сразу выдавайте Final Answer с объяснением.

**СТРОГОЕ ПРАВИЛО ЯЗЫКА:** Final Answer на ТОМ ЖЕ языке, на котором задан вопрос.

Начните!

Вопрос пользователя: {query}"""

    async def run_history_loop(self, query: str) -> str:
        """ ReAct-цикл с накапливаемой историей диалога и концентратором """
        try:
            selected_plugins = await self.route_query(query)
        except Exception as e:
            print(f"Ошибка маршрутизации: {e}")
            return "Не удалось определить контекст запроса."
        active_tools = [t for t in self.all_tools if t["plugin"] in selected_plugins]
        if not active_tools:
            return "Не удалось найти подходящие инструменты."

        print(f"\n[Concentrator] Концентрация знаний из {len(selected_plugins)} плагинов...")
        ctx = await self.concentrator.concentrate(query, selected_plugins, active_tools)
        if ctx.fallback_used:
            print("[Concentrator] ВНИМАНИЕ: используется fallback — полный контекст")

        # Карты маппинга
        skill_to_tool_map: Dict[str, str] = {}
        plugin_to_tool_map: Dict[str, str] = {}
        for tool in active_tools:
            pn = tool.get("plugin")
            if pn and pn not in plugin_to_tool_map:
                plugin_to_tool_map[pn] = tool["name"]
        for plugin in self.structure.plugins:
            if plugin["name"] in selected_plugins:
                for skill in plugin.get("skills", []):
                    sn = skill.get("name")
                    if sn:
                        fb = next((t["name"] for t in active_tools
                                   if t["plugin"] == plugin["name"]), None)
                        if fb:
                            skill_to_tool_map[sn] = fb

        self._tool_call_history.clear()
        self._tool_call_counts.clear()

        try:
            prompt_text = self._build_react_prompt_history(
                query, active_tools, selected_plugins, ctx)
            print(f"[DEBUG] React prompt: {len(prompt_text)} символов")
            history: List[Dict[str, str]] = [
                {"role": "user", "content": prompt_text}]
        except Exception:
            import traceback
            traceback.print_exc()
            return "Ошибка при сборке системного промпта."

        for iteration in range(_MAX_ITERATIONS_HISTORY):
            print(f"\n[Итерация {iteration + 1}]")
            if detect_loop(history):
                print("Обнаружено зацикливание LLM. Принудительное завершение.")
                return "Обнаружено зацикливание LLM. Принудительное завершение"
            try:
                llm_text = await self._call_llm(history)
                print(f"LLM (полный ответ):\n{llm_text}\n" + "-" * 30)
            except Exception as e:
                print(f"Ошибка обращения к LLM: {e}")
                return "Ошибка подключения к LLM."

            parsed = parse_llm_response(llm_text)
            if parsed["type"] == "action":
                clean_msg = (
                    f"Thought: {parsed.get('thought', '')}\n"
                    f"Action: {parsed['action']}\n"
                    f"Action Input: {parsed['action_input']}")
                history.append({"role": "assistant", "content": clean_msg})
            else:
                history.append({"role": "assistant", "content": llm_text})

            if parsed["type"] == "hallucinated_observation":
                history.append({"role": "user", "content": (
                    "ОШИБКА: вы написали 'Observation:' без 'Action:'. Это нарушение "
                    "формата ReAct. Observation предоставляет ТОЛЬКО система. "
                    "Начните с 'Thought:' и вызовите инструмент, либо выдайте "
                    "'Final Answer:'.")})
                continue

            if parsed["type"] == "thought":
                history.append({"role": "user", "content": (
                    "Продолжайте. Вы написали Thought, но не сделали выбор:\n"
                    "- если данных НЕДОСТАТОЧНО — вызовите инструмент;\n"
                    "- если данных ДОСТАТОЧНО — выдайте 'Final Answer:'.")})
                continue

            if parsed["type"] == "final_answer":
                answer_text = parsed["answer"]
                has_system_widgets = "<!-- SYSTEM_WIDGET" in answer_text
                has_observations = any(
                    msg["role"] == "user" and msg["content"].startswith("Observation:")
                    for msg in history)
                if has_system_widgets and not has_observations:
                    history.append({"role": "user", "content": (
                        "ОТКЛОНЕНО. Ваш Final Answer содержит плейсхолдеры виджетов, "
                        "но вы НЕ ВЫЗВАЛИ ни одного инструмента. Данные для виджетов "
                        "берутся из Observation. Вы перепутали ПЛАН с ОТЧЁТОМ. "
                        "Начните заново: вызовите первый инструмент из плана.")})
                    continue
                observations = [
                    msg["content"] for msg in history
                    if msg["role"] == "user" and msg["content"].startswith("Observation:")]
                hallucinated = detect_hallucinations(parsed["answer"], observations)
                if hallucinated:
                    print(f"ВНИМАНИЕ: Потенциальные галлюцинации: {hallucinated}")
                final_text = self._postprocess_final_answer(parsed["answer"])
                print(f"Финальный ответ:\n{final_text}\n")
                return final_text

            # parsed["type"] == "action"
            action = parsed["action"]
            action_input_str = parsed["action_input"]
            # Маппинги skill→tool, plugin→tool
            tool = next((t for t in active_tools if t["name"] == action), None)
            if not tool:
                if action in skill_to_tool_map:
                    action = skill_to_tool_map[action]
                    print(f"[CORRECTION] skill '{parsed['action']}' -> tool '{action}'")
                    tool = next((t for t in active_tools if t["name"] == action), None)
                elif action in plugin_to_tool_map:
                    action = plugin_to_tool_map[action]
                    print(f"[CORRECTION] plugin '{parsed['action']}' -> tool '{action}'")
                    tool = next((t for t in active_tools if t["name"] == action), None)
            if not tool:
                from difflib import get_close_matches
                matches = get_close_matches(
                    action, [t["name"] for t in active_tools], n=1, cutoff=0.7)
                if matches:
                    action = matches[0]
                    tool = next(t for t in active_tools if t["name"] == action)
                    print(f"Автоисправление (fuzzy): -> '{action}'")

            if not tool:
                observation = (
                    f"Error: Tool '{parsed['action']}' not found in active tools. "
                    f"Available: {', '.join(t['name'] for t in active_tools)}")
            else:
                try:
                    action_input = json.loads(action_input_str) if action_input_str else {}
                    call_sig = (action, json.dumps(action_input, sort_keys=True))
                    call_count = self._tool_call_counts.get(call_sig, 0)
                    if call_count >= 1:
                        self._tool_call_counts[call_sig] = call_count + 1
                        print(f"[DEDUP] Повторный вызов {action} (попытка #{call_count + 1})")
                        if call_count >= 2:
                            forced = (
                                "На основе полученных данных формирую ответ.\n\n"
                                f"ВНИМАНИЕ: вы попытались вызвать '{action}' уже "
                                f"{call_count + 1} раз с одинаковыми аргументами. "
                                "Это зацикливание. Используйте ТОЛЬКО данные из "
                                "предыдущих Observation. Проанализируйте их сами "
                                "и выдайте Final Answer с виджетами согласно плану.")
                            history.append({"role": "user",
                                            "content": f"Observation: {forced}"})
                            continue
                        cached_obs = self._tool_call_history[call_sig]
                        warning = (
                            "\n\n[СИСТЕМНОЕ ПРЕДУПРЕЖДЕНИЕ] Вы уже вызывали "
                            f"'{action}' с аргументами {action_input}. НЕ ВЫЗЫВАЙТЕ "
                            "его снова. Переходите к следующему шагу или Final Answer.")
                        observation = cached_obs + warning
                    else:
                        self._tool_call_counts[call_sig] = 1
                        observation, raw_data = await self._execute_tool_call(action, action_input)
                        self._tool_call_history[call_sig] = observation
                        self._cache_tool_data(action, raw_data)
                except json.JSONDecodeError:
                    observation = "Error: Invalid JSON in Action Input."
                except Exception as e:
                    import traceback
                    traceback.print_exc()
                    observation = f"Error executing tool: {e}"

            print(f"Observation (для консоли):\n{observation[:600]}")
            history.append({"role": "user",
                            "content": f"Observation: {observation}"})
        return "Превышено количество итераций."

    def _build_stateless_react_prompt(
        self,
        query: str,
        selected_plugins: List[str],
        active_tools: List[Dict[str, Any]],
        accumulated_data: str,
        recent_calls_warning: str = "") -> str:
        """ReAct-промпт для stateless-режима.
        Ключевые отличия от history-режима:
        - В промпт подаются полные методологии отобранных плагинов (без сжатия).
        - В промпт подаётся накопленный блок фактических данных (а не история).
        - Даётся указание составить план и выполнить первый шаг.
        - Все правила в отношении виджетов обоих типов - как в режиме history.
        - Указание, что структуру виджетов формировать можно только если план
          состоит из одного шага (финального ответа) """
        plugins_str = ", ".join(selected_plugins)

        # Полные методологии отобранных плагинов
        methodologies_parts: List[str] = []
        widgets_catalog_parts: List[str] = []
        for plugin in self.structure.plugins:
            if plugin["name"] not in selected_plugins:
                continue
            for skill in plugin["skills"]:
                methodologies_parts.append(
                    f"=== Скилл: {skill['name']} (плагин: {plugin['name']}) ===\n"
                    f"{skill['instructions']}")
                # Отдельно — каталог виджетов с дословным config
                for widget_def in skill.get("available_widgets", []):
                    w_type = widget_def.get("type", "")
                    for intent in widget_def.get("intents", []):
                        intent_name = intent.get("name")
                        if intent_name:
                            widgets_catalog_parts.append(
                                f"##### Intent: {intent_name} (тип: {w_type})\n"
                                f"- Описание: {intent.get('description', '')}\n"
                                f"- Config (ДОСЛОВНО):\n"
                                f"{json.dumps(intent.get('config', {}), ensure_ascii=False, indent=2)}")
        methodologies = "\n\n".join(methodologies_parts)
        widgets_catalog = "\n\n".join(widgets_catalog_parts) if widgets_catalog_parts else "(Виджетов нет)"

        # Список доступных инструментов
        tools_list = "\n".join(
            f"- {t['name']} (из плагина: {t['plugin']})" for t in active_tools)

        # Блок фактических данных
        if accumulated_data:
            data_block = (
                "## ФАКТИЧЕСКИЕ ДАННЫЕ КОМПАНИИ\n"
                "По данным компании известно, что:\n\n"
                f"{accumulated_data}\n\n"
                "ВАЖНО: каждый блок данных выше помечен инструментом и аргументами, "
                "которыми он был получен. Это означает, что данный вызов УЖЕ ВЫПОЛНЕН. "
                "НЕ ПОВТОРЯЙТЕ вызовы инструментов с теми же аргументами, что уже "
                "выполнены (см. строки вида «получены вызовом `<tool>` с аргументами "
                "`<args>`»). Если вам нужны дополнительные данные — вызовите ДРУГОЙ "
                "инструмент, либо ТОТ ЖЕ инструмент с ДРУГИМИ аргументами.\n")
        else:
            data_block = (
                "## ФАКТИЧЕСКИЕ ДАННЫЕ КОМПАНИИ\n"
                "(Пока данных нет — это первая итерация. Вызовите инструмент, "
                "чтобы их получить.)\n")

        # Предупреждение о недавних вызовах (анти-луп)
        warning_block = recent_calls_warning or ""

        return f"""**КРИТИЧЕСКОЕ ПРЕДУПРЕЖДЕНИЕ: Вы работаете с РЕАЛЬНЫМИ данными из корпоративных систем (Jira, SAP, Workday и т.п.).**
**НИКОГДА не выдумывайте, не изменяйте данные. Используйте ТОЛЬКО данные из блока ФАКТИЧЕСКИЕ ДАННЫЕ и из Observation.**
**Если вы не уверены, попросите уточнения вместо того, чтобы гадать.**

Вы — экспертный Помощник по Управлению Проектами, придерживающийся принципов PMBOK 8.
В данный момент вы работаете в доменах: {plugins_str}

## МЕТОДОЛОГИИ (полные, изучите ВНИМАТЕЛЬНО, включая правила виджетов)
{methodologies}

## КАТАЛОГ ВИДЖЕТОВ (config — ДОСЛОВНО из методологий)
{widgets_catalog}

## ДОСТУПНЫЕ ИНСТРУМЕНТЫ
{tools_list}

{data_block}

{warning_block}

## ВОПРОС ПОЛЬЗОВАТЕЛЯ
{query}

## ИНСТРУКЦИЯ

1. Изучите методологии выше (включая сведения о виджетах: их типы, config,
   правила фильтрации данных, описания intents).

2. **ОПРЕДЕЛИТЕ ВИДЖЕТЫ ФИНАЛЬНОГО ОТВЕТА.** Какой бы вопрос ни был задан,
   ответ почти всегда требует одного или нескольких виджетов из каталога
   выше. Прямо сейчас, в Thought, решите:
   - Какие виджеты вы ХОТИТЕ вставить в Final Answer (укажите их intent-ы)?
   - Какие ДАННЫЕ нужны каждому из этих виджетов (какие поля из каких таблиц)?
   - Какой ИНСТРУМЕНТ предоставляет эти данные?

3. **ОЦЕНИТЕ, ХВАТАЕТ ЛИ ДАННЫХ — С УЧЁТОМ ВИДЖЕТОВ.** Данных достаточно
   только тогда, когда для КАЖДОГО виджета из шага 2 в блоке ФАКТИЧЕСКИЕ
   ДАННЫЕ есть соответствующая таблица. Если вы хотите вставить виджет X,
   а данных для него ещё нет — это НЕ финальный шаг, нужно вызвать
   недостающий инструмент.

   ПРИМЕР ОШИБКИ, которую нужно избегать:
   - Хочу виджет `evm_curves` (нужны данные `calculate_evm`) и
     виджет `eac_forecast_scenarios` (тоже нужны `calculate_evm`).
   - В ФАКТИЧЕСКИХ ДАННЫХ есть только `get_budget_vs_actual`.
   - НЕВЕРНО: «данных достаточно, делаю Final Answer с виджетами» —
     рендер виджетов упадёт с «No cached data for tool 'calculate_evm'».
   - ВЕРНО: «нужно ещё вызвать `calculate_evm`, план состоит из 2 шагов».

4. Составьте план действий ReAct — пронумерованный список шагов:
   - Каждый шаг — EITHER вызов инструмента (с указанием имени и args),
     EITHER подготовка финального ответа.
   - Если данных НЕ достаточно (хоть для текста, хоть для виджетов):
     план состоит из нескольких шагов (вызовы инструментов), последний
     шаг — подготовка финального ответа.
   - Если данных ДОСТАТОЧНО для текста И для всех запланированных
     виджетов: план состоит из ОДНОГО шага — подготовка финального ответа.

5. **Правило формирования виджетов (КРИТИЧНО):**
   - НА ЛЮБОМ ШАГЕ плана, КРОМЕ последнего, вы можете ТОЛЬКО вызывать
     инструменты для сбора данных. ВАМ ЗАПРЕЩЕНО формировать структуру
     для отрисовки виджетов на промежуточных шагах.
   - Виджеты (плейсхолдеры `<!-- SYSTEM_WIDGET -->` и JSON-блоки
     `action_card`) появляются **ТОЛЬКО В ФИНАЛЬНОМ ОТВЕТЕ** — то есть
     на последнем шаге плана.
   - Каталог виджетов и их config приведены выше **ДЛЯ СВЕДЕНИЯ**: вы
     видите их на каждом шаге, чтобы понимать, какие данные нужно
     собрать. Но формировать виджеты вы будете позже, на последнем шаге,
     из того набора данных, который у вас накопится к этому моменту.

6. **Результатом вашего ответа должно быть**:
   а) блок `Plan:` с пронумерованными шагами в формате:
      `Step N: Call <tool_name> with args <json> — <причина>`
      или `Step N: Final Answer — <причина>`.
   б) `Thought:` — ваше рассуждение (включая выбор виджетов и проверку
      наличия данных для них — см. шаги 2 и 3).
   в) Если в плане больше одного шага: выполните первый шаг —
      `Action: <tool_name>` и `Action Input: <json>`.
   г) Если в плане ровно один шаг и это финальный ответ —
      `Final Answer: <текст ответа на языке вопроса пользователя>` + виджеты.

## ПРАВИЛА ВИДЖЕТОВ (из методологий, без изменений)

**ВИДЖЕТЫ — ЭТО НЕ ИНСТРУМЕНТЫ. Их нельзя вызывать через `Action:`.**

**ВИДЖЕТ ТИП А: СИСТЕМНЫЕ ВИДЖЕТЫ (LineChart, BarChart, ScatterChart и т.д.)**
Эти виджеты рендерятся ИЗ данных инструментов, которые вы вызывали.
В Final Answer вставьте плейсхолдер (один на виджет):
<!-- SYSTEM_WIDGET: имя_intent | FILTER: {{"key": "value"}} | TITLE: Заголовок -->
Где `имя_intent` — это ИМЯ INTENT-а из КАТАЛОГА ВИДЖЕТОВ выше (например
`risk_matrix_priority`, `evm_curves`, `cpi_spi_trend`).
**ИМЯ INTENT-А — НЕ ИМЯ ИНСТРУМЕНТА!** Не подставляйте в плейсхолдер имена
инструментов вроде `get_risk_register`, `calculate_evm`, `get_ncr_status` —
это имена вызовов, а не виджетов. Если сомневаетесь — НЕ вставляйте
плейсхолдер, дайте только текстовый ответ.
НЕ генерируйте для них JSON с data_rows!
В FILTER передавайте ТОЛЬКО простые значения или массивы строк.
ЗАПРЕЩЕНО `{{"$regex": "..."}}` или `{{"$gt": 10}}`.

**ВИДЖЕТ ТИП Б: МОДЕЛЬНЫЕ ВИДЖЕТЫ (action_card и др.)**
ИХ НЕЛЬЗЯ рендерить через <!-- SYSTEM_WIDGET --> placeholder!
В Final Answer сгенерируйте полный JSON-блок, ОБЯЗАТЕЛЬНО обёрнутый в ```json ... ```:
```json
{{
  "widget_type": "action_card",
  "intent": "<имя_intent>",
  "title": "<заголовок на языке вопроса пользователя>",
  "message": "<текст на основе данных из ФАКТИЧЕСКИХ ДАННЫХ>",
  "button": {{"text": "<текст кнопки>", "action": "<действие>", "payload": {{...}}}}
}}
```
ПОЛЕ message ДОЛЖНО БЫТЬ УНИКАЛЬНЫМ для каждого виджета.

**ОБЩИЕ ПРАВИЛА:**
- ЗАПРЕЩЕНО генерировать виджеты "по памяти" — берите intents ТОЛЬКО из каталога выше.
- ЗАПРЕЩЕНО выдумывать имена инструментов — берите ТОЛЬКО из списка ДОСТУПНЫЕ ИНСТРУМЕНТЫ.
- В Final Answer сначала дайте текстовый ответ пользователю (анализ, конкретные
  выводы с именами/числами из ФАКТИЧЕСКИХ ДАННЫХ), затем — плейсхолдеры/JSON виджетов.
- Если запрос затрагивает несколько аспектов, включайте ВСЕ релевантные виджеты.

## СИНТАКСИС ОТВЕТА (СТРОГО)

```
Plan:
Step 1: Call <tool_name> with args {{"k": "v"}} — причина
Step 2: Call <tool_name> with args {{}} — причина
Step 3: Final Answer — причина

Thought: <ваше рассуждение, почему план именно такой>
Action: <tool_name>
Action Input: {{"k": "v"}}
```

ИЛИ (одношаговый план = финальный ответ):

```
Plan:
Step 1: Final Answer — причина

Thought: <ваше рассуждение>
Final Answer: <текст ответа>
<!-- SYSTEM_WIDGET: ... | FILTER: {{}} | TITLE: ... -->
```json
{{ ... action_card ... }}
```
```

## КРИТИЧЕСКИЕ ПРАВИЛА
- **НЕ ПОВТОРЯЙТЕ ВЫЗОВЫ ИНСТРУМЕНТОВ** с теми же аргументами, что уже
  выполнены. Каждый блок фактических данных выше помечен инструментом и
  аргументами вызова — это ваша история вызовов. Перед составлением плана
  ПРОСМОТРИТЕ эти метки и НЕ включайте в план уже выполненные вызовы.
  Подряд-повтор будет принудительно пропущен системой; непоследовательный
  повтор (через несколько итераций) — не будет принудительно заблокирован,
  но БЕССМЫСЛЕН, так как данные не изменятся.
- Если данных достаточно — НЕ вызывайте инструменты, сразу давайте Final Answer.
- Если данных не достаточно — НЕ пытайтесь угадать ответ, вызовите инструмент
  (но НЕ из уже выполненных — см. выше).
- Имена сущностей в ответе — ТОЛЬКО из фактических данных.

**СТРОГОЕ ПРАВИЛО ЯЗЫКА:** Final Answer на ТОМ ЖЕ языке, на котором задан вопрос.

Начните!
"""

    def _format_accumulated_data(self, accumulated: List[Tuple[str, Dict[str, Any], str]]) -> str:
        """ Форматирует список прошлых вызовов (tool, args, observation)
        в единый блок фактических данных.
        Перед каждым блоком данных приписывается, каким инструментом и с какими
        аргументами он был получен - это даёт модели явный сигнал, что вызов
        уже выполнялся, и помогает избежать повторов (в т.ч. не подряд).
        Системные уведомления (`_system_notice`) выделяются в отдельную секцию """
        if not accumulated:
            return ""
        data_parts: List[str] = []
        notice_parts: List[str] = []
        data_index = 0
        for entry in accumulated:
            tool, args, obs = entry
            if tool == "_system_notice":
                notice_parts.append(obs)
                continue
            data_index += 1
            args_str = json.dumps(args, ensure_ascii=False)
            header = (
                f"--- Данные {data_index}: получены вызовом "
                f"`{tool}` с аргументами `{args_str}` ---"
            )
            # Извлекаем Markdown-таблицу из observation, если возможно
            body = obs
            try:
                obs_json = json.loads(obs)
                if isinstance(obs_json, dict):
                    data_md = obs_json.get("data")
                    if data_md:
                        body = data_md
            except json.JSONDecodeError:
                pass
            data_parts.append(f"{header}\n{body}")
        sections: List[str] = []
        if data_parts:
            sections.append("\n\n".join(data_parts))
        if notice_parts:
            sections.append("--- СИСТЕМНЫЕ УВЕДОМЛЕНИЯ ---\n" + "\n\n".join(notice_parts))
        return "\n\n".join(sections)

    def _push_recent_call(self, tool: str, args: Dict[str, Any]):
        """ Добавляет вызов в стек последних вызовов (вершина - индекс 0) """
        self._recent_calls_stack.insert(0, (tool, args))
        if len(self._recent_calls_stack) > _STATELESS_RECENT_STACK_SIZE:
            self._recent_calls_stack = self._recent_calls_stack[:_STATELESS_RECENT_STACK_SIZE]

    def _format_recent_calls_warning(self) -> str:
        """ Если стек не пуст - формирует явное предупреждение о подряд-повторе.
        Даже если модель видит все прошлые вызовы в блоке данных, это
        предупреждение дополнительно акцентирует внимание на самом свежем
        вызове (стек последних 2), потому что подряд-повтор будет
        принудительно пропущен системой """
        if not self._recent_calls_stack:
            return ""
        top_tool, top_args = self._recent_calls_stack[0]
        return (
            "## ПРЕДУПРЕЖДЕНИЕ О ПОДРЯД-ПОВТОРЕ\n"
            f"Самый свежий выполненный вызов (см. последний блок данных выше): "
            f"`{top_tool}` с аргументами "
            f"`{json.dumps(top_args, ensure_ascii=False)}`. "
            "Если ваш план начинается с ЭТОГО ЖЕ вызова (тот же инструмент + те "
            "же аргументы) — система ПРИНУДИТЕЛЬНО пропустит его и выполнит "
            "следующий шаг плана. Чтобы не тратить итерацию, СРАЗУ выберите "
            "другой первый шаг.\n")

    def _pick_first_non_duplicate_step(
            self, plan_steps: List[Dict[str, Any]],
            fallback_action: Optional[Tuple[str, Dict[str, Any]]]
            ) -> Optional[Tuple[str, Dict[str, Any]]]:
        """ Из списка шагов плана выбирает первый шаг, который не является
        подряд-дубликатом верхнего вызова из стека.
        Если план пустой - возвращает fallback_action (если он сам не дубликат) """
        for step in plan_steps:
            if step.get("kind") != "tool_call":
                continue
            tool = step["tool"]
            args = step.get("args", {}) or {}
            if not is_consecutive_duplicate(tool, args, self._recent_calls_stack):
                return (tool, args)
        # Все шаги - дубликаты (или не tool_call). Используем fallback
        if fallback_action and not is_consecutive_duplicate(
                fallback_action[0], fallback_action[1], self._recent_calls_stack):
            return fallback_action
        return None

    async def run_stateless_loop(self, query: str) -> str:
        """ Каждая итерация начинается с чистого листа,
        к запросу прирастает блок фактических данных. История диалога для LLM
        не накапливается. Анти-loop через стек последних 2 вызовов """
        accumulated: List[Tuple[str, Dict[str, Any], str]] = []
        self._recent_calls_stack.clear()

        for iteration in range(_MAX_ITERATIONS_STATELESS):
            print(f"\n{'=' * 80}\n[STATELESS Итерация {iteration + 1}]\n{'=' * 80}")

            accumulated_data = self._format_accumulated_data(accumulated)
            recent_warning = self._format_recent_calls_warning()

            # Роутинг (каждую итерацию заново, со всеми методологиями)
            try:
                selected_plugins = await self.route_query(query, accumulated_data)
            except Exception as e:
                print(f"Ошибка маршрутизации: {e}")
                return "Не удалось определить контекст запроса."

            active_tools = [t for t in self.all_tools if t["plugin"] in selected_plugins]
            if not active_tools:
                return "Не удалось найти подходящие инструменты."

            # Построение промпта (полные методологии + накопленные данные)
            prompt = self._build_stateless_react_prompt(
                query=query,
                selected_plugins=selected_plugins,
                active_tools=active_tools,
                accumulated_data=accumulated_data,
                recent_calls_warning=recent_warning)
            print(f"[DEBUG] Stateless prompt: {len(prompt)} символов")

            # Вызов LLM - модель строит план и выполняет первый шаг
            try:
                llm_text = await self._call_llm(
                    [{"role": "user", "content": prompt}])
            except Exception as e:
                print(f"Ошибка обращения к LLM: {e}")
                return "Ошибка подключения к LLM."
            print(f"LLM (полный ответ):\n{llm_text}\n" + "-" * 30)

            # Парсим ответ: извлекаем блок Plan и блок Thought/Action/Final Answer
            plan_text, rest_text = extract_plan_block(llm_text)
            plan_steps = parse_plan_steps(plan_text) if plan_text else []
            print(f"[PLAN] Шагов в плане: {len(plan_steps)}")
            for s in plan_steps:
                if s["kind"] == "tool_call":
                    print(f"  Step {s['step']}: Call {s['tool']} args={s.get('args')}")
                else:
                    print(f"  Step {s['step']}: {s['kind']}")

            parsed = parse_llm_response(rest_text)

            # Случай 1: модель решила, что данных достаточно - финальный ответ
            # (одношаговый план = Final Answer)
            is_single_final_step = (
                len(plan_steps) == 1 and plan_steps[0]["kind"] == "final_answer")
            if parsed["type"] == "final_answer" or is_single_final_step:
                if parsed["type"] != "final_answer":
                    # План одношаговый = Final Answer, но LLM не выдала Final Answer.
                    # Просим её сгенерировать финальный ответ отдельным вызовом
                    print("[STATELESS] Одношаговый план Final Answer, но LLM не выдала ответ. "
                          "Делаем дополнительный вызов для генерации ответа с виджетами.")
                    final_text = await self._generate_final_answer_stateless(
                        query=query,
                        selected_plugins=selected_plugins,
                        active_tools=active_tools,
                        accumulated_data=accumulated_data)
                    if final_text:
                        return final_text
                    # Если не вышло - продолжаем цикл
                    continue
                # Проверка: модель не вызывала ни одного инструмента, но в ответе
                # есть плейсхолдеры системных виджетов. Если данных в accumulated
                # нет, то это галлюцинация план-as-done
                answer_text = parsed["answer"]
                has_system_widgets = "<!-- SYSTEM_WIDGET" in answer_text
                if has_system_widgets and not accumulated:
                    print("[STATELESS] Final Answer с виджетами, но данных ещё нет. "
                          "Отклоняю — требую вызвать инструмент.")
                    # Добавляем в accumulated_data предупреждающий блок, чтобы
                    # следующая итерация знала, что нужна выборка данных
                    accumulated.append(("_system_notice", {},
                        "СИСТЕМНОЕ УВЕДОМЛЕНИЕ: в прошлой итерации модель попыталась "
                        "выдать Final Answer с виджетами, не вызвав ни одного "
                        "инструмента. Это запрещено. Нужно сначала вызвать инструмент."))
                    continue

                # Для каждого плейсхолдера SYSTEM_WIDGET
                # в Final Answer должен быть вызван питающий инструмент
                # (его данные лежат в data_cache). Если хотя бы одного нет -
                # отклоняем Final Answer и просим модель вызвать недостающий
                # инструмент. Это принудительная защита от срезания углов
                missing_widgets = self._check_widgets_data_presence(answer_text)
                if missing_widgets:
                    print(f"[STATELESS] Final Answer отклонён: для {len(missing_widgets)} "
                          f"виджет(ов) нет данных в кэше:")
                    needed_tools = set()
                    for w in missing_widgets:
                        print(f"  - intent='{w['intent']}' требует инструмент "
                              f"'{w['tool_name']}' (не вызывался)")
                        if w["tool_name"] and w["tool_name"] != "(не определён)":
                            needed_tools.add(w["tool_name"])
                    # Формируем понятное уведомление для модели
                    if needed_tools:
                        tools_list_str = ", ".join(f"`{t}`" for t in sorted(needed_tools))
                        notice = (
                            f"СИСТЕМНОЕ УВЕДОМЛЕНИЕ: ваш прошлый Final Answer отклонён. "
                            f"В нём были виджеты, для которых нет данных, потому что "
                            f"питающие их инструменты не были вызваны. "
                            f"НЕОБХОДИМО ВЫЗВАТЬ: {tools_list_str}. "
                            f"Составьте план, где первый шаг — вызов недостающего "
                            f"инструмента, и выполните его. После получения данных "
                            f"сформируйте Final Answer заново.")
                    else:
                        notice = (
                            "СИСТЕМНОЕ УВЕДОМЛЕНИЕ: ваш прошлый Final Answer отклонён. "
                            "В нём были виджеты с неизвестными источниками данных. "
                            "Уберите эти виджеты или вызовите корректные инструменты.")
                    accumulated.append(("_system_notice", {}, notice))
                    continue

                # Если модель решила, что план одношаговый
                # (Final Answer), крайне желательно сгенерировать виджеты.
                # Если виджетов нет, а в каталоге есть релевантные, то просим
                # модель добавить их в отдельном вызове
                if not ("<-- SYSTEM_WIDGET" in answer_text
                        or "```json" in answer_text
                        or "<!-- SYSTEM_WIDGET" in answer_text):
                    # Проверим, есть ли в выбранных плагинах хотя бы один виджет
                    has_any_widget = any(
                        skill.get("available_widgets")
                        for plugin in self.structure.plugins
                        if plugin["name"] in selected_plugins
                        for skill in plugin.get("skills", []))
                    if has_any_widget and not accumulated:
                        # Нет данных, нет виджетов, пусть попробует ещё раз
                        pass
                    # Если данные есть, но виджетов нет, добавим мягкое требование
                    # через дополнительный вызов (не блокируем)
                final_text = self._postprocess_final_answer(answer_text)
                print(f"Финальный ответ:\n{final_text}\n")
                return final_text

            # Модель хочет вызвать инструмент (первый шаг плана)
            if parsed["type"] == "action":
                proposed_tool = parsed["action"]
                try:
                    proposed_args = json.loads(parsed["action_input"]) if parsed["action_input"] else {}
                except json.JSONDecodeError:
                    proposed_args = {}

                # Анти-loop: если первый шаг - подряд-дубликат, - берём следующий
                # не-дубликат шаг из плана
                if is_consecutive_duplicate(proposed_tool, proposed_args, self._recent_calls_stack):
                    print(f"[ANTI-LOOP] Первый шаг плана '{proposed_tool}' — подряд-дубликат. "
                          "Ищу следующий не-дубликат шаг в плане.")
                    next_step = self._pick_first_non_duplicate_step(
                        plan_steps, fallback_action=(proposed_tool, proposed_args))
                    if next_step is None:
                        # Все шаги - дубликаты. Принудительно требуем финальный ответ
                        print("[ANTI-LOOP] Все шаги плана — подряд-дубликаты. "
                              "Принудительно требуем Final Answer.")
                        accumulated.append(("_system_notice", {},
                            "СИСТЕМНОЕ УВЕДОМЛЕНИЕ: модель пытается повторять "
                            "одни и те же вызовы. Все доступные данные уже собраны. "
                            "Сформируйте финальный ответ с виджетами на основе "
                            "имеющихся фактических данных."))
                        continue
                    proposed_tool, proposed_args = next_step
                    print(f"[ANTI-LOOP] Выполняю вместо первого шага: "
                          f"{proposed_tool} args={proposed_args}")

                # Маппинги skill→tool, plugin→tool
                tool = next((t for t in active_tools if t["name"] == proposed_tool), None)
                if not tool:
                    skill_to_tool = {}
                    plugin_to_tool = {}
                    for t in active_tools:
                        if t["plugin"] not in plugin_to_tool:
                            plugin_to_tool[t["plugin"]] = t["name"]
                    for plugin in self.structure.plugins:
                        if plugin["name"] in selected_plugins:
                            for skill in plugin.get("skills", []):
                                sn = skill.get("name")
                                if sn:
                                    fb = next((t["name"] for t in active_tools
                                               if t["plugin"] == plugin["name"]), None)
                                    if fb:
                                        skill_to_tool[sn] = fb
                    if proposed_tool in skill_to_tool:
                        proposed_tool = skill_to_tool[proposed_tool]
                        print(f"[CORRECTION] skill -> tool '{proposed_tool}'")
                    elif proposed_tool in plugin_to_tool:
                        proposed_tool = plugin_to_tool[proposed_tool]
                        print(f"[CORRECTION] plugin -> tool '{proposed_tool}'")
                    tool = next((t for t in active_tools if t["name"] == proposed_tool), None)
                if not tool:
                    from difflib import get_close_matches
                    matches = get_close_matches(
                        proposed_tool, [t["name"] for t in active_tools],
                        n=1, cutoff=0.7)
                    if matches:
                        proposed_tool = matches[0]
                        tool = next(t for t in active_tools if t["name"] == proposed_tool)
                        print(f"Автоисправление (fuzzy): -> '{proposed_tool}'")

                if not tool:
                    # Инструмент не найден - добавим уведомление в accumulated
                    available = ", ".join(t["name"] for t in active_tools)
                    accumulated.append(("_system_notice", {},
                        f"СИСТЕМНОЕ УВЕДОМЛЕНИЕ: инструмент '{proposed_tool}' не найден. "
                        f"Доступные: {available}."))
                    continue

                observation, raw_data = await self._execute_tool_call(proposed_tool, proposed_args)
                self._cache_tool_data(proposed_tool, raw_data)
                # Наращиваем accumulated (модель не видит имени инструмента,
                # только данные)
                accumulated.append((proposed_tool, proposed_args, observation))
                # Обновляем стек последних вызовов
                self._push_recent_call(proposed_tool, proposed_args)
                print(f"[STATELESS] Накоплено блоков данных: {len(accumulated)}")
                continue

            # LLM выдала Thought без Action/Final Answer, или
            # галлюцинировала Observation. Добавим уведомление в accumulated
            if parsed["type"] == "hallucinated_observation":
                accumulated.append(("_system_notice", {},
                    "СИСТЕМНОЕ УВЕДОМЛЕНИЕ: в прошлой итерации модель написала "
                    "'Observation:' без 'Action:'. Это нарушение формата. "
                    "Observation предоставляет ТОЛЬКО система. Начните с 'Thought:' "
                    "и вызовите инструмент через 'Action:'."))
                continue
            if parsed["type"] == "thought":
                accumulated.append(("_system_notice", {},
                    "СИСТЕМНОЕ УВЕДОМЛЕНИЕ: в прошлой итерации модель выдала "
                    "только Thought без Action или Final Answer. Сделайте выбор: "
                    "вызовите инструмент или выдайте финальный ответ."))
                continue

        return "Превышено количество итераций в stateless-режиме."

    async def _generate_final_answer_stateless(
        self,
        query: str,
        selected_plugins: List[str],
        active_tools: List[Dict[str, Any]],
        accumulated_data: str) -> Optional[str]:
        """ Дополнительный вызов LLM для генерации финального ответа с виджетами,
        когда план одношаговый (Final Answer), но LLM не выдала сам ответ """
        plugins_str = ", ".join(selected_plugins)
        widgets_catalog_parts: List[str] = []
        for plugin in self.structure.plugins:
            if plugin["name"] not in selected_plugins:
                continue
            for skill in plugin["skills"]:
                for widget_def in skill.get("available_widgets", []):
                    w_type = widget_def.get("type", "")
                    for intent in widget_def.get("intents", []):
                        intent_name = intent.get("name")
                        if intent_name:
                            widgets_catalog_parts.append(
                                f"##### Intent: {intent_name} (тип: {w_type})\n"
                                f"- Описание: {intent.get('description', '')}\n"
                                f"- Config (ДОСЛОВНО):\n"
                                f"{json.dumps(intent.get('config', {}), ensure_ascii=False, indent=2)}")
        widgets_catalog = "\n\n".join(widgets_catalog_parts) if widgets_catalog_parts else "(Виджетов нет)"

        prompt = f"""Вы — экспертный Помощник по Управлению Проектами (PMBOK 8).
Домены: {plugins_str}

## ФАКТИЧЕСКИЕ ДАННЫЕ КОМПАНИИ
По данным компании известно, что:

{accumulated_data}

## КАТАЛОГ ВИДЖЕТОВ (config — ДОСЛОВНО)
{widgets_catalog}

## ВОПРОС ПОЛЬЗОВАТЕЛЯ
{query}

## ИНСТРУКЦИЯ
Данных достаточно для ответа. Сформируйте финальный ответ.

ПРАВИЛО ФОРМИРОВАНИЯ ВИДЖЕТОВ (КРИТИЧНО):
- Вы можете вставлять в Final Answer только те виджеты, для которых
  в блоке ФАКТИЧЕСКИЕ ДАННЫЕ уже есть соответствующая таблица.
- Прежде чем вставить плейсхолдер `<!-- SYSTEM_WIDGET: ... -->`,
  проверьте: есть ли в ФАКТИЧЕСКИХ ДАННЫХ таблица с полями, которые
  требует этот виджет? Если нет — НЕ вставляйте его, выпустите ответ
  без этого виджета.
- Аналогично для `action_card`: данные для `message` берутся из
  ФАКТИЧЕСКИХ ДАННЫХ, никаких выдуманных чисел.

КРИТИЧЕСКИЕ ПРАВИЛА ВИДЖЕТОВ (из исходного решения):

**ВИДЖЕТ ТИП А: СИСТЕМНЫЕ ВИДЖЕТЫ** — вставляйте плейсхолдер:
<!-- SYSTEM_WIDGET: имя_intent | FILTER: {{"key": "value"}} | TITLE: Заголовок -->

**ВИДЖЕТ ТИП Б: МОДЕЛЬНЫЕ ВИДЖЕТЫ (action_card)** — генерируйте полный JSON,
обёрнутый в ```json ... ```:
```json
{{
  "widget_type": "action_card",
  "intent": "<имя_intent>",
  "title": "<заголовок>",
  "message": "<текст>",
  "button": {{"text": "...", "action": "...", "payload": {{...}}}}
}}
```

ОБЩИЕ ПРАВИЛА:
- Сначала текстовый ответ (анализ, конкретные выводы с именами/числами), затем виджеты.
- Имена сущностей — ТОЛЬКО из фактических данных.
- Не выдумывайте intents — берите из каталога выше.

**ЕСЛИ В КАТАЛОГЕ ЕСТЬ ХОТЯ БЫ ОДИН ВИДЖЕТ, РЕЛЕВАНТНЫЙ ВОПРОСУ — КРАЙНЕ ЖЕЛАТЕЛЬНО ЕГО СГЕНЕРИРОВАТЬ.**

Final Answer должен быть на ТОМ ЖЕ языке, что и вопрос пользователя.

Выдайте только Final Answer (без Plan и Thought).
"""
        try:
            llm_text = await self._call_llm([{"role": "user", "content": prompt}])
        except Exception as e:
            print(f"Ошибка LLM в _generate_final_answer_stateless: {e}")
            return None
        # Извлекаем Final Answer из ответа
        parsed = parse_llm_response(llm_text)
        answer_text = (parsed["answer"] if parsed["type"] == "final_answer"
            else llm_text.strip())
        # Убираем плейсхолдеры виджетов, для которых нет данных в кэше
        # (чтобы не вылезало «No cached data for tool X» в финальном ответе)
        answer_text = self._drop_unserviced_widget_placeholders(answer_text)
        return self._postprocess_final_answer(answer_text)

    def _drop_unserviced_widget_placeholders(self, final_answer: str) -> str:
        """ Удаляет плейсхолдеры <!-- SYSTEM_WIDGET --> для которых нет данных
        в data_cache. Возвращает текст с заменой таких плейсхолдеров на
        короткий комментарий для отладки (виден в raw_answer, не виден в UI) """
        registry = self._build_intent_registry()
        # Найдём все плейсхолдеры и отфильтруем проблемные
        pattern = re.compile(
            r"<!--\s*SYSTEM_WIDGET\s*:\s*(?P<intent>[^|]+?)\s*\|\s*"
            r"FILTER:\s*(?P<filter>[^|]+?)\s*\|\s*"
            r"TITLE:\s*(?P<title>.*?)\s*-->",
            re.DOTALL)

        def _replace(m: re.Match) -> str:
            intent_name = m.group("intent").strip()
            intent_name = intent_name.replace("_ ", "_").replace(" _", "_")
            if intent_name not in registry:
                from difflib import get_close_matches
                matches = get_close_matches(
                    intent_name, list(registry.keys()), n=1, cutoff=0.7)
                if matches:
                    intent_name = matches[0]
                else:
                    # Неизвестный intent, оставим, рендер сам сообщит
                    return m.group(0)  
            reg = registry[intent_name]
            if reg["widget_type"] in ("action_card", "ActionCard"):
                # Особый виджет, не трогаем
                return m.group(0)  
            tool_name = reg["tool_name"]
            if not tool_name or tool_name not in self.data_cache:
                print(f"[STATELESS] Удаляю плейсхолдер виджета '{intent_name}': "
                      f"нет данных от инструмента '{tool_name}'")
                return f"<!-- Widget '{intent_name}' опущен: нет данных от инструмента '{tool_name}' -->"
            return m.group(0)

        return pattern.sub(_replace, final_answer)

async def main():
    agent = PmIqAgent()
    try:
        await agent.initialize()
        print(f"\n[CONFIG] LOOP_MODE = {LOOP_MODE!r}\n")
        queries = [
            "Проанализируй загрузку команды разработки, ответь с диаграммой."
        ]
        for q in queries:
            await agent.run(q)
            print("\n" + "=" * 80 + "\n")
    finally:
        print("\nЗавершение работы и очистка ресурсов...")
        await agent.close()

if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(main())