"""Tests de la Pipe NAO Brain (sans Open WebUI ni body-gateway).

    cd robotics/brain/webui && python3 -m unittest discover -s tests -v
"""

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import nao_pipe  # noqa: E402

TAGS_PROMPT = (
    "### Task: Generate 1-3 broad tags categorizing the main themes of the chat "
    'history ### Output: JSON format: { "tags": ["tag1", "tag2", "tag3"] } '
    "### Chat History: <chat_history> USER: Hello NAO </chat_history>"
)


def _body(text):
    return {"messages": [{"role": "user", "content": text}]}


class TestBackgroundTasks(unittest.TestCase):

    def setUp(self):
        self.pipe = nao_pipe.Pipe()
        patcher = mock.patch.object(nao_pipe.requests, "post")
        self.post = patcher.start()
        self.addCleanup(patcher.stop)

    def test_task_argument_never_reaches_gateway(self):
        for task in ("title_generation", "tags_generation", "follow_up_generation",
                     "query_generation", "something_new"):
            self.pipe.pipe(_body("Hello NAO"), __task__=task)
        self.post.assert_not_called()

    def test_task_argument_replies(self):
        self.assertEqual(self.pipe.pipe(_body("x"), __task__="tags_generation"),
                         '{"tags": ["General"]}')
        self.assertEqual(self.pipe.pipe(_body("x"), __task__="unknown_task"), "")

    def test_task_prompt_without_task_argument(self):
        reply = self.pipe.pipe(_body(TAGS_PROMPT))
        self.assertEqual(reply, '{"tags": ["General"]}')
        self.post.assert_not_called()

    def test_title_prompt_without_task_argument(self):
        reply = self.pipe.pipe(_body("### Task:\nGenerate a concise title for the chat"))
        self.assertIn('"title"', reply)
        self.post.assert_not_called()

    def test_regular_message_reaches_gateway(self):
        self.post.return_value.json.return_value = {"speech": "Salut !", "actions": []}
        reply = self.pipe.pipe(_body("Hello NAO comment vas tu ?"))
        self.post.assert_called_once()
        self.assertEqual(self.post.call_args.kwargs["json"]["text"], "Hello NAO comment vas tu ?")
        self.assertIn("**NAO :** Salut !", reply)

    def test_message_mentioning_task_is_regular(self):
        self.post.return_value.json.return_value = {"speech": "Oui", "actions": []}
        self.pipe.pipe(_body("NAO, quelle est ta ### Task: du jour ?"))
        self.post.assert_called_once()


if __name__ == "__main__":
    unittest.main()
