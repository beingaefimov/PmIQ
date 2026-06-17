""" Агент с этапом концентрации знаний.
Пайплайн:
    Фаза А - route_query: LLM выбирает 1-3 релевантных плагина
    Фаза А.5 - Skill concentrate: сжимает методичку под вопрос
        и формирует самодостаточный план + planned_widgets
    Фаза Б - ReAct-цикл: Thought -> Action -> Observation -> ... -> Final Answer
        где промпт строится из концентрата, а не из полной методички """

import asyncio
import json
import re
import sys
from pathlib import Path
from typing import List, Dict, Any
import yaml
import fnmatch
from widget_renderer import render_widget
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from openai import OpenAI

from concentrator import (
    SkillConcentrator,
    ConcentratedContext,
    is_special_widget,
    SPECIAL_WIDGET_TYPES)

PM_IQ_ROOT = Path(__file__).parent.parent

# Настройки LLM
LLM_BASE_URL = "http://localhost:3101/v1"
LLM_MODEL_NAME = "local-model"
_MAX_TOKENS = 8096
_TEMPERATURE = 0.1

# Парсер структуры плагинов/скиллов
class PmIqStructureParser:
    def __init__(self, root_path: Path):
        self.root_path = root_path
        self.plugins_dir = root_path / "plugins"
        self.plugins: List[Dict[str, Any]] = []

    def _parse_skill(self, skill_dir: Path) -> Dict[str, Any]:
        skill_file = skill_dir / "SKILL.md"
        if not skill_file.exists():
            return None
        with open(skill_file, 'r', encoding='utf-8') as f:
            content = f.read()
        if not content.startswith('---'):
            return None
        parts = content.split('---', 2)
        if len(parts) < 3:
            return None
        frontmatter = yaml.safe_load(parts[1]) or {}
        # Отдаём модели весь файл, включая YAML-описание виджетов
        full_instructions = f"{parts[1].strip()}\n---\n{parts[2].strip()}"
        return { "name": frontmatter.get("name", skill_dir.name),
            "description": frontmatter.get("description", ""),
            "instructions": full_instructions,
            "path": skill_dir,
            "available_widgets": frontmatter.get("available_widgets", [])}

    def _parse_plugin(self, plugin_dir: Path) -> Dict[str, Any]:
        plugin_info = {"name": plugin_dir.name,
            "path": plugin_dir,
            "skills": [],
            "mcp_config": None}
        mcp_config_file = plugin_dir / ".mcp.json"
        if mcp_config_file.exists():
            with open(mcp_config_file, 'r', encoding='utf-8') as f:
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
            if not plugin_dir.is_dir() or plugin_dir.name.startswith('.'):
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
        # Кэш сырых данных { "tool_name": [список словарей] }
        self.data_cache: Dict[str, List[Dict[str, str]]] = {}
        # История вызовов инструментов для защиты от дубль-вызовов
        # { (tool_name, json_args): observation_text }
        self._tool_call_history: Dict[tuple, str] = {}
        # Счётчик вызовов: { (tool_name, json_args): count }
        self._tool_call_counts: Dict[tuple, int] = {}
        # Концентратор знаний
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

    def _expand_widget_descriptors(self, final_answer: str) -> str:
        """ Постобработка виджетов после получения Final Answer от LLM.
        Ищет JSON-блоки с полем "widget_type" двумя способами:
          1. Fenced: ```json ... ``` (стандартный формат)
          2. Bare: { ... "widget_type": ... ... } тк LLM иногда забывает ограждения.
        Каждый найденный дескриптор прогоняется через render_widget и заменяется
        на отрендеренный envelope в ```json ... ``` формате """

        def fix_underscore_spaces(m):
            """ LLM бывает вставляет пробелы в ключи: например "data_ rows" чистим в "data_rows" """
            inner = m.group(1)
            fixed = re.sub(r'_\s+', '_', inner)
            return f'"{fixed}"'

        def process_raw_json(raw: str) -> str | None:
            """ Возвращает отрендеренный envelope (в ```json ... ```) или None """
            raw = raw.strip()
            raw = re.sub(r'"([^"]*)"', fix_underscore_spaces, raw)
            try:
                descriptor = json.loads(raw)
            except json.JSONDecodeError as e:
                print(f"[WidgetRenderer] JSONDecodeError: {e} | raw[:200]: {raw[:200]}")
                return None
            if "widget_type" not in descriptor:
                return None
            # Если это уже отрендеренный бэкендом график (echarts), не трогаем
            if descriptor.get("widget_type") == "echarts":
                return None
            echarts_envelope = render_widget(descriptor)
            if echarts_envelope is None:
                print(f"[WidgetRenderer] render_widget=None для intent={descriptor.get('intent')}")
                return None
            expanded = json.dumps(echarts_envelope, ensure_ascii=False, indent=2)
            print(f"[WidgetRenderer] OK: {descriptor.get('widget_type')}/{descriptor.get('intent')} -> {len(expanded)} chars")
            return f"```json\n{expanded}\n```"

        # Обрабатываем fenced-блоки (```json ... ```)
        json_block_re = re.compile(r"```json\s*([\s\S]*?)```", re.MULTILINE)
        def replace_fenced(match: re.Match) -> str:
            rendered = process_raw_json(match.group(1))
            return rendered if rendered is not None else match.group(0)
        result = json_block_re.sub(replace_fenced, final_answer)
        # обрабатываем bare JSON-блоки с "widget_type"
        # LLM иногда не оборачивает JSON в ограждения. Ищем сбалансированные
        # {...} блоки, содержащие "widget_type", и обрабатываем их.
        # Пропускаем блоки внутри ``` ... ``` fences (они уже обработаны
        # ранее или это другой код - не трогаем).
        bare_blocks: list[tuple[int, int, str]] = []  # (start, end, raw_json)
        # Сначала находим все span-ы code fences (начало/конец).
        # \n? - опциональный, т.к. последний ``` может быть в конце текста без \n
        fence_re = re.compile(r"```[a-zA-Z0-9]*\n?")
        fence_spans: list[tuple[int, int]] = []
        fence_starts = [m.start() for m in fence_re.finditer(result)]
        for k in range(0, len(fence_starts) - 1, 2):
            open_start = fence_starts[k]
            close_start = fence_starts[k + 1]
            # span covers from ```json\n ... up to start of closing ```
            fence_spans.append((open_start, close_start))

        def in_fence(pos: int) -> bool:
            for fs, fe in fence_spans:
                if fs <= pos < fe:
                    return True
            return False

        i = 0
        text = result
        while i < len(text):
            ch = text[i]
            if ch != "{":
                i += 1
                continue
            # Пропускаем, если курсор внутри code fence
            if in_fence(i):
                i += 1
                continue
            # Пробуем найти сбалансированный {...} начиная с i
            depth = 0
            in_string = False
            escape = False
            j = i
            while j < len(text):
                c = text[j]
                if escape:
                    escape = False
                    j += 1
                    continue
                if c == "\\":
                    escape = True
                    j += 1
                    continue
                if c == '"':
                    in_string = not in_string
                    j += 1
                    continue
                if in_string:
                    j += 1
                    continue
                if c == "{":
                    depth += 1
                elif c == "}":
                    depth -= 1
                    if depth == 0:
                        # Нашли сбалансированный блок
                        raw = text[i:j + 1]
                        # Проверяем, что это виджет (содержит "widget_type")
                        if '"widget_type"' in raw or '"widget_type ' in raw:
                            bare_blocks.append((i, j + 1, raw))
                        i = j + 1
                        break
                j += 1
            else:
                # Не сбалансировано - выходим
                break
            if depth != 0:
                break
        # Заменяем bare-блоки справа налево, чтобы не сбить индексы
        for start, end, raw in reversed(bare_blocks):
            rendered = process_raw_json(raw)
            if rendered is not None:
                result = result[:start] + rendered + result[end:]
        return result

    def _clean_broken_json(self, text: str) -> str:
        """ Удаляет оборванные ```json блоки, если LLM уперлась в max_tokens """
        cleaned = re.sub(r"```json\s*[\s\S]*?(?=```)", lambda m: m.group(0) if text.count("```") % 2 == 0 else "", text)
        cleaned = re.sub(r"```json\s*[\s\S]*?(?<!```)$", "", text)
        return cleaned.strip()

    # MCP-подключения
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

    # Фаза А - Маршрутизация
    async def route_query(self, query: str) -> List[str]:
        """ Архитектура с мульти-плагиновой маршрутизацией (Уровень 1).
        При проектировании агентных систем с большим количеством инструментов (30+) 
        существует дилемма: "Полнота контекста" vs "Перегрузка контекста (Context Bloat)".
        Известные подходы:
        Уровень 1: Мульти-плагиновый роутер (текущая реализация)
        - Как работает: LLM сначала анализирует запрос и выбирает 1-3 релевантных плагина. 
        Затем в ReAct-промт загружаются инструменты и инструкции только этих плагинов.
        - Плюсы: Резкая экономия токенов (вместо 30 инструментов в промпте попадает 4-8), 
        высокая надежность на локальных моделях (7B-8B), поддержка кросс-доменных запросов 
        (например, "задержка и бюджет" выберет 2 плагина).
        - Минусы: Зависит от способности LLM корректно классифицировать запрос по доменам.
        - Условия применения: Локальные LLM, < 50 инструментов, чёткие границы доменов.
        Уровкнь 1 bis: Добавить 1.2 уровень роутинга - после выбора плагина, LLM выбирает конкретный скилл внутри него.
        Но только введя арбитра, оценщика выбранного скиллса и если выбор одного не очевиден (ясно, что он покрывает вопрос),
        то оставлять все.
        Уровень 2: Семантический поиск инструментов "RAG for Tools"
        - Как работает: Описания всех инструментов векторизуются. При запросе 
        выполняется семантический поиск (cosine similarity), и в промпт попадают топ(5) 
        наиболее релевантных инструментов, независимо от того, в каком они плагине.
        - Плюсы: Бесконечная масштабируемость (1000+ инструментов), не зависит от жестких 
        границ плагинов, находит инструменты по смыслу, а не по названию категории.
        - Минусы: Требует локальной модели эмбеддингов, векторного хранилища (QDrant/Chroma/FAISS), 
        усложняет архитектуру.
        - Условия применения: Продакшен-системы с > 50 инструментами, сильно пересекающимися доменами.
        Уровень 3: Иерархия суб-агентов
        - Как работает: Главный агент-диспетчер разбивает сложный запрос на подзадачи и 
        делегирует их специализированным суб-агентам (например, "Агент расписания" и "Агент рисков"), 
        которые общаются друг с другом.
        - Плюсы: Глубочайшее рассуждение, автономность, решение сверхсложных задач.
        - Минусы: Экспоненциальный рост затрат токенов, высокая задержка (latency), высокий 
        риск зацикливания или потери контекста.
        - Условия применения: Мощные, желательно естественные, модели (GPT-4o, Claude 3.5 Sonnet), сложные 
        многоэтапные автономные workflow"""
        plugins_summary = []
        for p in self.structure.plugins:
            skills_desc = "\n    ".join([f"- {s['name']}: {s['description']}" for s in p['skills']])
            plugins_summary.append(f"### {p['name']}\n    {skills_desc}")
        prompt = f"""Вы — маршрутизатор запросов по управлению проектами.
Проанализируйте запрос пользователя и выберите от 1 до 3 НАИБОЛЕЕ РЕЛЕВАНТНЫХ доменов-плагинов.

КРИТИЧЕСКОЕ УКАЗАНИЕ:
- Если запрос упоминает НЕСКОЛЬКО аспектов (например, задержки И бюджет, расписание И риски), вы ОБЯЗАНЫ выбрать ВСЕ релевантные плагины, а не только один.
- Внимательно читайте описания скиллов. Каждое описание объясняет, какие типы запросов обрабатывает скилл.
- Сопоставляйте интент пользователя с теми скиллами, чьи описания покрывают релевантные аспекты.

Доступные домены со скиллами:
{chr(10).join(plugins_summary)}

Запрос пользователя: {query}

Ответьте ТОЛЬКО валидным JSON-массивом имён плагинов.
Пример: ["pm-project-core", "pm-value-and-performance"]
Не добавляйте markdown-форматирование и пояснения."""
        loop = asyncio.get_event_loop()

        def sync_llm_call():
            return self.llm_client.chat.completions.create(
                model=LLM_MODEL_NAME,
                messages=[{"role": "user", "content": prompt}],
                temperature=_TEMPERATURE)

        response = await loop.run_in_executor(None, sync_llm_call)
        raw_response = response.choices[0].message.content.strip()

        print(f"{'='*80}")
        print(f"СЫРОЙ ОТВЕТ ОТ LLM (роутер):")
        print(f"{'='*80}")
        print(raw_response)
        print(f"{'='*80}\n")

        raw_response = raw_response.replace('```json', '').replace('```', '').strip()
        try:
            selected_plugins = json.loads(raw_response)
            if isinstance(selected_plugins, str):
                selected_plugins = [selected_plugins]
            # Иногда модель может вернуть имя скилла вместо плагина, проверяем
            valid_plugins = []
            for item in selected_plugins:
                if any(plug["name"] == item for plug in self.structure.plugins):
                    valid_plugins.append(item)
                else:
                    for plug in self.structure.plugins:
                        if any(skill["name"] == item for skill in plug["skills"]):
                            valid_plugins.append(plug["name"])
                            print(f"LLM вернула имя скилла '{item}', маппим на плагин '{plug['name']}'")
                            break
            valid_plugins = list(dict.fromkeys(valid_plugins))
            if not valid_plugins:
                valid_plugins = ["pm-project-core"]
            print(f"Маршрутизация: выбраны плагины {valid_plugins}")
            return valid_plugins
        except json.JSONDecodeError:
            print(f"Ошибка парсинга JSON от роутера. Ответ: {raw_response}. Fallback.")
            return ["pm-project-core"]

    # Фаза Б - ReAct-промпт
    def _build_react_prompt(
        self,
        query: str,
        active_tools: List[Dict[str, Any]],
        active_plugin_names: List[str],
        ctx: ConcentratedContext) -> str:
        """ ReAct-промпт на основе концентрированного контекста.
        Список active_tools больше выводится в промпт здесь.
        LLM получает инструменты и правила их применения только через
        концентрат (ctx.concentrated_knowledge + ctx.plan + ctx.planned_tools) """
        plugins_str = ", ".join(active_plugin_names)
        # Секция запланированных виджетов: системные и модельные
        system_widgets, model_widgets = SkillConcentrator.split_widgets_by_type(
            ctx.planned_widgets)
        # Системные виджеты (BarChart/LineChart/ScatterChart и тд) -> placeholder
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
                "выводы с именами/числами из Observation), затем — плейсхолдер виджета. "
                "Пример:\n"
                "  Final Answer: По данным о загрузке, взять новую задачу без перегрузки "
                "могут: Sidorov B. (доступная мощность 20%) и Frolov B. (20%). "
                "Остальные перегружены (Simonova A. -20%, Team_1 -10%).\n"
                "  <!-- SYSTEM_WIDGET: availability_gap | FILTER: {} | TITLE: Доступная мощность по ресурсам -->\n\n"
                + "\n\n".join(sys_lines))
        else:
            system_widgets_section = (
                "(Нет запланированных системных виджетов. Если нужен график — "
                "выберите intent из реестра и вставьте плейсхолдер.)")
        # Модельные виджеты (action_card и др.) -> полный JSON в Final Answer
        # Разбиваем на REQUIRED (LLM-концентратор выбрала) и AVAILABLE (force-included)
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
                    # config пустой - это fallback-режим, полный YAML в skills_content
                    config_line = (
                        "- Config: см. в ЗНАНИЯХ выше "
                        "(YAML-блок available_widgets скилла "
                        f"{skill}). Найдите по имени intent '{w.get('intent', '')}'.")
                return (
                    f"##### Intent: {w['intent']} (тип: {w['widget_type']})\n"
                    f"- Описание (для чего): {desc}\n"
                    f"- Из скилла: {skill}\n"
                    f"- Предлагаемый заголовок: {w.get('title_hint', '')}\n"
                    f"- Обоснование: {w.get('rationale', '')}\n"
                    f"{config_line}")

            parts = []
            parts.append(
                "Эти виджеты ТРЕБУЮТ LLM-генерации содержимого (message, button и прочее согласно описания).\n"
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
                "   затем — JSON-блоки виджетов. Пример:\n"
                "   Final Answer: Найдены 2 активных риска с высоким воздействием:\n"
                "   - R-1: Server delivery delay (risk_score=9, владелец Hubbin I.I.)...\n"
                "   - R-2: Staff shortage (risk_score=9, владелец Sergo P.P.)...\n"
                "   ```json\n{ ... action_card для R-1 ... }\n```\n"
                "   ```json\n{ ... action_card для R-2 ... }\n```\n"
                "3. ПОЛЕ message ДОЛЖНО БЫТЬ УНИКАЛЬНЫМ для каждого виджета.\n"
                "   НЕ копируйте message из одного виджета в другой. Каждый action_card\n"
                "   описывает КОНКРЕТНЫЙ объект — text должен отражать именно его.\n"
                "   Например, для риска 'Staff shortage' message про 'резервного поставщика'\n"
                "   БЕССМЫСЛЕНЕН — нужно писать про резервный персонал/найм."
            )

            # REQUIRED - LLM-концентратор сочла релевантными вопросу
            if required_mw:
                req_lines = [render_widget_block(w) for w in required_mw]
                parts.append(
                    "### ОБЯЗАТЕЛЬНО СГЕНЕРИРОВАТЬ\n"
                    "Сгенерируйте ВСЕ эти виджеты в Final Answer:\n\n"
                    + "\n\n".join(req_lines))

            # AVAILABLE - force-included, LLM решает по description
            if available_mw:
                avail_lines = [render_widget_block(w) for w in available_mw]
                parts.append(
                    "### ДОСТУПНО (сгенерируйте если релевантные вопросу)\n"
                    "Прочитайте Описание каждого и решите, какие релевантны вопросу.\n"
                    "Сгенерируйте в Final Answer только те, что реально подходят.\n"
                    "Если вопрос требует плана действий/реагирования — сгенерируйте\n"
                    "хотя бы один подходящий action_card.\n\n"
                    + "\n\n".join(avail_lines))

            model_widgets_section = "\n\n".join(parts)
        else:
            model_widgets_section = "(Нет запланированных модельных виджетов.)"

        plan_section = ctx.plan if ctx.plan else "(Предплана нет. Анализируйте с нуля.)"

        # Подсказка по запланированным инструментам
        planned_tools_hint = ""
        if ctx.planned_tools:
            planned_tools_hint = (
                f"\nПРЕДЛАГАЕМЫЙ ПОРЯДОК ИНСТРУМЕНТОВ (отклоняйтесь только с обоснованием в Thought): "
                f"{' -> '.join(ctx.planned_tools)}")

        knowledge_section = ctx.concentrated_knowledge if ctx.concentrated_knowledge else (
            "(Концентрированных знаний нет. Опирайтесь на инструменты и общие принципы PMBOK 8.)")

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

