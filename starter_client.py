import asyncio
import json
import logging
import os
import shutil
from contextlib import AsyncExitStack
from typing import Any, List, Dict, TypedDict
from datetime import datetime, timedelta
from pathlib import Path
import re

from dotenv import load_dotenv
from openai import AsyncOpenAI
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

class ToolDefinition(TypedDict):
    name: str
    description: str
    input_schema: dict


class Configuration:
    """Manages configuration and environment variables for the MCP client."""

    def __init__(self) -> None:
        """Initialize configuration with environment variables."""
        self.load_env()
        self.api_key = os.getenv("OPENAI_API_KEY") or os.getenv("ANTHROPIC_API_KEY")

    @staticmethod
    def load_env() -> None:
        """Load environment variables from .env file."""
        load_dotenv(Path(__file__).with_name(".env"))

    @staticmethod
    def load_config(file_path: str | Path) -> dict[str, Any]:
        """Load server configuration from JSON file.

        Args:
            file_path: Path to the JSON configuration file.

        Returns:
            Dict containing server configuration.

        Raises:
            FileNotFoundError: If configuration file doesn't exist.
            JSONDecodeError: If configuration file is invalid JSON.
            ValueError: If configuration file is missing required fields.
        """
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                config = json.load(f)
        except FileNotFoundError:
            raise FileNotFoundError(f"Configuration file not found: {file_path}")
        except json.JSONDecodeError as e:
            raise json.JSONDecodeError(f"Invalid JSON in configuration file: {e.msg}", e.doc, e.pos)

        if "mcpServers" not in config:
            raise ValueError("Configuration file is missing required 'mcpServers' field")

        return config

    @property
    def llm_api_key(self) -> str:
        """Get the Vocareum API key.

        Returns:
            The API key as a string.

        Raises:
            ValueError: If the API key is not found in environment variables.
        """
        if not self.api_key:
            raise ValueError("Set OPENAI_API_KEY to your Vocareum key in .env")
        return self.api_key


class Server:
    """Manages MCP server connections and tool execution."""

    def __init__(self, name: str, config: dict[str, Any]) -> None:
        self.name: str = name
        self.config: dict[str, Any] = config
        self.stdio_context: Any | None = None
        self.session: ClientSession | None = None
        self._cleanup_lock: asyncio.Lock = asyncio.Lock()
        self.exit_stack: AsyncExitStack = AsyncExitStack()

    async def initialize(self) -> None:
        """Initialize the server connection."""
        command = shutil.which("npx") if self.config["command"] == "npx" else self.config["command"]
        if command is None:
            raise ValueError("The command must be a valid string and cannot be None.")

        server_params = StdioServerParameters(
            command=command,
            args=self.config["args"],
            env={**os.environ, **self.config["env"]} if self.config.get("env") else None,
        )
        try:
            stdio_transport = await self.exit_stack.enter_async_context(stdio_client(server_params))
            read, write = stdio_transport
            session = await self.exit_stack.enter_async_context(ClientSession(read, write))
            await session.initialize()
            self.session = session
            logging.info(f"✓ Server '{self.name}' initialized")
        except Exception as e:
            logging.error(f"Error initializing server {self.name}: {e}")
            await self.cleanup()
            raise

    async def list_tools(self) -> List[ToolDefinition]:
        """List available tools from the server.

        Returns:
            A list of available tool definitions.

        Raises:
            RuntimeError: If the server is not initialized.
        """
        if not self.session:
            raise RuntimeError(f"Server {self.name} not initialized")

        tools_response = await self.session.list_tools()
        tools : List[ToolDefinition] = []

        for item in tools_response:
            if isinstance(item, tuple) and item[0] == "tools":
                for tool in item[1]:
                    tool_def: ToolDefinition = {
                        "name": tool.name,
                        "description": tool.description,
                        "input_schema": tool.inputSchema
                    }
                    tools.append(tool_def)

        return tools

    async def execute_tool(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        retries: int = 2,
        delay: float = 1.0,
    ) -> Any:
        """Execute a tool with retry mechanism.

        Args:
            tool_name: Name of the tool to execute.
            arguments: Tool arguments.
            retries: Number of retry attempts.
            delay: Delay between retries in seconds.

        Returns:
            Tool execution result.

        Raises:
            RuntimeError: If server is not initialized.
            Exception: If tool execution fails after all retries.
        """
        if not self.session:
            raise RuntimeError(f"Server {self.name} not initialized")

        attempt = 0
        while attempt < retries:
            try:
                logging.info(f" Executing {tool_name}...")
                result = await self.session.call_tool(
                    name=tool_name,
                    arguments=arguments,
                    read_timeout_seconds=timedelta(seconds=60),
                )
                return result
            except Exception as e:
                attempt += 1
                logging.warning(f"Error executing tool: {e}. Attempt {attempt} of {retries}")
                if attempt < retries:
                    logging.info(f"Retrying in {delay} seconds....")
                    await asyncio.sleep(delay)
                else:
                    logging.error("Max retries reached. Failing")
                    raise

    async def cleanup(self) -> None:
        """Clean up server resources."""
        async with self._cleanup_lock:
            try:
                await self.exit_stack.aclose()
                self.session = None
                self.stdio_context = None
            except Exception as e:
                logging.error(f"Error during cleanup of server {self.name}: {e}")


