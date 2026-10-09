"""Tests sans réseau des règles du tchat Twitch.

    cd robotics/brain/twitch_bridge && python3 -m unittest discover -s tests -v
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import twitch_chat  # noqa: E402

LINE = ("@badges=subscriber/12;display-name=Joa\\sM;mod=0 "
        ":joa!joa@joa.tmi.twitch.tv PRIVMSG #monolithoid :!nao salut, ça va ?\r\n")


def msg(text, user="joa", t=0.0, mod="0"):
    return twitch_chat.ChatMessage(user=user, display_name=user, channel="c",
                                   text=text, tags={"mod": mod}, received_at=t)


class TestParse(unittest.TestCase):

    def test_privmsg_with_tags(self):
        m = twitch_chat.parse_privmsg(LINE, now=1.0)
        self.assertEqual(m.user, "joa")
        self.assertEqual(m.display_name, "Joa M")
        self.assertEqual(m.channel, "monolithoid")
        self.assertEqual(m.text, "!nao salut, ça va ?")
        self.assertFalse(m.is_mod)

    def test_other_lines_ignored(self):
        self.assertIsNone(twitch_chat.parse_privmsg("PING :tmi.twitch.tv"))
        self.assertIsNone(twitch_chat.parse_privmsg(":tmi.twitch.tv 001 justinfan1 :Welcome"))

    def test_broadcaster_is_mod(self):
        line = LINE.replace("badges=subscriber/12", "badges=broadcaster/1")
        self.assertTrue(twitch_chat.parse_privmsg(line).is_mod)


class TestCommand(unittest.TestCase):

    def test_extract(self):
        ex = twitch_chat.extract_command
        self.assertEqual(ex("!nao salut"), "salut")
        self.assertEqual(ex("!NAO, ça va ?"), "ça va ?")
        self.assertEqual(ex("\x01ACTION !nao danse\x01"), "danse")
        self.assertIsNone(ex("!nao"))
        self.assertIsNone(ex("!naomi salut"))
        self.assertIsNone(ex("salut !nao"))


class TestFilter(unittest.TestCase):

    def test_accepts_and_cooldown(self):
        f = twitch_chat.ChatFilter(user_cooldown_s=30)
        self.assertEqual(f.check(msg("!nao salut"), now=0), ("salut", "ok"))
        self.assertEqual(f.check(msg("!nao encore"), now=10)[1], "délai par spectateur")
        self.assertEqual(f.check(msg("!nao autre", user="bob"), now=10)[0], "autre")
        self.assertEqual(f.check(msg("!nao encore"), now=31)[0], "encore")

    def test_rejected_message_does_not_start_cooldown(self):
        f = twitch_chat.ChatFilter(blocklist=["Gros-Mot"])
        self.assertEqual(f.check(msg("!nao un gros mot"), now=0)[1], "mot interdit")
        self.assertEqual(f.check(msg("!nao poli"), now=1)[0], "poli")

    def test_links_bots_mods(self):
        f = twitch_chat.ChatFilter(ignored_users=["Nightbot"])
        self.assertEqual(f.check(msg("!nao va sur www.site.com"))[1], "lien")
        self.assertEqual(f.check(msg("!nao regarde https://x.y"))[1], "lien")
        self.assertEqual(f.check(msg("!nao salut", user="nightbot"))[1], "utilisateur ignoré")
        mods = twitch_chat.ChatFilter(mods_only=True)
        self.assertEqual(mods.check(msg("!nao salut"))[1], "réservé aux modérateurs")
        self.assertEqual(mods.check(msg("!nao salut", mod="1"))[0], "salut")

    def test_truncate(self):
        f = twitch_chat.ChatFilter(max_chars=12)
        q, _ = f.check(msg("!nao un deux trois quatre"))
        self.assertEqual(q, "un deux…")


class TestQueue(unittest.TestCase):

    def test_burst_keeps_newest(self):
        q = twitch_chat.LatestQueue(maxlen=2, max_age_s=90)
        for i in range(4):
            q.push(msg(f"!nao {i}", t=0), str(i))
        self.assertEqual(q.pop(timeout=0, now=1)[1], "2")
        self.assertEqual(q.pop(timeout=0, now=1)[1], "3")
        self.assertIsNone(q.pop(timeout=0, now=1))

    def test_old_messages_dropped(self):
        q = twitch_chat.LatestQueue(maxlen=5, max_age_s=60)
        q.push(msg("!nao vieux", t=0), "vieux")
        q.push(msg("!nao récent", t=50), "récent")
        self.assertEqual(q.pop(timeout=0, now=100)[1], "récent")

    def test_prompt(self):
        self.assertEqual(twitch_chat.brain_prompt(msg("x", user="Joa"), "salut"),
                         "[tchat Twitch] Joa : salut")


if __name__ == "__main__":
    unittest.main()