1. **ЦЕЛОСТНОСТЬ ДАННЫХ:**
   - Observation содержит JSON с основным полем `data` — это Markdown-таблица с фактической информацией.
   - Используйте ТОЛЬКО данные из поля `data`. НИКОГДА не выдумывайте имена, числа или факты.
   - **МАППИНГ СУЩНОСТЕЙ:** пользователи называют сущности разговорно ("Перенос 1С", "Стройка"). Сопоставляйте их с точными системными именами из `project_name`, `wbs_name`, `resource_name`.
   - В Final Answer и в `data_rows` виджетов ВСЕГДА используйте ТОЛЬКО точные системные имена из таблицы `data`.
   - Парсите Markdown-таблицу внимательно: каждая строка после разделителя — отдельная запись.
   - **ИМЕНА ИНСТРУМЕНТОВ:** вызывайте ТОЛЬКО инструменты, упомянутые в ЗНАНИЯХ, ПЛАНЕ или ПРЕДЛАГАЕМОМ ПОРЯДКЕ ИНСТРУМЕНТОВ выше. НИКОГДА не используйте имена навыков (Skills) в качестве Action. НИКОГДА не выдумывайте инструменты — если имя не упомянуто в плане/концентрате, его нельзя вызывать.
   - **СИНТАКСИС ФИЛЬТРОВ:** в плейсхолдере `FILTER: {{}}` передавайте ТОЛЬКО простые значения (строки) или массивы строк `["A", "B"]`. ЗАПРЕЩЕНО `{{"$regex": "..."}}` или `{{"$gt": 10}}`.

