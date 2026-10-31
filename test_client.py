"""Regression checks for the Vocareum Chat Completions adapter."""
import json
import io
import sqlite3
import unittest
from contextlib import redirect_stdout
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
from openai import AsyncOpenAI
from mcp.types import TextContent

from starter_client import ChatSession, DataExtractor


class GatewayTests(unittest.IsolatedAsyncioTestCase):
    async def test_multiple_tool_results_before_next_completion(self):
        requests = []

        def respond(request):
            body = json.loads(request.content)
            requests.append(body)
            self.assertEqual(request.url.path, '/v1/chat/completions')
            self.assertEqual(body['model'], 'claude-sonnet-5-5')
            if len(requests) == 1:
                self.assertEqual(body['tools'][0]['function']['parameters']['type'], 'object')
                message = {
                    'role': 'assistant', 'content': None,
                    'tool_calls': [
                        {'id': f'call_{n}', 'type': 'function',
                         'function': {'name': 'scrape_websites', 'arguments': '{}'}}
                        for n in range(2)
                    ],
                }
            else:
                history = body['messages']
                self.assertEqual([m['role'] for m in history], ['system', 'user', 'assistant', 'tool', 'tool'])
                self.assertEqual([m['tool_call_id'] for m in history[3:]], ['call_0', 'call_1'])
                self.assertTrue(all(m['content'] == 'scraped' for m in history[3:]))
                message = {'role': 'assistant', 'content': 'Done'}
            return httpx.Response(200, json={
                'id': 'test', 'object': 'chat.completion', 'created': 0,
                'model': 'claude-sonnet-5-5',
                'choices': [{'index': 0, 'message': message,
                             'finish_reason': 'tool_calls' if len(requests) == 1 else 'stop'}],
            })

        server = SimpleNamespace(name='scraper', execute_tool=AsyncMock(return_value=SimpleNamespace(
            content=[TextContent(type='text', text='scraped')],
        )))
        session = ChatSession([server], 'test-key')
        await session.llm.close()
        session.llm = AsyncOpenAI(
            api_key='test-key', base_url='https://gateway.test/v1',
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(respond)),
        )
        session.available_tools = [{'name': 'scrape_websites', 'description': 'Scrape',
                                    'input_schema': {'type': 'object', 'properties': {}}}]
        session.tool_to_server = {'scrape_websites': 'scraper'}
        session.data_extractor = SimpleNamespace(extract_and_store_data=AsyncMock())
        try:
            await session.process_query('Scrape')
            self.assertEqual(server.execute_tool.await_count, 2)
            session.data_extractor.extract_and_store_data.assert_awaited_once_with('Scrape', 'Done', None)
        finally:
            await session.llm.close()

    async def test_extraction_uses_chat_completion_content(self):
        llm = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=AsyncMock(
            return_value=SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=' {"plans": []} '))]),
        ))))
        extractor = DataExtractor(None, llm, 'claude-sonnet-5-5')
        result = await extractor._get_structured_extraction('Extract prices')
        self.assertEqual(json.loads(result), {'plans': []})
        self.assertEqual(llm.chat.completions.create.call_args.kwargs['model'], 'claude-sonnet-5-5')

    async def test_prices_with_apostrophes_and_nulls_are_saved_and_displayed(self):
        db = sqlite3.connect(':memory:')
        db.row_factory = sqlite3.Row

        async def execute(name, args):
            cursor = db.execute(args['query'])
            rows = [dict(row) for row in cursor.fetchall()] if cursor.description else []
            return SimpleNamespace(content=[TextContent(type='text', text=json.dumps(rows))], isError=False)

        server = SimpleNamespace(execute_tool=execute)
        extractor = DataExtractor(server, None, 'claude-sonnet-5-5')
        await extractor.setup_data_tables()
        extractor._get_structured_extraction = AsyncMock(return_value=json.dumps({'plans': [
            {'company_name': "Provider's AI", 'plan_name': 'DeepSeek V3',
             'input_tokens': 0.25, 'output_tokens': None, 'billing_period': 'per 1M tokens'},
            {'company_name': 'Other provider', 'plan_name': 'DeepSeek V3',
             'input_tokens': 0.30, 'output_tokens': 0.80, 'billing_period': 'per 1M tokens'},
        ]}))
        try:
            stored = await extractor.extract_and_store_data("Compare provider's costs", 'Prices')
            self.assertEqual(stored, 2)
            rows = db.execute('SELECT * FROM pricing_plans ORDER BY id').fetchall()
            self.assertEqual(rows[0]['company_name'], "Provider's AI")
            self.assertIsNone(rows[0]['output_tokens'])
            self.assertEqual(rows[1]['company_name'], 'Other provider')
            self.assertEqual(rows[0]['source_query'], "Compare provider's costs")
            session = ChatSession([], 'test-key')
            session.sqlite_server = server
            try:
                output = io.StringIO()
                with redirect_stdout(output):
                    await session.show_stored_data()
                self.assertIn("Provider's AI", output.getvalue())
                self.assertIn('Recently Stored Data:', output.getvalue())
                self.assertIn('Pricing Plans:', output.getvalue())
                header = next(line for line in output.getvalue().splitlines() if 'Input Tokens' in line)
                self.assertEqual([cell.strip() for cell in header.split('|')],
                                 ['Company', 'Plan', 'Input Tokens', 'Output Tokens'])
                provider_row = next(line for line in output.getvalue().splitlines() if "\u2022 Provider's AI" in line)
                self.assertEqual([cell.strip() for cell in provider_row.split('|')],
                                 ["\u2022 Provider's AI", 'DeepSeek V3', '$0.25', 'not listed'])
                separators = [line for line in output.getvalue().splitlines() if line and set(line) == {'-'}]
                self.assertEqual(len(separators), 2)
                self.assertEqual(separators[0], separators[1])
                self.assertEqual(output.getvalue().count('=' * 50), 2)
                self.assertNotIn('Error', output.getvalue())
            finally:
                await session.llm.close()
        finally:
            db.close()

    async def test_recent_data_query_limits_rows_and_uses_creation_time(self):
        db = sqlite3.connect(':memory:')
        db.row_factory = sqlite3.Row
        db.execute('CREATE TABLE pricing_plans (company_name, plan_name, input_tokens, output_tokens, currency, created_at)')
        # Insertion order deliberately differs from timestamp order.
        for day in (7, 1, 6, 2, 5, 3, 4):
            db.execute('INSERT INTO pricing_plans VALUES (?, ?, ?, ?, ?, ?)',
                       (f'Provider {day}', 'Plan', 0.1, 0.2, 'USD', f'2026-10-{day:02d}'))

        async def execute(name, args):
            self.assertEqual(name, 'read_query')
            self.assertEqual(args['query'], 'SELECT company_name, plan_name, input_tokens, output_tokens, currency FROM pricing_plans ORDER BY created_at DESC LIMIT 5')
            rows = [dict(row) for row in db.execute(args['query'])]
            return SimpleNamespace(content=[TextContent(type='text', text=json.dumps(rows))], isError=False)

        session = ChatSession([], 'test-key')
        session.sqlite_server = SimpleNamespace(execute_tool=execute)
        try:
            output = io.StringIO()
            with redirect_stdout(output):
                await session.show_stored_data()
            text = output.getvalue()
            self.assertEqual(text.count('\u2022'), 5)
            self.assertLess(text.index('Provider 7'), text.index('Provider 6'))
            self.assertNotIn('Provider 1', text)
            self.assertNotIn('Provider 2', text)
            self.assertTrue(text.rstrip().endswith('=' * 50))
        finally:
            await session.llm.close()
            db.close()


if __name__ == '__main__':
    unittest.main()