class DataExtractor:
    """Handles extraction and storage of structured data from LLM responses."""
    
    def __init__(self, sqlite_server: Server, llm_client: AsyncOpenAI, model: str):
        self.sqlite_server = sqlite_server
        self.llm = llm_client
        self.model = model
        
    async def setup_data_tables(self) -> None:
        """Setup tables for storing extracted data."""
        try:
            
            await self.sqlite_server.execute_tool("write_query", {
                "query": """
                CREATE TABLE IF NOT EXISTS pricing_plans (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    company_name TEXT NOT NULL,
                    plan_name TEXT NOT NULL,
                    input_tokens REAL,
                    output_tokens REAL,
                    currency TEXT DEFAULT 'USD',
                    billing_period TEXT,  -- 'monthly', 'yearly', 'one-time'
                    features TEXT,  -- JSON array
                    limitations TEXT,
                    source_query TEXT,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
                )
                """
            })
            
            logging.info("✓ Data extraction tables initialized")
            
        except Exception as e:
            logging.error(f"Failed to setup data tables: {e}")

    async def _get_structured_extraction(self, prompt: str) -> str:
        """Use Claude to extract structured data."""
        try:
            response = await self.llm.chat.completions.create(
                max_tokens=1024,
                model=self.model,
                messages=[{'role': 'user', 'content': prompt}]
            )
            
            return (response.choices[0].message.content or "").strip()
            
        except Exception as e:
            logging.error(f"Error in structured extraction: {e}")
            return '{"error": "extraction failed"}'
    
    @staticmethod
    def _sql_value(value: Any) -> str:
        """Quote values for the SQLite MCP tool, which accepts SQL text."""
        if value is None:
            return 'NULL'
        if isinstance(value, (int, float)):
            return str(value)
        return "'" + str(value).replace("'", "''") + "'"

    async def extract_and_store_data(self, user_query: str, llm_response: str,
                                   source_url: str = None) -> int:
        """Extract prices from the answer and save each provider separately."""
        try:
            extraction_prompt = f"""
            Extract only explicitly stated pricing information from this text:
            {llm_response}

            Return JSON with a "plans" array. Each plan has:
            company_name, plan_name, input_tokens (number or null),
            output_tokens (number or null), currency, billing_period,
            features (array of strings), limitations.
            Include separate plans for different companies and models.
            Preserve the quoted price units in billing_period (for example,
            "per 1 million tokens"). Never invent missing prices; use null.
            If there are no explicit prices, return {{"plans": []}}.
            Return only valid JSON, without Markdown fences.
            """
            raw = await self._get_structured_extraction(extraction_prompt)
            raw = re.sub(r'^```(?:json)?\s*|\s*```$', '', raw.strip())
            pricing_data = json.loads(raw)
            if pricing_data.get('error'):
                raise ValueError(pricing_data['error'])
            plans = pricing_data.get('plans', [])
            if not isinstance(plans, list):
                raise ValueError('Expected a plans array')
            stored = 0
            for plan in plans:
                if plan.get('input_tokens') is None and plan.get('output_tokens') is None:
                    continue
                values = [
                    plan.get('company_name') or pricing_data.get('company_name', 'unknown'),
                    plan.get('plan_name', 'unknown plan'),
                    plan.get('input_tokens'), plan.get('output_tokens'),
                    plan.get('currency', 'USD'), plan.get('billing_period'),
                    json.dumps(plan.get('features', [])), plan.get('limitations'),
                    user_query,
                ]
                query = (
                    'INSERT INTO pricing_plans '
                    '(company_name, plan_name, input_tokens, output_tokens, currency, '
                    'billing_period, features, limitations, source_query) VALUES ('
                    + ', '.join(self._sql_value(v) for v in values) + ')'
                )
                result = await self.sqlite_server.execute_tool('write_query', {'query': query})
                if getattr(result, 'isError', False):
                    raise RuntimeError('SQLite rejected pricing insert: ' + str(result.content))
                stored += 1
            logger.info('Stored %s pricing plans', stored)
            if not stored:
                print('No explicit token prices found to save. Ask a pricing question using the scraped content.')
            return stored
        except Exception as exc:
            logger.error('Error extracting or saving pricing data: %s', exc)
            return 0


