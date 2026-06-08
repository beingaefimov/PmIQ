import asyncio
import json
import re
import sys
from typing import List, Dict, Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from openai import OpenAI

# ==============================================================================
# КОНФИГУРАЦИЯ
# ==============================================================================

# 1. Настройки LLM (llama.cpp server)
# По умолчанию llama.cpp запускает OpenAI-совместимый сервер на порту 8080
LLM_BASE_URL = "http://localhost:8080/v1"
LLM_MODEL_NAME = "local-model" # Имя модели не важно для llama.cpp, но требуется клиентом

# 2. Настройки MCP Сервера
# В реальном проекте здесь будет путь к вашему собранному серверу.
# Для примера используем команду, указанную в server.json. 
# Если пакет не опубликован в npm, замените на путь к локальному скрипту, 
# например: sys.executable, "-m", "pm_iq_mcp_server"
MCP_COMMAND = "npx"
MCP_ARGS = ["-y", "@pm-iq/universal", "start"]

# ==============================================================================
# REACT PROMPT TEMPLATE
# ==============================================================================
REACT_PROMPT = """You are an expert Project Management Assistant (PM IQ), aligned with PMBOK 8 principles.
You have access to the following tools to query project management systems:

{tool_descriptions}

Use the following format strictly:
Question: the input question you must answer
Thought: you should always think about what to do and which tool to use
Action: the action to take, MUST be one of [{tool_names}]
Action Input: the input to the action, formatted as a valid JSON string
Observation: the result of the action (this will be provided by the system)
... (this Thought/Action/Action Input/Observation cycle can repeat N times)
Thought: I now know the final answer
Final Answer: the final answer to the original input question

Begin!
Question: {input}
"""