2. **ОТОБРАЖЕНИЕ ВИДЖЕТОВ (КРИТИЧНО — ПРОЧТИ ДВАЖДЫ):**

   **ВИДЖЕТЫ — ЭТО НЕ ИНСТРУМЕНТЫ. Их нельзя вызывать через `Action:`.**
   Плейсхолдеры `<!-- SYSTEM_WIDGET -->` и JSON-блоки виджетов появляются **ТОЛЬКО ВНУТРИ Final Answer** — прямо в тексте ответа пользователя.

   **ВИДЖЕТ ТИП А: СИСТЕМНЫЕ ВИДЖЕТЫ (LineChart, BarChart, ScatterChart и т.д.)**
   Эти виджеты отображают данные из таблиц `data`. НЕ генерируйте для них JSON с data_rows!
   1. ВЫБЕРИТЕ один intent из запланированных выше (или из реестра, если план неполный).
   2. Вставьте ОДИН плейсхолдер **прямо в текст Final Answer** строго в формате:
      <!-- SYSTEM_WIDGET: имя_intent | FILTER: {{"ключ_из_data": "значение"}} | TITLE: Ваш контекстный заголовок -->
   3. В FILTER передавайте JSON с параметрами для фильтрации строк из таблицы. Пустой фильтр `FILTER: {{}}` = все строки.

   **ВИДЖЕТ ТИП Б: ВИДЖЕТЫ МОДЕЛИ (action_card и кастомные)**
   1. Для `action_card` генерируйте ПОЛНЫЙ JSON-блок **внутри Final Answer**. Поле "data_rows" = [].
   2. Если сами рассчитали данные, можно сгенерировать виджет ПОЛНОСТЬЮ. `widget_type` = тип диаграммы (не имя intent).

   **ОБЩИЕ ПРАВИЛА:**
   - ЗАПРЕЩЕНО генерировать виджеты "по памяти" или придумывать названия intent-ов.
   - Если запрос затрагивает несколько аспектов, включайте ВСЕ релевантные виджеты в Final Answer.
   - ЗАПРЕЩЕНО писать `Action: <!-- SYSTEM_WIDGET ... -->`. Это частая ошибка. Виджет — НЕ инструмент.