class ChatSession:
    """Orchestrates the interaction between user, LLM, and tools."""

    def __init__(self, servers: list[Server], api_key: str) -> None:
        self.servers: list[Server] = servers
        self.model = os.getenv("OPENAI_MODEL") or "claude-sonnet-5-5"
        self.llm = AsyncOpenAI(
            api_key=api_key,
            base_url=os.getenv("OPENAI_BASE_URL") or "https://openai.vocareum.com/v1",
            timeout=60,
            max_retries=0,
        )
        self.available_tools: List[ToolDefinition] = []
        self.tool_to_server: Dict[str, str] = {}
        self.sqlite_server: Server | None = None
        self.data_extractor: DataExtractor | None = None

    async def cleanup_servers(self) -> None:
        """Clean up all servers properly."""
        for server in reversed(self.servers):
            try:
                await server.cleanup()
            except Exception as e:
                logging.warning(f"Warning during final cleanup: {e}")

    async def process_query(self, query: str) -> None:
        """Run Chat Completions and return MCP tool results to the model."""
        messages = [
            {'role': 'system', 'content': 'For pricing questions, retrieve saved website content using extract_scraped_info, or scrape the source if needed. Base comparisons on retrieved prices, include provider, model, input and output prices and units. Never invent missing prices.'},
            {'role': 'user', 'content': query},
        ]
        tools = [
            {
                'type': 'function',
                'function': {
                    'name': tool['name'],
                    'description': tool['description'],
                    'parameters': tool['input_schema'],
                },
            }
            for tool in self.available_tools
        ]
        full_response = ''
        source_url = None

        for _ in range(20):
            response = await self.llm.chat.completions.create(
                model=self.model,
                max_tokens=2024,
                messages=messages,
                **({'tools': tools} if tools else {}),
            )
            message = response.choices[0].message
            if message.content:
                print(message.content)
                full_response += message.content + '\n'
            if not message.tool_calls:
                break

            # Append every call before sending any of their results.
            messages.append({
                'role': 'assistant',
                'content': message.content,
                'tool_calls': [call.model_dump(exclude_none=True) for call in message.tool_calls],
            })
            for call in message.tool_calls:
                tool_name = call.function.name
                server_name = self.tool_to_server.get(tool_name)
                server = next((s for s in self.servers if s.name == server_name), None)
                try:
                    if server is None:
                        raise ValueError(f'Unknown tool: {tool_name}')
                    arguments = json.loads(call.function.arguments)
                    print(f'Calling tool {tool_name}')
                    result = await server.execute_tool(tool_name, arguments)
                    parts = []
                    for block in result.content:
                        if getattr(block, 'type', None) == 'text':
                            parts.append(block.text)
                        else:
                            parts.append(json.dumps(block.model_dump(mode='json')))
                    result_text = '\n'.join(parts)
                    source_url = source_url or self._extract_url_from_result(result_text)
                except Exception as exc:
                    result_text = json.dumps({'error': str(exc)})
                messages.append({
                    'role': 'tool',
                    'tool_call_id': call.id,
                    'content': result_text,
                })
        else:
            raise RuntimeError('Stopped after 20 tool rounds; narrow your query and try again.')

        if self.data_extractor and full_response.strip():
            await self.data_extractor.extract_and_store_data(query, full_response.strip(), source_url)

    def _extract_url_from_result(self, result_text: str) -> str | None:
        """Extract URL from tool result."""
        url_pattern = r'https?://[^\s<>"{}|\\^`\[\]]+'
        urls = re.findall(url_pattern, result_text)
        return urls[0] if urls else None

    async def chat_loop(self) -> None:
        """Run an interactive chat loop."""
        print("\nMCP Chatbot with Data Extraction Started!")
        print("Type your queries, 'show data' to view stored data, or 'quit' to exit.")
        
        while True:
            try:
                query = input("\nQuery: ").strip()
        
                if not query:
                    continue
                if query.lower() == 'quit':
                    break
                elif query.lower() == 'show data':
                    await self.show_stored_data()
                    continue
                    
                await self.process_query(query)
                print("\n")
                    
            except KeyboardInterrupt:
                print("\nExiting...")
                break
            except Exception as e:
                print(f"\nError: {str(e)}")

    async def show_stored_data(self) -> None:
        """Display five recent plans with column headers and separators."""
        if not self.sqlite_server:
            print('No database available')
            return
        try:
            result = await self.sqlite_server.execute_tool('read_query', {
                'query': 'SELECT company_name, plan_name, input_tokens, output_tokens, '
                         'currency FROM pricing_plans ORDER BY created_at DESC LIMIT 5'
            })
            if getattr(result, 'isError', False):
                raise RuntimeError(str(result.content))
            rows = []
            for block in result.content:
                raw = block.get('text', '') if isinstance(block, dict) else getattr(block, 'text', '')
                if not raw:
                    continue
                try:
                    parsed = json.loads(raw)
                except json.JSONDecodeError:
                    import ast
                    parsed = ast.literal_eval(raw)
                if not isinstance(parsed, list):
                    raise ValueError('Expected a list of database rows')
                rows.extend(parsed)
            print('\nRecently Stored Data:')
            print('=' * 50)
            print('\nPricing Plans:')
            headers = ['Company', 'Plan', 'Input Tokens', 'Output Tokens']
            display_rows = []
            for plan in rows:
                input_price = f"${plan['input_tokens']}" if plan.get('input_tokens') is not None else 'not listed'
                output_price = f"${plan['output_tokens']}" if plan.get('output_tokens') is not None else 'not listed'
                display_rows.append([str(plan['company_name']), str(plan['plan_name']), input_price, output_price])
            widths = [max(len(row[i]) for row in [headers] + display_rows) for i in range(len(headers))]
            print('    ' + ' | '.join(value.ljust(width) for value, width in zip(headers, widths)))
            separator = '-' * (4 + sum(widths) + 3 * (len(headers) - 1))
            print(separator)
            if not display_rows:
                print('No pricing plans saved yet. Run a pricing comparison using the scraped content first.')
            for row in display_rows:
                print('  \u2022 ' + ' | '.join(value.ljust(width) for value, width in zip(row, widths)))
            print(separator)
            print('=' * 50)
        except Exception as exc:
            print(f'Error showing data: {exc}')

    async def start(self) -> None:
        """Main chat session handler."""
        try:
            for server in self.servers:
                try:
                    await server.initialize()
                    if "sqlite" in server.name.lower():
                        self.sqlite_server = server
                except Exception as e:
                    logging.error(f"Failed to initialize server: {e}")
                    await self.cleanup_servers()
                    return

            for server in self.servers:
                tools = await server.list_tools()
                self.available_tools.extend(tools)
                for tool in tools:
                    self.tool_to_server[tool["name"]] = server.name

            print(f"\nConnected to {len(self.servers)} server(s)")
            print(f"Available tools: {[tool['name'] for tool in self.available_tools]}")
            
            if self.sqlite_server:
                self.data_extractor = DataExtractor(self.sqlite_server, self.llm, self.model)
                await self.data_extractor.setup_data_tables()
                print("Data extraction enabled")

            await self.chat_loop()

        finally:
            await self.cleanup_servers()
            await self.llm.close()


async def main() -> None:
    """Initialize and run the chat session."""
    config = Configuration()
    
    script_dir = Path(__file__).parent
    config_file = script_dir / "server_config.json"
    
    server_config = config.load_config(config_file)
    
    servers = [Server(name, srv_config) for name, srv_config in server_config["mcpServers"].items()]
    chat_session = ChatSession(servers, config.llm_api_key)
    await chat_session.start()


if __name__ == "__main__":
    asyncio.run(main())
