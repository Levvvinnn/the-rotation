"""Language fallback request/validation checks, with no external API access."""
import json
import os
import unittest
from unittest.mock import AsyncMock, Mock, patch

from rotation.understand import Intent, _validated_intent, understand


class LanguageAITest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.env = patch.dict(os.environ, {'GEMINI_API_KEY': 'test-only', 'GEMINI_MODEL': 'gemini-2.5-flash-lite'})
        self.env.start()
        self.addCleanup(self.env.stop)

    def response(self, output, status=200):
        response = Mock(status=status)
        response.json = AsyncMock(return_value={
            'candidates': [{'content': {'parts': [{'text': json.dumps(output)}]}}]})
        request = Mock()
        request.__aenter__ = AsyncMock(return_value=response)
        request.__aexit__ = AsyncMock(return_value=False)
        session = Mock()
        session.post.return_value = request
        context = Mock()
        context.__aenter__ = AsyncMock(return_value=session)
        context.__aexit__ = AsyncMock(return_value=False)
        return context, session

    async def test_paraphrase_reaches_gemini_with_schema_and_custom_tasks(self):
        output = dict(kind='did', task='cooking', amount=None, note='', confidence=.98)
        context, session = self.response(output)
        with patch('rotation.understand.aiohttp.ClientSession', return_value=context):
            got = await understand('I whipped up dinner for everyone', ['clean pool'])
        self.assertEqual(got, Intent('did', 'cooking', None, ''))
        url = session.post.call_args.args[0]
        kwargs = session.post.call_args.kwargs
        self.assertEqual(url, 'https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash-lite:generateContent')
        self.assertEqual(kwargs['headers']['x-goog-api-key'], 'test-only')
        self.assertNotIn('test-only', url)
        self.assertIn('clean pool', kwargs['json']['systemInstruction']['parts'][0]['text'])
        self.assertEqual(kwargs['json']['generationConfig']['responseMimeType'], 'application/json')

    async def test_local_conjugation_avoids_api(self):
        with patch('rotation.understand.aiohttp.ClientSession') as client:
            got = await understand("I've driven today")
        self.assertEqual((got.kind, got.task), ('did', 'drive'))
        client.assert_not_called()

    async def test_api_error_and_malformed_json_do_not_log(self):
        for status, output in [(429, {}), (200, []), (200, {'kind': 'did'})]:
            context, _ = self.response(output, status)
            with patch('rotation.understand.aiohttp.ClientSession', return_value=context):
                self.assertIsNone(await understand('dinner was on me'))

    async def test_timeout_does_not_log(self):
        context, session = self.response({})
        session.post.side_effect = TimeoutError
        with patch('rotation.understand.aiohttp.ClientSession', return_value=context):
            self.assertIsNone(await understand('dinner was on me'))

    async def test_chatter_and_overlong_messages_do_not_call_api(self):
        with patch('rotation.understand.aiohttp.ClientSession') as client:
            for text in ['', 'lmao', 'great movie', 'I drove ' + 'x' * 2000]:
                self.assertIsNone(await understand(text))
        client.assert_not_called()


class ModelValidationTest(unittest.TestCase):
    def output(self, **overrides):
        return dict(dict(kind='did', task='cooking', amount=None, note='', confidence=.98), **overrides)

    def test_money_must_match_explicit_message_amount(self):
        self.assertEqual(_validated_intent(self.output(task='pay', amount=48), 'dinner was on me, $48').cents, 4800)
        for output, text in [(self.output(amount=48), 'I cooked'),
                             (self.output(amount=20), 'dinner was on me, $48'),
                             (self.output(), 'dinner was on me, $48'),
                             (self.output(amount=-5), 'dinner was on me'),
                             (self.output(amount=True), 'dinner was on me')]:
            self.assertIsNone(_validated_intent(output, text))

    def test_low_confidence_and_invalid_fields_rejected(self):
        for overrides in [{'confidence': .8}, {'confidence': True}, {'confidence': float('nan')},
                          {'kind': 'delete'}, {'task': '@everyone'}, {'task': []}]:
            self.assertIsNone(_validated_intent(self.output(**overrides), 'I cooked'))

    def test_doing_and_canonical_task(self):
        got = _validated_intent(self.output(kind='doing', task='cooked'), 'Im on dinner duty today')
        self.assertEqual((got.kind, got.task, got.cents), ('doing', 'cooking', None))