3. **ПРОВЕРКА ФАКТОВ:**
   - Перед Final Answer проверьте каждое имя и число по полю `data` в Observation.
   - Если имени НЕТ в `data`, НЕ упоминайте его в ответе.

4. **ЛОГИКА РАБОТЫ (ИЗБЕГАТЬ ЗАЦИКЛИВАНИЯ — КРИТИЧНО):**
   Вы работаете в цикле: Thought -> Action -> Observation -> ... -> Final Answer
   - В КАЖДОМ ответе ТОЛЬКО ОДИН блок: либо `Thought + Action + Action Input`, либо `Thought + Final Answer`.
   - **НИКОГДА не пишите несколько Action в одном ответе.** Вызывайте инструменты ПО ОДНОМУ.
   - НЕ ПИШИТЕ "Question:" в начале ответа.
   - **СТРОЖАЙШЕ ЗАПРЕЩЕНО писать "Observation:" самому.** Observation предоставляет ТОЛЬКО система после выполнения Action. ВАШ ответ ВСЕГДА должен начинаться с "Thought:" и заканчиваться либо "Action:", либо "Final Answer:".
   - После Observation (от системы):
     * данных НЕДОСТАТОЧНО -> новый Thought + **ДРУГОЙ** инструмент (не тот же самый!)
     * данных ДОСТАТОЧНО -> `Thought: я теперь знаю финальный ответ` + Final Answer
   - **ЗАПРЕЩЕНО вызывать один инструмент с теми же аргументами более одного раза подряд.**
     Если вы уже получили данные от `calculate_evm {{}}`, НЕ ВЫЗЫВАЙТЕ `calculate_evm {{}}` снова —
     данные не изменятся. Переходите к СЛЕДУЮЩЕМУ инструменту из плана или к Final Answer.
   - **ПРИМЕР правильного перехода** (после получения Observation от calculate_evm):
     ```
     Thought: Из данных видно: 1C_Migration имеет CPI < 1.0 в Months 2-4 (0.88, 0.86, 0.86) —
     3 периода подряд = системная проблема. Pool_Phase1: CPI 1.04 -> 0.96 -> 0.95 —
     тренд к ухудшению. Local_App: CPI > 1.1 — эффективно. Теперь нужен EAC прогноз —
     вызову get_okr_alignment.
     Action: get_okr_alignment
     Action Input: {{}}
     ```
   - **НЕПРАВИЛЬНО** (зацикливание):
     ```
     Thought: Теперь проверю наличие трёх подряд периодов с CPI < 1.0...
     Action: calculate_evm       <- ЗАПРЕЩЕНО, уже вызывался!
     Action Input: {{}}
     ```

5. **ОТКЛОНЕНИЕ ОТ ПЛАНА:**
   - План выше — это рекомендация. Если Observation противоречит плану, ОТКЛОНЯЙТЕСЬ, но объясните в Thought.
   - Если запланированный виджет оказывается нерелевантным после получения данных, не выводите его.

6. **МНОЖЕСТВЕННЫЕ АСПЕКТЫ:**
   - Если запрос упоминает НЕСКОЛЬКО аспектов, вызовите инструменты из ВСЕХ релевантных доменов.

7. **ВЫХОД ИЗ ТУПИКА:**
   - Если инструмент не найден или Observation содержит "error", сразу выдавайте Final Answer с объяснением.

8. **ПРАВИЛЬНЫЙ ПЕРЕХОД К FINAL ANSWER С ВИДЖЕТОМ (пример):**

   После получения Observation:
   ```
   Thought: Я получил все нужные данные о загрузке. Могу ответить на вопрос с диаграммой.
   Final Answer: Перегружен сотрудник: Frolov A. (120%). Он — Middle Dev. Остальные — в норме.
   <!-- SYSTEM_WIDGET: overload_by_person | FILTER: {{}} | TITLE: Загрузка команды разработки -->
   ```

   **НЕПРАВИЛЬНО (виджет как Action — ЗАПРЕЩЕНО):**
   ```
   Thought: ...
   Action: <!-- SYSTEM_WIDGET: overload_by_person | FILTER: {{}} | TITLE: ... -->
   ```

**СТРОГОЕ ПРАВИЛО ЯЗЫКА:**
В шагах 'Thought:' разрешается думать на английском.
Однако 'Final Answer:' ДОЛЖЕН быть на ТОМ ЖЕ языке, на котором задан вопрос пользователя.

Начните!