# ==============================================================================
# AGENT CLASS
# ==============================================================================
class PmIqAgent:
    def __init__(self):
        self.llm_client = OpenAI(
            base_url=LLM_BASE_URL,
            api_key="not-needed-for-local-llama-cpp"
        )
        self.tools: List[Dict[str, Any]] = []
        self.tool_map: Dict[str, Any] = {}

    async def initialize_mcp(self):
        """Подключается к MCP серверу и загружает доступные инструменты."""
        print("🔄 Подключение к PM IQ MCP серверу...")
        server_params = StdioServerParameters(
            command=MCP_COMMAND,
            args=MCP_ARGS,
            env={"JIRA_API_URL": "https://demo.jira.com", "MS_PROJECT_TENANT_ID": "demo-tenant"} # Пример переменных окружения
        )
        
        async with stdio_client(server_params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                
                # Получаем список инструментов
                tools_result = await session.list_tools()
                self.tools = tools_result.tools
                
                # Создаем мапу для быстрого вызова
                for tool in self.tools:
                    self.tool_map[tool.name] = lambda args, t=tool: session.call_tool(t.name, args)
                
                print(f"✅ Загружено {len(self.tools)} инструментов из PM IQ.")
                return self.tools

    def _format_tool_descriptions(self) -> str:
        """Форматирует описания инструментов для промпта LLM."""
        descriptions = []
        for tool in self.tools:
            desc = f"- {tool.name}: {tool.description}"
            if tool.inputSchema:
                desc += f" Input schema: {json.dumps(tool.inputSchema)}"
            descriptions.append(desc)
        return "\n".join(descriptions)

    def _parse_llm_response(self, text: str) -> Dict[str, str]:
        """Парсит ответ LLM на предмет вызова инструмента или финального ответа."""
        # Ищем паттерн Action: ... \n Action Input: ...
        action_match = re.search(r"Action:\s*(.*?)\s*Action Input:\s*(.*)", text, re.IGNORECASE | re.DOTALL)
        
        if action_match:
            return {
                "type": "action",
                "action": action_match.group(1).strip(),
                "action_input": action_match.group(2).strip()
            }
        
        # Ищем финальный ответ
        final_match = re.search(r"Final Answer:\s*(.*)", text, re.IGNORECASE | re.DOTALL)
        if final_match:
            return {
                "type": "final_answer",
                "answer": final_match.group(1).strip()
            }
            
        # Если LLM просто рассуждает без действия
        return {"type": "thought", "text": text}

    async def run(self, query: str, max_iterations: int = 5):
        """Основной цикл агента."""
        tool_descriptions = self._format_tool_descriptions()
        tool_names = ", ".join([t.name for t in self.tools])
        
        history = []
        current_prompt = REACT_PROMPT.format(
            tool_descriptions=tool_descriptions,
            tool_names=tool_names,
            input=query
        )
        history.append({"role": "user", "content": current_prompt})

        print(f"\n🤖 Вопрос: {query}\n" + "-"*50)

        for iteration in range(max_iterations):
            print(f"\n[Итерация {iteration + 1}]")
            
            # 1. Запрос к LLM
            print("🧠 LLM думает...")
            response = self.llm_client.chat.completions.create(
                model=LLM_MODEL_NAME,
                messages=history,
                temperature=0.1, # Низкая температура для лучшего следования формату
                max_tokens=1024
            )
            
            llm_text = response.choices[0].message.content
            print(f"📝 Ответ LLM:\n{llm_text}\n" + "-"*30)
            
            # 2. Парсинг ответа
            parsed = self._parse_llm_response(llm_text)
            history.append({"role": "assistant", "content": llm_text})

            # 3. Обработка результата
            if parsed["type"] == "final_answer":
                print(f"\n✅ Финальный ответ:\n{parsed['answer']}")
                return parsed["answer"]
                
            elif parsed["type"] == "action":
                action_name = parsed["action"]
                action_input_str = parsed["action_input"]
                
                if action_name not in self.tool_map:
                    observation = f"Error: Tool '{action_name}' not found. Available tools: {tool_names}"
                else:
                    try:
                        # Парсим JSON input
                        action_input = json.loads(action_input_str)
                        print(f"🔧 Вызов инструмента: {action_name} с аргументами: {action_input}")
                        
                        # Вызов MCP инструмента (требуется активная сессия, поэтому мы немного изменим архитектуру ниже)
                        # Для упрощения примера, мы сделаем вызов внутри контекстного менеджера
                        observation = await self._execute_tool(action_name, action_input)
                    except json.JSONDecodeError:
                        observation = f"Error: Invalid JSON in Action Input. Raw: {action_input_str}"
                    except Exception as e:
                        observation = f"Error executing tool: {str(e)}"
                
                print(f"👁️ Наблюдение (Observation):\n{observation[:500]}...") # Обрезаем для читаемости
                history.append({"role": "user", "content": f"Observation: {observation}"})
                
            else:
                # LLM просто рассуждает, продолжаем цикл
                pass

        print("\n⚠️ Превышено максимальное количество итераций. Агент не смог дать финальный ответ.")
        return "Извините, я не смог обработать ваш запрос за отведенное время."

    async def _execute_tool(self, tool_name: str, args: dict) -> str:
        """Вспомогательный метод для выполнения инструмента в рамках сессии."""
        # TODO: метод _execute_tool содержит заглушку [MOCK OBSERVATION],
        # чтобы скрипт гарантированно запускался без реального npm-пакета.
        # Чтобы сделать его полностью рабочим, нужно вынести
        # async with ClientSession на уровень класса и хранить session как self.session
        return f"[MOCK OBSERVATION] Tool '{tool_name}' executed with args: {args}. (В реальном запуске здесь будет ответ от Jira/SAP/Primavera)"

# ==============================================================================
# MAIN EXECUTION
# ==============================================================================
async def main():
    agent = PmIqAgent()
    
    # Инициализация MCP (в реальном коде здесь нужно сохранить session в self.session)
    # Для полноценной работы см. комментарий в _execute_tool
    await agent.initialize_mcp()
    
    # Примеры запросов
    queries = [
        "Каков текущий статус критического пути по проекту 'Миграция ERP' и есть ли задержки?",
        "Покажи реестр рисков с высоким воздействием для текущего квартала.",
        "Есть ли перегрузка ресурсов в команде разработки на следующей неделе?"
    ]
    
    for q in queries:
        await agent.run(q)
        print("\n" + "="*80 + "\n")

if __name__ == "__main__":
    # Для Windows требуется настройка event loop для asyncio
    if sys.platform == 'win32':
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    
    asyncio.run(main())
