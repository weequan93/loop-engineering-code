import unittest

from slug import slug


class TestSlug(unittest.TestCase):
    def test_whitespace_only(self):
        for text in ("", " ", "   ", "\t", "\n", " \t\n "):
            with self.subTest(text=text):
                self.assertEqual(slug(text), "")

    def test_existing_behavior(self):
        for text, expected in (("Hello World", "hello-world"), ("a  b", "a--b"),
                               ("a\tb", "a\tb"), ("Hello", "hello"), (" Hello ", "-hello-")):
            with self.subTest(text=text):
                self.assertEqual(slug(text), expected)