Вопрос пользователя: {query}"""

    # Парсер ответа LLM (ReAct)
    def _parse_llm_response(self, text: str) -> Dict[str, str]:
        """ Парсит ответ LLM без использования регулярок, на базе стейт-машины """
        lines = text.split('\n')
        if lines and lines[0].strip().lower().startswith('question:'):
            lines = lines[1:]
            text = '\n'.join(lines)
        thought_parts = []
        action = ""
        action_input_parts = []
        state = 'none'
        for line in lines:
            stripped = line.strip()
            if not stripped and state != 'action_input':
                continue
            lower = stripped.lower()
            # FINAL ANSWER - абсолютный приоритет
            if lower.startswith("final answer:") or lower.startswith("final answer :"):
                idx = text.lower().find("final answer")
                if idx != -1:
                    colon_idx = text.find(":", idx)
                    answer_text = text[colon_idx + 1:].strip() if colon_idx != -1 else text[idx + len("final answer"):].strip()
                else:
                    answer_text = stripped
                return {"type": "final_answer", "answer": answer_text}
            if state == 'action_input':
                if lower.startswith("action:") or lower.startswith("action input:"):
                    continue
                action_input_parts.append(stripped)
                continue
            if lower.startswith("observation:"):
                if action:
                    return {"type": "action",
                            "thought": "\n".join(thought_parts).strip(),
                            "action": action.strip(),
                            "action_input": "\n".join(action_input_parts).strip()}
                else:
                    # LLM сгаллюцинировала Observation без Action - это нарушение ReAct.
                    # Возвращаем отдельный тип, чтобы run() мог вставить user-сообщение
                    return {"type": "hallucinated_observation",
                        "text": text,}
            if lower.startswith("action input:") or lower.startswith("action input :"):
                state = 'action_input'
                parts = stripped.split(":", 1)
                if len(parts) > 1 and parts[1].strip():
                    action_input_parts.append(parts[1].strip())
                continue
            if lower.startswith("action:") or lower.startswith("action :") or lower == "action":
                if state != 'action':
                    state = 'action'
                    parts = stripped.split(":", 1)
                    if len(parts) > 1 and parts[1].strip():
                        candidate = parts[1].strip()
                        # ДЕТЕКЦИЯ ПЛЕЙСХОЛДЕРА-В-ACTION:
                        # LLM иногда путается и пишет виджет как Action.
                        # Это значит, что она хотела выдать Final Answer.
                        # Конвертируем весь накопленный Thought + сам плейсхолдер
                        # в Final Answer, чтобы пройти к рендерингу виджетов
                        if (candidate.startswith("<!--")
                                or candidate.startswith("SYSTEM_WIDGET")
                                or "<!-- SYSTEM_WIDGET" in text):
                            print("[PARSER] Action содержит <!-- SYSTEM_WIDGET — "
                                  "конвертирую в Final Answer")
                            # Собираем Final Answer из Thought + всего текста Action
                            fa_parts = []
                            if thought_parts:
                                fa_parts.append("\n".join(thought_parts).strip())
                            fa_parts.append(candidate)
                            # Если ниже есть ещё строки (Action Input и т.д.) -
                            # они уже в тексте, найдём их через срез текста
                            full_action_text = text[text.find("Action:"):]
                            # Если в полном тексте ответа есть SYSTEM_WIDGET,
                            # возьмём его целиком - там могут быть плейсхолдеры
                            if "<!-- SYSTEM_WIDGET" in full_action_text:
                                # Берём от первого Thought до конца
                                thought_idx = text.lower().find("thought:")
                                if thought_idx != -1:
                                    raw_answer = text[thought_idx:].strip()
                                    # Вычищаем служебные префиксы ReAct:
                                    # "Thought: ...", "Action: ...", "Action Input: ..."
                                    # Оставляем только содержательные даанные
                                    cleaned_lines = []
                                    for line in raw_answer.split("\n"):
                                        ls = line.strip()
                                        ll = ls.lower()
                                        if ll.startswith("thought:"):
                                            content = ls[len("thought:"):].strip()
                                            if content:
                                                cleaned_lines.append(content)
                                        elif ll.startswith("action:"):
                                            content = ls[len("action:"):].strip()
                                            if content:
                                                cleaned_lines.append(content)
                                        elif ll.startswith("action input:"):
                                            # Пропускаем, это служебное
                                            continue
                                        else:
                                            cleaned_lines.append(line)
                                    cleaned_answer = "\n".join(cleaned_lines).strip()
                                    return {"type": "final_answer",
                                        "answer": cleaned_answer}
                            return {"type": "final_answer",
                                "answer": "\n\n".join(fa_parts).strip()}
                        action = candidate.split()[0]
                continue
            if lower.startswith("thought:") or lower.startswith("thought :") or lower == "thought":
                state = 'thought'
                parts = stripped.split(":", 1)
                if len(parts) > 1 and parts[1].strip():
                    thought_parts.append(parts[1].strip())
                continue
            if state == 'thought':
                thought_parts.append(stripped)
            elif state == 'action':
                if not action:
                    action = stripped.split()[0]
            elif state == 'action_input':
                action_input_parts.append(stripped)
        if action:
            return {"type": "action",
                    "thought": "\n".join(thought_parts).strip(),
                    "action": action.strip(),
                    "action_input": "\n".join(action_input_parts).strip()}
        return {"type": "thought", "text": text}

    def _detect_loop(self, history: List[Dict], threshold: int = 3) -> bool:
        """ Обнаруживает зацикливание ReAct. Два критерия:
        1. Последние N Thought-сообщений похожи (старый критерий, первые 100 символов).
        2. Последние N assistant-сообщений содержат ОДИНАКОВЫЙ Action + Action Input """
        if len(history) < threshold * 2:
            return False
        recent_assistant_msgs = [
            msg["content"] for msg in history[-threshold*2:]
            if msg["role"] == "assistant"][-threshold:]
        if len(recent_assistant_msgs) < threshold:
            return False
        # Критерий 1: похожие первые 100 символов 
        first_msg_prefix = recent_assistant_msgs[0][:100]
        similar_count = sum(1 for msg in recent_assistant_msgs if msg[:100] == first_msg_prefix)
        if similar_count >= threshold:
            return True
        # Критерий 2: одинаковый Action + Action Input в последних N сообщениях
        def extract_action_sig(msg: str) -> str:
            """ Извлекает 'Action: X | Action Input: Y' из сообщения """
            action = ""
            action_input = ""
            for line in msg.split("\n"):
                ls = line.strip().lower()
                if ls.startswith("action:") and not action:
                    parts = line.strip().split(":", 1)
                    if len(parts) > 1:
                        action = parts[1].strip().split()[0] if parts[1].strip() else ""
                elif ls.startswith("action input:") and not action_input:
                    parts = line.strip().split(":", 1)
                    if len(parts) > 1:
                        action_input = parts[1].strip()
            return f"{action}||{action_input}"

        sigs = [extract_action_sig(msg) for msg in recent_assistant_msgs]
        # Если все сигнатуры непустые и одинаковые, то это зацикливание
        if all(s for s in sigs) and len(set(sigs)) == 1:
            print(f"[LOOP-DETECT] Обнаружено зацикливание на Action: {sigs[0]}")
            return True

        return False

    def _detect_hallucinations(self, final_answer: str, observations: List[str]) -> List[str]:
        """ Это очень наивная версия, суть заглушка.
        Сейчас - проверяет, не выдумала ли LLM имена, которых нет в данных """
        mentioned_names = set(re.findall(r'\b([А-ЯA-Z][а-яa-z]+\s+[А-ЯA-Z]\.)\b', final_answer))
        data_names = set()
        for obs in observations:
            data_names.update(re.findall(r'\b([А-ЯA-Z][а-яa-z]+\s+[А-ЯA-Z]\.)\b', obs))
        hallucinated = mentioned_names - data_names
        return list(hallucinated)

    # Рендер системных плейсхолдеров
    def _render_system_widgets(self, final_answer: str) -> str:
        """ Находит плейсхолдеры системных виджетов и заменяет их на ECharts JSON """
        intent_registry = {}
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
                            intent_registry[intent_name] = {
                                "widget_type": w_type,
                                "config": intent.get("config", {}),
                                "tool_name": tool_name,
                                "description": intent.get("description", "").lower()}
        result_parts = []
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
                for key, value in filter_rules.items():
                    if isinstance(value, str) and "|" in value:
                        filter_rules[key] = [v.strip() for v in value.split("|")]
                if intent_name not in intent_registry:
                    # Fuzzy match - LLM иногда опечатывается в имени intent-а
                    # (например, availability_ghap вместо availability_gap)
                    from difflib import get_close_matches
                    matches = get_close_matches(
                        intent_name,
                        list(intent_registry.keys()),
                        n=1,
                        cutoff=0.7)
                    if matches:
                        correct_name = matches[0]
                        print(
                            f"[WIDGET-FUZZY] Intent '{intent_name}' -> '{correct_name}' "
                            f"(fuzzy match, опечатка LLM)")
                        intent_name = correct_name
                    else:
                        result_parts.append(
                            f"<!-- ERROR: Unknown intent '{intent_name}'. "
                            f"Available: {', '.join(intent_registry.keys())} -->")
                        last_end = end_idx + len(end_marker)
                        start_idx = final_answer.find(start_marker, last_end)
                        continue
                reg = intent_registry[intent_name]
                # ЗАЩИТА: особые виджеты (action_card и др.) нельзя
                # рендерить через плейсхолдер, им нужны message/button от LLM
                if is_special_widget(reg["widget_type"]):
                    result_parts.append(
                        f"<!-- ERROR: intent '{intent_name}' имеет тип "
                        f"'{reg['widget_type']}' — это ОСОБЫЙ виджет. "
                        f"Его НЕЛЬЗЯ рендерить через <!-- SYSTEM_WIDGET -->. "
                        f"LLM должна сгенерировать полный JSON-блок с message/button "
                        f"в Final Answer. -->")
                    last_end = end_idx + len(end_marker)
                    start_idx = final_answer.find(start_marker, last_end)
                    continue
                tool_name = reg["tool_name"]
                if not tool_name or tool_name not in self.data_cache:
                    result_parts.append(f"<!-- ERROR: No cached data for tool '{tool_name}' -->")
                else:
                    raw_data = self.data_cache[tool_name]
                    description = reg["description"]
                    filtered = raw_data
                    if 'period начинается со слова "month"' in description:
                        filtered = [r for r in filtered if r.get("period", "").lower().startswith("month")]
                    elif 'forecast' in description and 'eac' in description:
                        filtered = [r for r in filtered if r.get("scenario", "").strip() not in ("", "actual")]
                    if filter_rules:
                        temp_filtered = []
                        for row in filtered:
                            match = True
                            for key, value in filter_rules.items():
                                row_val = str(row.get(key, ""))
                                if isinstance(value, dict):
                                    continue
                                if isinstance(value, list):
                                    if not any(fnmatch.fnmatch(row_val, str(v)) for v in value):
                                        match = False
                                        break
                                elif isinstance(value, str) and '*' in value:
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
                    descriptor = {"widget_type": reg["widget_type"],
                        "intent": intent_name,
                        "title": title or "Chart",
                        "config": reg["config"],
                        "data_rows": filtered}
                    echarts_json = render_widget(descriptor)
                    if echarts_json:
                        result_parts.append(f"```json\n{json.dumps(echarts_json, ensure_ascii=False, indent=2)}\n```")
                    else:
                        result_parts.append(
                            f"<!-- ERROR: render_widget returned None for "
                            f"{intent_name} (possibly 0 rows after filter, "
                            f"or config mismatch). Filter was: {filter_rules} -->")
            except Exception as e:
                result_parts.append(f"<!-- SYSTEM{inner_text}--> [WIDGET PARSER ERROR: {str(e)}]")
            last_end = end_idx + len(end_marker)
            start_idx = final_answer.find(start_marker, last_end)
        result_parts.append(final_answer[last_end:])
        return "".join(result_parts)

    async def run(self, query: str, max_iterations: int = 16):
        # МАРШРУТИЗАЦИЯ
        try:
            selected_plugins = await self.route_query(query)
        except Exception as e:
            print(f"Ошибка маршрутизации: {e}")
            return "Не удалось определить контекст запроса."
        active_tools = [t for t in self.all_tools if t["plugin"] in selected_plugins]
        if not active_tools:
            print(f"Для выбранных плагинов не найдено инструментов.")
            return "Не удалось найти подходящие инструменты."
        # КОНЦЕНТРАЦИЯ ЗНАНИЙ
        print(f"\n[Concentrator] Концентрация знаний из {len(selected_plugins)} плагинов...")
        ctx = await self.concentrator.concentrate(query, selected_plugins, active_tools)
        if ctx.fallback_used:
            print("[Concentrator] ВНИМАНИЕ: используется fallback — полный контекст методички")
        print()
        # Карты маппинга (Skill -> Tool, Plugin -> Tool) - на случай, если LLM
        # в ReAct вызовет skill вместо tool
        skill_to_tool_map = {}
        plugin_to_tool_map = {}
        for tool in active_tools:
            plugin_name = tool.get("plugin")
            if plugin_name and plugin_name not in plugin_to_tool_map:
                plugin_to_tool_map[plugin_name] = tool["name"]
        for plugin in self.structure.plugins:
            if plugin["name"] in selected_plugins:
                for skill in plugin.get("skills", []):
                    skill_name = skill.get("name")
                    if skill_name:
                        fallback_tool = next(
                            (t["name"] for t in active_tools if t["plugin"] == plugin["name"]),
                            None)
                        if fallback_tool:
                            skill_to_tool_map[skill_name] = fallback_tool
        active_plugin_names = ", ".join(selected_plugins)
        print(f"\n{'='*80}")
        print(f"ДАННЫЕ ДЛЯ КОНТРОЛЯ:")
        print(f"{'='*80}")
        print(f"Активные плагины: {active_plugin_names}")
        print(f"Количество инструментов: {len(active_tools)}")
        for tool in active_tools:
            print(f"  - {tool['name']} (из плагина: {tool['plugin']})")
        print(f"{'='*80}\n")
        # ReAct-цикл
        # Сбрасываем историю вызовов для нового запроса
        self._tool_call_history.clear()
        self._tool_call_counts.clear()
        try:
            prompt_text = self._build_react_prompt(
                query, active_tools, selected_plugins, ctx)
            print(f"[DEBUG] React prompt: {len(prompt_text)} символов")
            history = [{"role": "user", "content": prompt_text}]
        except Exception as e:
            import traceback
            print("\n[КРИТИЧЕСКАЯ ОШИБКА В _build_react_prompt!]")
            traceback.print_exc()
            return "Ошибка при сборке системного промпта."
        for iteration in range(max_iterations):
            print(f"\n[Итерация {iteration + 1}]")
            if self._detect_loop(history):
                print("Обнаружено зацикливание LLM. Принудительное завершение.")
                return "Обнаружено зацикливание LLM. Принудительное завершение"
            try:
                loop = asyncio.get_event_loop()
                # ДИНАМИЧЕСКИЙ РАСЧЕТ LIMMITA ДЛЯ ОТВЕТА
                # CONTEXT_WINDOW_SIZE = 14000
                # SAFETY_BUFFER = 400 # Оставляем немного места на неточности подсчета токенов
                # # Быстрая оценка: 1 токен ~ 4 символа для английского, ~2.5 для русского.
                # # Берем усредненный коэффициент 3.5 для нашего микса
                # history_chars = sum(len(msg["content"]) for msg in history)
                # estimated_input_tokens = int(history_chars / 3.5)
                # remaining_tokens = CONTEXT_WINDOW_SIZE - estimated_input_tokens - SAFETY_BUFFER
                # if remaining_tokens < 500:
                #     print("КРИТИЧЕСКОЕ ПРЕДУПРЕЖДЕНИЕ: История почти заполнила контекстное окно. Принудительное завершение.")
                #     return "История диалога слишком велика для данного окна модели."
                # dynamic_max_tokens = min(8096, remaining_tokens) # Ограничиваем сверху разумным пределом
                def sync_llm_call():
                    return self.llm_client.chat.completions.create(
                        model=LLM_MODEL_NAME,
                        messages=history,
                        temperature=_TEMPERATURE,
                        max_tokens=_MAX_TOKENS)
                response = await loop.run_in_executor(None, sync_llm_call)
                llm_text = response.choices[0].message.content
                print(f"LLM (полный ответ):\n{llm_text}\n" + "-" * 30)
            except Exception as e:
                print(f"Ошибка обращения к LLM: {e}")
                return "Ошибка подключения к LLM."
            parsed = self._parse_llm_response(llm_text)
            if parsed["type"] == "action":
                clean_assistant_msg = (
                    f"Thought: {parsed.get('thought', '')}\n"
                    f"Action: {parsed['action']}\n"
                    f"Action Input: {parsed['action_input']}")
                history.append({"role": "assistant", "content": clean_assistant_msg})
                print(f"\n{'='*60}")
                print(f"САНИТИЗИРОВАННОЕ СООБЩЕНИЕ ДЛЯ ИСТОРИИ:")
                print(f"{'='*60}")
                print(clean_assistant_msg)
                print(f"{'='*60}\n")
            else:
                # Для Final Answer / Thought / галлюцинированного Observation
                # сохраняем assistant-сообщение как есть
                history.append({"role": "assistant", "content": llm_text})
            # ОБРАБОТКА ГАЛЛЮЦИНИРОВАННОГО OBSERVATION
            # LLM написала "Observation:" без вызова Action - нарушение ReAct.
            # Вставляем user-сообщение с предупреждением, чтобы:
            #   1) LLM поняла, что Observation приходит от системы, а не от неё
            #   2) Не было 2 assistant-сообщений подряд
            if parsed["type"] == "hallucinated_observation":
                print("[PARSER] LLM сгаллюцинировала Observation без Action. "
                      "Вставляю user-сообщение-предупреждение.")
                warning_msg = (
                    "ОШИБКА: вы написали 'Observation:' без предшествующего 'Action:'. "
                    "Это нарушение формата ReAct. Observation предоставляет ТОЛЬКО система "
                    "после выполнения Action. ВЫ НЕ ДОЛЖНЫ писать Observation сами.\n\n"
                    "Пожалуйста, начните с 'Thought:' и вызовите инструмент через 'Action:'. "
                    "Или, если данных достаточно, выдайте 'Final Answer:'.")
                history.append({"role": "user", "content": warning_msg})
                continue
            # ОБРАБОТКА ЧИСТОГО THOUGHT (без Action и Final Answer)
            # LLM выдала только Thought, не решила что делать дальше.
            # Вставляем user-сообщение, чтобы подтолкнуть к Action/Final Answer
            # и избежать 2 assistant-сообщений подряд.
            if parsed["type"] == "thought":
                print("[PARSER] LLM выдала только Thought без Action/Final Answer. "
                      "Вставляю user-сообщение-подсказку.")
                nudge_msg = (
                    "Продолжайте. Вы написали Thought, но не сделали выбор:\n"
                    "- если данных НЕДОСТАТОЧНО — вызовите инструмент: "
                    "'Action: <tool_name>' + 'Action Input: {...}'\n"
                    "- если данных ДОСТАТОЧНО — выдайте 'Final Answer: ...'\n"
                    "НЕ пишите 'Observation:' сами — его предоставляет система.")
                history.append({"role": "user", "content": nudge_msg})
                continue
            if parsed["type"] == "final_answer":
                # ДЕТЕКЦИЯ "PLAN-AS-DONE" ГАЛЛЮЦИНАЦИИ:
                # LLM выдала Final Answer с <!-- SYSTEM_WIDGET --> плейсхолдерами,
                # но ни один инструмент не был вызван (кэш пуст).
                # Это значит, что LLM приняла план за отчёт о сделанном
                answer_text = parsed["answer"]
                has_system_widgets = "<!-- SYSTEM_WIDGET" in answer_text
                has_observations = any(
                    msg["role"] == "user" and msg["content"].startswith("Observation:")
                    for msg in history)
                if has_system_widgets and not has_observations:
                    print("[PARSER] Final Answer с виджетами, но ни один инструмент не вызван. "
                          "Отклоняю — требую вызвать инструмент.")
                    reject_msg = (
                        "ОТКЛОНЕНО. Ваш Final Answer содержит плейсхолдеры виджетов "
                        "<!-- SYSTEM_WIDGET -->, но вы НЕ ВЫЗВАЛИ ни одного инструмента. "
                        "Данные для виджетов берутся из Observation, а Observation "
                        "появляется только после вызова инструмента через Action.\n\n"
                        "Вы перепутали ПЛАН (инструкцию) с ОТЧЁТОМ о сделанном. "
                        "План — это то, что ВАМ нужно сделать. Вы ещё ничего не сделали.\n\n"
                        "Пожалуйста, начните заново:\n"
                        "1. Вызовите первый инструмент из плана через Action.\n"
                        "2. Дождитесь Observation от системы.\n"
                        "3. Только после получения данных выдавайте Final Answer с виджетами.")
                    history.append({"role": "user", "content": reject_msg})
                    continue
                observations = [
                    msg["content"] for msg in history
                    if msg["role"] == "user" and msg["content"].startswith("Observation:")]
                hallucinated = self._detect_hallucinations(parsed["answer"], observations)
                if hallucinated:
                    print(f"\n{'-'*60}")
                    print(f"ВНИМАНИЕ: Потенциальные галлюцинации ({len(hallucinated)}):")
                    for item in hallucinated:
                        print(f"  - {item}")
                    print(f"{'-'*60}")
                final_text = parsed["answer"]
                final_text = self._render_system_widgets(final_text)
                final_text = self._expand_widget_descriptors(final_text)
                print(f"Финальный ответ:\n{final_text}\n")
                return final_text
            elif parsed["type"] == "action":
                tool = next((t for t in active_tools if t["name"] == parsed["action"]), None)
                if not tool:
                    original_action = parsed["action"]
                    if original_action in skill_to_tool_map:
                        parsed["action"] = skill_to_tool_map[original_action]
                        tool = next((t for t in active_tools if t["name"] == parsed["action"]), None)
                        print(f"[CORRECTION] LLM вызвала skill '{original_action}', исправлено на tool '{parsed['action']}'")
                    elif original_action in plugin_to_tool_map:
                        parsed["action"] = plugin_to_tool_map[original_action]
                        tool = next((t for t in active_tools if t["name"] == parsed["action"]), None)
                        print(f"[CORRECTION] LLM вызвала plugin '{original_action}', исправлено на tool '{parsed['action']}'")
                if not tool:
                    from difflib import get_close_matches
                    tool_names = [t["name"] for t in active_tools]
                    matches = get_close_matches(parsed["action"], tool_names, n=1, cutoff=0.7)
                    if matches:
                        tool = next(t for t in active_tools if t["name"] == matches[0])
                        print(f"Автоисправление (fuzzy): '{parsed['action']}' -> '{matches[0]}'")
                        parsed["action"] = matches[0]
                if not tool:
                    observation = (
                        f"Error: Tool '{parsed['action']}' not found in active tools. "
                        f"Available tools: {', '.join([t['name'] for t in active_tools])}")
                else:
                    try:
                        action_input = json.loads(parsed["action_input"])
                        # TODO: Это спорно, повторные вызовы могут ть, наверное не позволять повторные ПОДРЯД
                        # ЗАЩИТА ОТ ДУБЛЬ-ВЫЗОВА: считаем, сколько раз этот tool
                        # вызывался с такими же аргументами
                        call_sig = (parsed["action"], json.dumps(action_input, sort_keys=True))
                        call_count = self._tool_call_counts.get(call_sig, 0)
                        if call_count >= 1:
                            # Уже вызывался. Считаем повторный вызов
                            self._tool_call_counts[call_sig] = call_count + 1
                            print(f"[DEDUP] Инструмент {parsed['action']} вызывается "
                                  f"повторно (попытка #{call_count + 1})")
                            if call_count >= 2:
                                # 3-я+ попытка -> ПРИНУДИТЕЛЬНЫЙ Final Answer.
                                # LLM зациклилась, ждать дальше бессмысленно
                                print("[DEDUP] 3+ вызова одного инструмента → "
                                      "ПРИНУДИТЕЛЬНЫЙ Final Answer")
                                forced_answer = (
                                    "На основе полученных данных формирую ответ.\n\n"
                                    "ВНИМАНИЕ: вы попытались вызвать инструмент "
                                    f"'{parsed['action']}' уже {call_count + 1} раз с "
                                    "одинаковыми аргументами. Это зацикливание. "
                                    "Используйте ТОЛЬКО данные, которые уже есть в "
                                    "предыдущих Observation. Проанализируйте их сами "
                                    "(не вызывайте инструменты) и выдайте Final Answer "
                                    "с виджетами согласно плану.")
                                history.append({
                                    "role": "user",
                                    "content": f"Observation: {forced_answer}"})
                                continue
                            # 2-я попытка -> возвращаем кэш + ПРЕДУПРЕЖДЕНИЕ
                            cached_obs = self._tool_call_history[call_sig]
                            warning = (
                                "\n\n[СИСТЕМНОЕ ПРЕДУПРЕЖДЕНИЕ] Вы уже вызывали "
                                f"инструмент '{parsed['action']}' с аргументами "
                                f"{action_input}. Данные выше — это ТОТ ЖЕ результат. "
                                "НЕ ВЫЗЫВАЙТЕ этот инструмент снова. "
                                "Переходите к СЛЕДУЮЩЕМУ шагу плана или к Final Answer. "
                                "Анализ (подсчёт периодов, сравнение порогов) "
                                "делайте В Thought сами, а не через вызов инструмента.")
                            observation = cached_obs + warning
                        else:
                            # Первый вызов - реальный MCP-запрос
                            self._tool_call_counts[call_sig] = 1
                            print(f"Вызов инструмента: {parsed['action']}")
                            print(f"Аргументы: {json.dumps(action_input, ensure_ascii=False)}")
                            session = self.mcp_sessions[tool["server"]]
                            print(f"Вызов MCP-сервера: {tool['server']}...")
                            result = await session.call_tool(parsed["action"], action_input)
                            observation = "\n".join([
                                str(item.text) if hasattr(item, 'text') else str(item)
                                for item in result.content])
                            # Сохраняем в историю вызовов (сырой, до модификаций)
                            self._tool_call_history[call_sig] = observation
                        # Извлекаем сырые данные в кеш
                        try:
                            obs_json = json.loads(observation)
                            if "_raw_data" in obs_json:
                                self.data_cache[parsed["action"]] = obs_json["_raw_data"]
                                del obs_json["_raw_data"]
                                observation = json.dumps(obs_json, ensure_ascii=False, indent=2)
                                print(f"[CACHE] Сохранено {len(self.data_cache[parsed['action']])} строк для {parsed['action']}")
                        except json.JSONDecodeError:
                            pass
                        print(f"\n{'='*60}")
                        print(f"СЫРОЙ ОТВЕТ ОТ MCP-СЕРВЕРА (ПОСЛЕ ИЗВЛЕЧЕНИЯ КЭША):")
                        print(observation[:1500] + ("..." if len(observation) > 1500 else ""))
                        print(f"{'='*60}\n")
                    except json.JSONDecodeError:
                        print(f"Ошибка парсинга JSON в Action Input: {parsed['action_input']}")
                        observation = f"Error: Invalid JSON in Action Input."
                    except Exception as e:
                        print(f"Ошибка выполнения инструмента: {e}")
                        import traceback
                        traceback.print_exc()
                        observation = f"Error executing tool: {str(e)}"
                display_obs = observation if len(observation) < 600 else observation[:600] + "\n...[обрезано для консоли]..."
                print(f"Observation (для консоли):\n{display_obs}")
                print(f"\n{'='*60}")
                print(f"ПЕРЕДАЁТСЯ В LLM КАК OBSERVATION:")
                print(f"Размер: {len(observation)} символов")
                print(f"{'='*60}\n")
                history.append({"role": "user", "content": f"Observation: {observation}"})
        return "Превышено количество итераций."

async def main():
    agent = PmIqAgent()
    try:
        await agent.initialize()
        queries = [
            # "Покажи активные риски с высоким воздействием для проекта 'Миграция ERP' и предложи план реагирования, ответь с диаграммой."
            "Проанализируй загрузку команды разработки, ответь с диаграммой."
            # # Простой запрос (должен выбрать 1 плагин)
            # "Каков текущий статус критического пути по проекту 'Миграция ERP' и есть ли задержки?",
            # # Кросс-доменный запрос (должен выбрать 2-3 плагина: core + value + risk)
            # "Как 5-дневная задержка в проекте 'Миграция ERP' повлияет на бюджет и какие есть связанные с этим риски?",
            # # Запрос к ресурсам
            # "Есть ли перегрузка ресурсов в команде разработки на следующей неделе?"
        ]
        for q in queries:
            await agent.run(q)
            print("\n" + "=" * 80 + "\n")
    finally:
        print("\nЗавершение работы и очистка ресурсов...")
        await agent.close()

if __name__ == "__main__":
    if sys.platform == 'win32':
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(main())