"""Tests de la personnalité lue depuis un fichier.

    cd robotics/brain/body_gateway && python3 -m unittest discover -s tests -v
"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import persona  # noqa: E402


class TestLoadPersona(unittest.TestCase):

    def _file(self, content: str) -> str:
        f = tempfile.NamedTemporaryFile("w", suffix=".md", delete=False, encoding="utf-8")
        f.write(content)
        f.close()
        self.addCleanup(os.unlink, f.name)
        return f.name

    def test_reads_file(self):
        path = self._file("  Tu es Bob, un robot grognon.\n\nTu détestes les lundis.\n")
        self.assertEqual(persona.load_persona(path),
                         "Tu es Bob, un robot grognon.\n\nTu détestes les lundis.")

    def test_default_without_path(self):
        self.assertEqual(persona.load_persona(""), persona.DEFAULT_PERSONA)

    def test_default_when_missing(self):
        self.assertEqual(persona.load_persona("/nonexistent/personnalite.md"),
                         persona.DEFAULT_PERSONA)

    def test_default_when_empty(self):
        self.assertEqual(persona.load_persona(self._file(" \n\n")), persona.DEFAULT_PERSONA)

    def test_default_when_directory(self):
        # Docker crée un dossier à la place d'un fichier monté absent.
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(persona.load_persona(d), persona.DEFAULT_PERSONA)

    def test_default_starts_with_nao(self):
        self.assertTrue(persona.DEFAULT_PERSONA.startswith("Tu es NAO"))
        self.assertNotIn("{", persona.DEFAULT_PERSONA)


if __name__ == "__main__":
    unittest.main()
