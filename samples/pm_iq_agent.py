""" PM IQ Agent - Архитектура с мульти-плагиновой маршрутизацией (Уровень 1).

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
  многоэтапные автономные workflow """

import asyncio
import json
import re
import sys
from pathlib import Path
from typing import List, Dict, Any
import yaml
import fnmatch
from widget_renderer import render_widget
from difflib import get_close_matches
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from openai import OpenAI

PM_IQ_ROOT = Path(__file__).parent.parent

# Настройки LLM
LLM_BASE_URL = "http://localhost:3101/v1"
LLM_MODEL_NAME = "local-model"

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
        return {"name": frontmatter.get("name", skill_dir.name),
            "description": frontmatter.get("description", ""),
            "instructions": full_instructions,
            "path": skill_dir,
            "available_widgets": frontmatter.get("available_widgets", [])}
    
    def _parse_plugin(self, plugin_dir: Path) -> Dict[str, Any]:
        plugin_info = {"name": plugin_dir.name, "path": plugin_dir, "skills": [], "mcp_config": None}
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
        
    async def initialize(self):
        print("Сканирование структуры...")
        self.structure.scan_plugins()
        print(f"Найдено {len(self.structure.plugins)} плагинов")
        print("\nПодключение к MCP серверам (согласно .mcp.json)...")
        await self._connect_to_mcp_servers()
        print(f"Загружено {len(self.all_tools)} инструментов (внутренний реестр)")
    
    def _expand_widget_descriptors(self, final_answer: str) -> str:
        """ Постобработка виджетов после получения Final Answer от LLM """
        json_block_re = re.compile(r"```json\s*([\s\S]*?)```", re.MULTILINE)

        def replace_block(match: re.Match) -> str:
            raw = match.group(1).strip()
            
            # LLM бывает вставляет пробелы в ключи: "data_ rows" чистим в "data_rows"
            def fix_underscore_spaces(m):
                inner = m.group(1)
                fixed = re.sub(r'_\s+', '_', inner) # Заменяем "_ " на "_"
                return f'"{fixed}"'
            
            raw = re.sub(r'"([^"]*)"', fix_underscore_spaces, raw)
            try:
                descriptor = json.loads(raw)
            except json.JSONDecodeError as e:
                print(f"[WidgetRenderer] JSONDecodeError: {e} | raw[:200]: {raw[:200]}")
                return match.group(0)
            if "widget_type" not in descriptor:
                return match.group(0)
            # Если это уже отрендеренный бэкендом график (echarts), не трогаем его
            if descriptor.get("widget_type") == "echarts":
                return match.group(0)
            echarts_envelope = render_widget(descriptor)
            if echarts_envelope is None:
                print(f"[WidgetRenderer] render_widget=None для intent={descriptor.get('intent')}")
                return match.group(0)
            expanded = json.dumps(echarts_envelope, ensure_ascii=False, indent=2)
            print(f"[WidgetRenderer] OK: {descriptor.get('widget_type')}/{descriptor.get('intent')} → {len(expanded)} chars")
            return f"```json\n{expanded}\n```"

        return json_block_re.sub(replace_block, final_answer)
    
    def _clean_broken_json(self, text: str) -> str:
        """ Удаляет оборванные ```json блоки, если LLM уперлась в max_tokens """
        # Ищем все открывающие теги ```json
        # Если после них нет закрывающего ```, считаем блок битым и удаляем его
        cleaned = re.sub(r"```json\s*[\s\S]*?(?=```)", lambda m: m.group(0) if text.count("```") % 2 == 0 else "", text)
        # Более простой и надежный вариант: просто вырезаем любой ```json, за которым не следует ``` до конца строки/текста
        cleaned = re.sub(r"```json\s*[\s\S]*?(?<!```)$", "", text)
        return cleaned.strip()
    
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
        try:
            await session.__aenter__()
            await session.initialize()
        except Exception:
            # Если инициализация упала, закрываем stdio_cm, чтобы не было утечки
            try:
                await stdio_cm.__aexit__(*sys.exc_info())
            except Exception:
                pass
            raise
        self._context_managers.append((stdio_cm, session))
        return session
    
    async def close(self):
        """ Подавляет шумные BaseExceptionGroup / GeneratorExit от anyio/mcp,
        которые возникают при завершении дочерних stdio процессов """
        for stdio_cm, session in self._context_managers:
            try:
                await session.__aexit__(None, None, None)
            except Exception:
                pass  # Игнорируем ошибки закрытия сессии
            try:
                await stdio_cm.__aexit__(None, None, None)
            except Exception:
                pass  # Игнорируем GeneratorExit от anyio

    async def route_query(self, query: str) -> List[str]:
        # Собираем полное описание каждого плагина (все скиллы + примеры)
        plugins_summary = []
        for p in self.structure.plugins:
            skills_desc = "\n    ".join([f"- {s['name']}: {s['description']}" for s in p['skills']])
            plugins_summary.append(f"### {p['name']}\n    {skills_desc}")
        prompt = f"""You are a Project Management Router. 
Analyze the user's query and select the 1 to 3 MOST RELEVANT plugin domains.

CRITICAL INSTRUCTION:
- If the query mentions MULTIPLE aspects (e.g., delays AND budget, schedule AND risks), 
  you MUST select ALL relevant plugins, not just one.
- Read skill descriptions carefully. Each description explains what types of queries it handles.
- Match the user's intent to the skills whose descriptions cover the relevant aspects.

Available Domains with Skills:
{chr(10).join(plugins_summary)}

User Query: {query}

Respond with ONLY a valid JSON array of plugin names. 
Example: ["pm-project-core", "pm-value-and-performance"]
Do not add markdown formatting or explanations."""
        loop = asyncio.get_running_loop()

        def sync_llm_call():
            return self.llm_client.chat.completions.create(
                model=LLM_MODEL_NAME,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.0)

        response = await loop.run_in_executor(None, sync_llm_call)
        raw_response = response.choices[0].message.content.strip()
        
        print(f"{'='*80}")
        print(f"СЫРОЙ ОТВЕТ ОТ LLM:")
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
                    # Может это имя скилла? Ищем плагин, которому он принадлежит
                    for plug in self.structure.plugins:
                        if any(skill["name"] == item for skill in plug["skills"]):
                            valid_plugins.append(plug["name"])
                            print(f"LLM вернула имя скилла '{item}', маппим на плагин '{plug['name']}'")
                            break
            # Убираем дубликаты
            valid_plugins = list(dict.fromkeys(valid_plugins))
            # TODO: Плохое решение
            if not valid_plugins:
                valid_plugins = ["pm-project-core"]
            print(f"Маршрутизация: выбраны плагины {valid_plugins}")
            return valid_plugins
        except json.JSONDecodeError:
            print(f"Ошибка парсинга JSON от роутера. Ответ: {raw_response}. Fallback.")
            return ["pm-project-core"]

    def _build_react_prompt(self, query: str, active_tools: List[Dict[str, Any]], active_plugin_names: List[str]) -> str:
        skills_context = ""
        for plugin in self.structure.plugins:
            if plugin["name"] in active_plugin_names:
                for skill in plugin["skills"]:
                    skills_context += f"## Skill: {skill['name']} (Plugin: {plugin['name']})\nDescription: {skill['description']}\nInstructions:\n{skill['instructions']}\n\n"
        tools_context = []
        for tool in active_tools:
            schema_str = json.dumps(tool.get("inputSchema", {}), indent=2, ensure_ascii=False)
            tools_context.append(f"- {tool['name']}: {tool.get('description', '')}\n  Input schema: {schema_str}")        
        tool_names = ", ".join([t['name'] for t in active_tools])
        plugins_str = ", ".join(active_plugin_names)
        
        return f"""**КРИТИЧЕСКОЕ ПРЕДУПРЕЖДЕНИЕ: Вы работаете с РЕАЛЬНЫМИ данными из корпоративных систем (Jira, SAP, Workday и т.п.).**
**НИКОГДА не выдумывайте, не изменяйте данные. Используйте ТОЛЬКО данные, предоставленные в Observation.**
**Если вы не уверены, попросите уточнения вместо того, чтобы гадать.**

Вы — экспертный Помощник по Управлению Проектами, придерживающийся принципов PMBOK 8.
В данный момент вы работаете в доменах: {plugins_str}

У вас есть доступ к следующим навыкам (Skills) с подробными инструкциями:
{skills_context}

У вас есть доступ к следующим инструментам:
{chr(10).join(tools_context)}

КРИТИЧЕСКИ ВАЖНЫЕ ПРАВИЛА:

1. **ЦЕЛОСТНОСТЬ ДАННЫХ:** 
   - Observation содержит JSON с основным полем `data` — это Markdown-таблица с фактической информацией.
   - Пример структуры:
        {{
          "data": "| resource_name | allocation_pct | status |\\n|---|---|---|\\n| Petrov A. | 130 | Overallocated |"
        }}
   - Вы должны использовать ТОЛЬКО данные из поля `data`. НИКОГДА не выдумывайте имена, числа или факты.
   - **МАППИНГ СУЩНОСТЕЙ (ВАЖНО):** Пользователи могут называть сущности разговорными терминами (например, "Перенос 1С", "Стройка", "Фундамент"). Вы ДОЛЖНЫ самостоятельно сопоставить их с точными названиями из полей `project_name`, `wbs_name` или `resource_name` в таблице `data` (например, "1C_Transformation", "Construction_Phase1").
   - В Final Answer и в `data_rows` виджетов ВСЕГДА используйте ТОЛЬКО точные системные имена из таблицы `data`. НЕ ПЕРЕИМЕНОВЫВАЙТЕ сущности.   
   - Парсите Markdown-таблицу внимательно: каждая строка после разделителя — это отдельная запись.
   - **ВАЖНО: ИМЕНА ИНСТРУМЕНТОВ:** Вы можете вызывать ТОЛЬКО инструменты из списка выше. НИКОГДА не используйте имена навыков (Skills) в качестве Action.
   - **ВАЖНО: СИНТАКСИС ФИЛЬТРОВ:** В плейсхолдере `FILTER: {{}}` передавайте ТОЛЬКО простые значения (строки) или массивы строк `["A", "B"]`. ЗАПРЕЩЕНО использовать синтаксис баз данных (например, `{{"$regex": "..."}}` или `{{"$gt": 10}}`).

2. **ОТОБРАЖЕНИЕ ВИДЖЕТОВ:**
   В ваших инструкциях (выше) описаны доступные виджеты. У каждого есть `intent` и `description`.
   
   **ВИДЖЕТ ТИП А: СИСТЕМНЫЕ ВИДЖЕТЫ (LineChart, BarChart, ScatterChart и т.д.)**
   Эти виджеты отображают данные из таблиц `data` с использованием заданного вами фильтра. НЕ генерируйте для них JSON с data_rows!
   1. ВЫБЕРИТЕ один intent, чей `description` наилучшим образом соответствует вопросу.
   2. Напишите ОДИН плейсхолдер строго в формате:
      <!-- SYSTEM_WIDGET: имя_intent | FILTER: {{"ключ_из_data": "значение"}} | TITLE: Ваш контекстный заголовок -->
   3. В FILTER передавайте JSON с параметрами для фильтрации строк из таблицы `data`.

   **ВИДЖЕТ ТИП Б: ВИДЖЕТЫ МОДЕЛИ (action_card И ДРУГИЕ КАСТОМНЫЕ ВИДЖЕТЫ)**
   1. Для `action_card` вы ВСЕГДА генерируете ПОЛНЫЙ JSON-блок в формате ```json```, строго следуя инструкции из Skill. Для action_card поле "data_rows" должно быть пустым массивом [], и не указывайте название intent.
   2. **СТРОГИЙ ЗАПРЕТ:** Для `action_card` и других виджетов Типа Б ЗАПРЕЩЕНО писать плейсхолдер `<!-- SYSTEM_WIDGET: ... -->` перед JSON-блоком. Пишите ТОЛЬКО JSON-блок.
   3. Если вы сами рассчитали данные, которых нет в таблице `data`, вы можете сгенерировать виджет ПОЛНОСТЬЮ сами. Поле `widget_type` ДОЛЖНО содержать тип диаграммы (например, "action_card", "BarChart" и т.д.), НЕ пишите в таком случае название intent в `widget_type`.
   4. **ВАЖНО:** Виджеты Типа Б всегда оформляются как полноценные JSON-блоки. Плейсхолдеры <!-- SYSTEM_WIDGET --> применяются ИСКЛЮЧИТЕЛЬНО к виджетам Типа А.
   
   **ОБЩИЕ ПРАВИЛА ДЛЯ ВИДЖЕТОВ:**
   - ЗАПРЕЩЕНО генерировать виджеты "по памяти" или придумывать названия intent-ов. 
   - Если инструмент уже вернул нужные строки, и вы хотите отобразить их ВСЕ, укажите пустой фильтр: `FILTER: {{}}`.
   - Если запрос затрагивает несколько аспектов, включайте ВСЕ релевантные виджеты (и Типа А, и Типа Б).

3. **ПРОВЕРКА ФАКТОВ:**
   - Перед выдачей Final Answer проверьте каждое имя и число по полю `data` в Observation.
   - Если имени НЕТ в `data`, вы НЕ ДОЛЖНЫ упоминать его в ответе.
   - Проверяйте пороги: перегруженными считаются только ресурсы с загрузкой > 100%.

4. **ЛОГИКА РАБОТЫ (ИЗБЕЖАТЬ ЗАЦИКЛИВАНИЯ):**
   Вы работаете в цикле: Thought → Action → Observation → Thought → Action → Observation → ... → Final Answer
   
   **ВАЖНО:**
   - В КАЖДОМ вашем ответе вы пишете ТОЛЬКО ОДИН блок: либо `Thought + Action + Action Input`, либо `Thought + Final Answer`.
   - **НИКОГДА не пишите несколько Action в одном ответе.** Вызывайте инструменты ПО ОДНОМУ, дожидаясь Observation после каждого вызова.
   - **НЕ ПИШИТЕ "Question:" в начале вашего ответа** — вопрос уже есть в системном промпте.
   - **НЕ ПИШИТЕ "Observation:" сами** — система предоставит его автоматически после выполнения инструмента.
   - После получения `Observation` от системы вы должны принять решение:
     * **Если данных НЕДОСТАТОЧНО** (нужна информация из другого домена/инструмента) → напишите новый `Thought` и вызовите ДРУГОЙ инструмент.
     * **Если данных ДОСТАТОЧНО** для полного ответа → напишите `Thought: я теперь знаю финальный ответ` и затем `Final Answer`.
   - **ЗАПРЕЩЕНО вызывать один и тот же инструмент с теми же аргументами более одного раза.** Если вы уже вызвали `tool1` и получили данные, НЕ вызывайте его снова — переходите дальше.   

5. **ТРЕБОВАНИЕ К МНОЖЕСТВЕННЫМ ИНСТРУМЕНТАМ:**
   - Если запрос упоминает НЕСКОЛЬКО аспектов (например, задержки И бюджет), вы ДОЛЖНЫ вызвать инструменты из ВСЕХ релевантных доменов, прежде чем переходить к Final Answer.

6. **ОБЯЗАТЕЛЬНОЕ ИСПОЛЬЗОВАНИЕ РЕАЛЬНЫХ ДАННЫХ:**
   - Получив Observation, вы ДОЛЖНЫ использовать ИМЕННО эти данные. Вы НЕ ДОЛЖНЫ модифицировать, переименовывать или "улучшать" данные любым способом.

7. **ВЫХОД ИЗ ТУПИКА И ОБРАБОТКА ОШИБОК:**
   - Если вы не можете найти нужный инструмент или Observation содержит поле "error", сразу выдайте Final Answer, объяснив ситуацию и предложив альтернативы.

**ПРИМЕР ПРАВИЛЬНОГО ЦИКЛА:**

Простой запрос (один инструмент):
Первый ответ модели:
    Thought: Мне нужно получить данные о задачах.
    Action: get_tasks
    Action Input: {{}}

(Система выполнит инструмент и предоставит Observation в следующем сообщении)

Второй ответ модели:
    Thought: Я получил данные о задачах. Теперь могу ответить на вопрос.
    Final Answer: На основе полученных данных...

КРОСС-ДОМЕННЫЙ ЗАПРОС (несколько инструментов):
Запрос пользователя: "Как задержка повлияет на задачу и каков бюджет, влиияние рисков?"

Итерация 1:
    Thought: Мне нужны данные о расписании, бюджете и рисках. Начну с расписания.
    Action: get_schedule_vs_actual
    Action Input: {{}}

(Система предоставит Observation)

Итерация 2:
    Thought: Расписание получено. Теперь нужны данные о бюджете.
    Action: get_budget_vs_actual
    Action Input: {{}}

(Система предоставит Observation)

Итерация 3:
    Thought: Бюджет получен. Теперь нужен реестр рисков.
    Action: get_risks
    Action Input: {{}}

(Система предоставит Observation)

Итерация 4:
    Thought: Все данные собраны. Могу сформулировать ответ.
    Final Answer: На основе полученных данных...

**ВАЖНО:** НИКОГДА не пишите несколько Action в одном ответе. Вызывайте инструменты ПО ОДНОМУ, дожидаясь Observation после каждого вызова.

Начните!

СТРОГОЕ ПРАВИЛО ЯЗЫКА:
В шагах 'Thought:' вам РАЗРЕШАЕНО думать на английском языке.
Однако ваш 'Final Answer:' ДОЛЖЕН БЫТЬ написан ИМЕННО НА ТОМ ЖЕ ЯЗЫКЕ, на котором задан вопрос пользователя ниже. 

Вопрос: {query}"""
       
    def _parse_llm_response(self, text: str) -> Dict[str, str]:
        """ Парсит ответ LLM без использования регулярок (на базе стейт-машины) """
        lines = text.split('\n')
        # Удаляем повторение Question из начала ответа, если модель его продублировала
        if lines and lines[0].strip().lower().startswith('question:'):
            lines = lines[1:]
            text = '\n'.join(lines)
        thought_parts = []
        action = ""
        action_input_parts = []
        state = 'none'

        # # Извлекаем JSON-блоки и плейсхолдеры виджетов из текста.
        # # Они могут быть случайно сгенерированы внутри Thought, а не в Final Answer.
        # # охраним и добавим в конец Final Answer
        # orphan_widgets = []
        # # Находим все ```json ... ``` блоки
        # json_pattern = re.compile(r'```json\s*([\s\S]*?)```', re.MULTILINE)
        # for match in json_pattern.finditer(text):
        #     json_content = match.group(0)
        #     # Проверяем, что это виджет (содержит widget_type)
        #     if '"widget_type"' in json_content or "'widget_type'" in json_content:
        #         orphan_widgets.append(json_content)
        # # Находим все плейсхолдеры <!-- SYSTEM_WIDGET: ... -->
        # placeholder_pattern = re.compile(r'<!--\s*SYSTEM_WIDGET:.*?-->', re.MULTILINE)
        # for match in placeholder_pattern.finditer(text):
        #     orphan_widgets.append(match.group(0))
        
        for line in lines:
            stripped = line.strip()
            if not stripped and state != 'action_input':
                # Пропускаем пустые строки, если только мы не парсим многострочный JSON
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
                # # Если виджеты были найдены в тексте, но их нет в Final Answer,
                # # добавляем их в конец
                # if orphan_widgets:
                #     for widget in orphan_widgets:
                #         if widget not in answer_text:
                #             answer_text = answer_text + "\n\n" + widget
                return {"type": "final_answer", "answer": answer_text}
            # Если мы уже нашли Action, игнорируем все последующие Action и Action Input,
            # это защита, если модель пытается вызвать сразу несколько инструментов в одном ответе
            if state == 'action_input':
                if lower.startswith("action:") or lower.startswith("action input:"):
                    continue  # Игнорируем последующие команды
                action_input_parts.append(stripped)
                continue
            # OBSERVATION (если LLM нагаллюцинировала Observation, мы должны остановиться)
            if lower.startswith("observation:"):
                # Если к этому моменту мы собрали Action - возвращаем его
                if action:
                    return {"type": "action", 
                        "thought": "\n".join(thought_parts).strip(), 
                        "action": action.strip(), 
                        "action_input": "\n".join(action_input_parts).strip()}
                else:
                    # LLM сгаллюцинировала Observation без Action, игнорируем
                    return {"type": "thought", "text": "\n".join(thought_parts).strip() if thought_parts else text}
            # ACTION INPUT
            if lower.startswith("action input:") or lower.startswith("action input :"):
                state = 'action_input'
                # Проверяем, нет ли текста на этой же строке после двоеточия
                parts = stripped.split(":", 1)
                if len(parts) > 1 and parts[1].strip():
                    action_input_parts.append(parts[1].strip())
                continue
            # ACTION (Обрабатываем пропущенное двоеточие)
            if lower.startswith("action:") or lower.startswith("action :") or lower == "action":
                # Если состояние уже 'action', игнорируем повторные вызовы (фиксируем только первый)
                if state != 'action':
                    state = 'action'
                    parts = stripped.split(":", 1)
                    if len(parts) > 1 and parts[1].strip():
                        # Берем первое слово, если написали "Action: tool_name..."
                        action = parts[1].strip().split()[0]
                continue
            # THOUGHT
            if lower.startswith("thought:") or lower.startswith("thought :") or lower == "thought":
                state = 'thought'
                parts = stripped.split(":", 1)
                if len(parts) > 1 and parts[1].strip():
                    thought_parts.append(parts[1].strip())
                continue
            # ОБРАБОТКА ТЕКСТА ВНУТРИ БЛОКОВ (если ключевых слов на этой строке нет)
            if state == 'thought':
                thought_parts.append(stripped)
            elif state == 'action':
                # LLM бывает пишет: "Action:" на одной строке, а "get_risk_register" на следующей
                if not action:
                    action = stripped.split()[0] 
            elif state == 'action_input':
                # Собираем многострочный JSON
                action_input_parts.append(stripped) 
        # Если после прохода по всем строкам у нас есть Action, возвращаем его
        if action:
            return {"type": "action", 
                    "thought": "\n".join(thought_parts).strip(), 
                    "action": action.strip(), 
                    "action_input": "\n".join(action_input_parts).strip()}
        # Если ничего не поняли - возвращаем как есть, детектор зацикливания это отловит
        return {"type": "thought", "text": text}
    
    def _detect_loop(self, history: List[Dict], threshold: int = 3) -> bool:
        """ Обнаруживает зацикливание: если последние N ответов LLM похожи, возвращает True """
        if len(history) < threshold * 2:
            return False
        # Берем последние N ответов ассистента
        recent_assistant_msgs = [
            msg["content"] for msg in history[-threshold*2:] 
            if msg["role"] == "assistant"][-threshold:]
        # Проверяем, насколько они похожи (простая проверка: первые 100 символов)
        if len(recent_assistant_msgs) < threshold:
            return False
        first_msg_prefix = recent_assistant_msgs[0][:100]
        similar_count = sum(1 for msg in recent_assistant_msgs if msg[:100] == first_msg_prefix)
        return similar_count >= threshold

    def _detect_hallucinations(self, final_answer: str, observations: List[str]) -> List[str]:
        """ Это только пример! Проверяет, не выдумала ли LLM имена, которых нет в данных """
        # Извлекаем все имена (паттерн: Имя + инициал, например "Alexeev A." или "Иванов И.")
        mentioned_names = set(re.findall(r'\b([А-ЯA-Z][а-яa-z]+\s+[А-ЯA-Z]\.)\b', final_answer))
        # Собираем все имена из Observation
        data_names = set()
        for obs in observations:
            data_names.update(re.findall(r'\b([А-ЯA-Z][а-яa-z]+\s+[А-ЯA-Z]\.)\b', obs))
        # Находим выдуманные имена
        hallucinated = mentioned_names - data_names
        return list(hallucinated)
    
    def _render_system_widgets(self, final_answer: str) -> str:
        """ Находит плейсхолдеры системных виджетов и заменяет их на ECharts JSON """
        # Собираем индекс виджетов из скиллов
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
        # Парсинг строками (без регулярок)
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
            inner_text = final_answer[start_idx + len(start_marker) : end_idx]
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
                # Исправляем LLM-синтаксис "A|B|C" на нормальный массив ["A", "B", "C"]
                for key, value in filter_rules.items():
                    if isinstance(value, str) and "|" in value:
                        filter_rules[key] = [v.strip() for v in value.split("|")]
                # Логика рендеринга
                if intent_name not in intent_registry:
                    result_parts.append(f"<!-- ERROR: Unknown intent '{intent_name}' -->")
                else:
                    reg = intent_registry[intent_name]
                    # Если это action_card или другой виджет Типа Б, не рендерим как системный.
                    # Модель должна была сгенерировать его как JSON-блок, а не плейсхолдер
                    if reg["widget_type"] == "action_card" or "action" in intent_name.lower():
                        # Просто удаляем плейсхолдер (модель нарушила правило, но мы не ломаем вывод)
                        result_parts.append("")
                        last_end = end_idx + len(end_marker)
                        start_idx = final_answer.find(start_marker, last_end)
                        continue
                    tool_name = reg["tool_name"]
                    if not tool_name or tool_name not in self.data_cache:
                        result_parts.append(f"<!-- ERROR: No cached data for tool '{tool_name}' -->")
                    else:
                        raw_data = self.data_cache[tool_name]
                        description = reg["description"]
                        # Правила из SKILL.md
                        filtered = raw_data
                        if 'period начинается со слова "month"' in description:
                            filtered = [r for r in filtered if r.get("period", "").lower().startswith("month")]
                        elif 'forecast' in description and 'eac' in description:
                            filtered = [r for r in filtered if r.get("scenario", "").strip() not in ("", "actual")]
                        # Динамический фильтр от LLM
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
                        # Сборка и рендер
                        descriptor = {"widget_type": reg["widget_type"],
                            "intent": intent_name,
                            "title": title or "Chart",
                            "config": reg["config"],
                            "data_rows": filtered}
                        echarts_json = render_widget(descriptor)
                        if echarts_json:
                            result_parts.append(f"```json\n{json.dumps(echarts_json, ensure_ascii=False, indent=2)}\n```")
                        else:
                            result_parts.append(f"<!-- ERROR: render_widget failed for {intent_name} (filtered 0 rows) -->")
            except Exception as e:
                result_parts.append(f"<!-- SYSTEM{inner_text}--> [WIDGET PARSER ERROR: {str(e)}]")
            last_end = end_idx + len(end_marker)
            start_idx = final_answer.find(start_marker, last_end)
        result_parts.append(final_answer[last_end:])
        return "".join(result_parts)
    
    async def run(self, query: str, max_iterations: int = 16):
        print(f"\nВопрос: {query}\n")
        # Мульти-маршрутизация
        try:
            selected_plugins = await self.route_query(query)
        except Exception as e:
            print(f"Ошибка маршрутизации: {e}")
            return "Не удалось определить контекст запроса."
        # Агрегация инструментов
        active_tools = [t for t in self.all_tools if t["plugin"] in selected_plugins]
        if not active_tools:
            print(f"Для выбранных плагинов не найдено инструментов.")
            return "Не удалось найти подходящие инструменты."
        # Строим карты маппинга (Skill -> Tool, Plugin -> Tool)
        skill_to_tool_map = {}
        plugin_to_tool_map = {}
        # Маппинг: Имя Плагина -> Первый инструмент этого плагина (запасной вариант)
        for tool in active_tools:
            plugin_name = tool.get("plugin")
            if plugin_name and plugin_name not in plugin_to_tool_map:
                plugin_to_tool_map[plugin_name] = tool["name"]
        # Маппинг: Имя Скилла -> Первый инструмент родительского плагина
        for plugin in self.structure.plugins:
            if plugin["name"] in selected_plugins:
                for skill in plugin.get("skills", []):
                    skill_name = skill.get("name")
                    if skill_name:
                        # Находим первый инструмент, относящийся к этому плагину
                        fallback_tool = next((t["name"] for t in active_tools if t["plugin"] == plugin["name"]), None)
                        if fallback_tool:
                            skill_to_tool_map[skill_name] = fallback_tool
        active_plugin_names = ", ".join(selected_plugins)
        print(f"\n{'='*80}")
        print(f"ИНСТРУМЕНТЫ В ПРОМПТЕ REACT:")
        print(f"{'='*80}")
        print(f"Активные плагины: {active_plugin_names}")
        print(f"Количество инструментов: {len(active_tools)}")
        print(f"Список инструментов:")
        for tool in active_tools:
            print(f"  - {tool['name']} (из плагина: {tool['plugin']})")
        print(f"{'='*80}\n")
        # Отладка сборки промпта
        print("[DEBUG] Начинаю собирать промпт ReAct...")
        try:
            prompt_text = self._build_react_prompt(query, active_tools, selected_plugins)
            print(f"[DEBUG] Промпт успешно собран. Длина: {len(prompt_text)} символов")
            history = [{"role": "user", "content": prompt_text}]
        except Exception as e:
            import traceback
            print("\n[КРИТИЧЕСКАЯ ОШИБКА В _build_react_prompt!]")
            traceback.print_exc()
            return "Ошибка при сборке системного промпта."
               
        for iteration in range(max_iterations):
            print(f"\n[Итерация {iteration + 1}]")
            # Детектор зацикливания
            if self._detect_loop(history):
                print("Обнаружено зацикливание LLM. Принудительное завершение.")
                return "Обнаружено зацикливание LLM. Принудительное завершение"
            try:
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
                loop = asyncio.get_running_loop()
                def sync_llm_call():
                    return self.llm_client.chat.completions.create(
                        model=LLM_MODEL_NAME, 
                        messages=history, 
                        temperature=0.1,
                        # TODO: max_tokens
                        max_tokens=8096)
                response = await loop.run_in_executor(None, sync_llm_call)
                llm_text = response.choices[0].message.content
                print(f"LLM (полный ответ):\n{llm_text}\n" + "-" * 30)
            except Exception as e:
                print(f"Ошибка обращения к LLM: {e}")
                return "Ошибка подключения к LLM."
            parsed = self._parse_llm_response(llm_text)
            # Санизируем историю.
            # Если был вызов Action, мы сохраняем в историю только Thought и Action.
            # Мы намеренно выбрасываем любой галлюцинированный Observation или Final Answer, 
            # который LLM могла сгенерировать в том же ответе, чтобы не "отравлять" контекст для следующего шага
            if parsed["type"] == "action":
                clean_assistant_msg = f"Thought: {parsed.get('thought', '')}\nAction: {parsed['action']}\nAction Input: {parsed['action_input']}"
                history.append({"role": "assistant", "content": clean_assistant_msg})
                # Показываем, что реально сохраняется в историю
                print(f"\n{'='*60}")
                print(f"САНИТИЗИРОВАННОЕ СООБЩЕНИЕ ДЛЯ ИСТОРИИ:")
                print(f"{'='*60}")
                print(clean_assistant_msg)
                print(f"{'='*60}\n")
            else:
                # Для Final Answer или простого Thought сохраняем как есть
                history.append({"role": "assistant", "content": llm_text})             
            if parsed["type"] == "final_answer":
                observations = [msg["content"] for msg in history if msg["role"] == "user" and msg["content"].startswith("Observation:")]
                hallucinated = self._detect_hallucinations(parsed["answer"], observations)
                if hallucinated:
                    print(f"\n{'-'*60}")
                    print(f"ВНИМАНИЕ: Потенциальные галлюцинации ({len(hallucinated)}):")
                    for item in hallucinated:
                        print(f"  • {item}")
                    print(f"{'-'*60}")
                final_text = parsed["answer"]
                # Сначала рендерим системные плейсхолдеры (подставляют данные из кэша)
                final_text = self._render_system_widgets(final_text)
                # Затем рендерим то, что сгенерировала LLM сама (action_card и llm-виджеты)
                final_text = self._expand_widget_descriptors(final_text)
                # Очистка от оборванных JSON
                # final_text = self._clean_broken_json(final_text)
                print(f"Финальный ответ:\n{final_text}\nCompleted")
                return final_text
            elif parsed["type"] == "action":
                # Точное совпадение
                tool = next((t for t in active_tools if t["name"] == parsed["action"]), None)
                # Если не найдено, проверяем не вызвала ли модель скилл или плагин
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
                # Если всё ещё не найдено, то ищем похожее имя (fuzzy matching)
                if not tool:
                    tool_names = [t["name"] for t in active_tools]
                    matches = get_close_matches(parsed["action"], tool_names, n=1, cutoff=0.7)
                    if matches:
                        tool = next(t for t in active_tools if t["name"] == matches[0])
                        print(f"Автоисправление (fuzzy): '{parsed['action']}' → '{matches[0]}'")
                        parsed["action"] = matches[0]  # исправляем для истории
                if not tool:
                    observation = f"Error: Tool '{parsed['action']}' not found in active tools. Available tools: {', '.join([t['name'] for t in active_tools])}"
                else:
                    try:
                        action_input = json.loads(parsed["action_input"])
                        print(f"Вызов инструмента: {parsed['action']}")
                        print(f"Аргументы: {json.dumps(action_input, ensure_ascii=False)}")
                        session = self.mcp_sessions[tool["server"]]
                        print(f"Вызов MCP-сервера: {tool['server']}...")
                        result = await session.call_tool(parsed["action"], action_input)
                        # Извлекаем текст из результата
                        observation = "\n".join([str(item.text) if hasattr(item, 'text') else str(item) for item in result.content])
                        # Извлекаем сырые данные в кеш
                        try:
                            obs_json = json.loads(observation)
                            if "_raw_data" in obs_json:
                                # Сохраняем сырые данные локально для рендеринга виджетов
                                self.data_cache[parsed["action"]] = obs_json["_raw_data"]
                                # Удаляем их из текста, чтобы не тратить токены
                                del obs_json["_raw_data"]
                                observation = json.dumps(obs_json, ensure_ascii=False, indent=2)
                                print(f"[CACHE] Сохранено {len(self.data_cache[parsed['action']])} строк для {parsed['action']}")
                        except json.JSONDecodeError:
                            pass # Если MCP вернул не JSON, просто идём дальше
                        # Показываем сырой ответ от MCP
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
                # Для консоли обрезаем, но для LLM передаём всё
                display_obs = observation if len(observation) < 600 else observation[:600] + "\n...[обрезано для консоли]..."
                print(f"Observation (для консоли):\n{display_obs}")
                print(f"\n{'='*60}")
                print(f"ПЕРЕДАЁТСЯ В LLM КАК OBSERVATION:")
                print(f"Размер: {len(observation)} символов")
                print(f"{'='*60}\n")
                # Передаём полный observation в LLM, без обрезки, LLM видит только Markdown таблицу
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