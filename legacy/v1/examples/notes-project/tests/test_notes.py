import html
from pathlib import Path
import tempfile
import time
import unittest

import api
import storage
import views


class StorageTests(unittest.TestCase):
    def test_persistence_across_reconnect_and_ordered_ids(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "notes.db"
            connection = storage.connect(path)
            try:
                first = storage.add(connection, "First")
                second = storage.add(connection, "Second")
                self.assertIsInstance(first, int)
                self.assertGreater(second, first)
            finally:
                connection.close()
            connection = storage.connect(path)
            try:
                self.assertEqual(storage.all_notes(connection), [(first, "First"), (second, "Second")])
            finally:
                connection.close()


class ViewTests(unittest.TestCase):
    def test_html_is_escaped_and_ids_are_preserved(self):
        dangerous = '<script>alert("x")</script> & "quoted"'
        output = views.render([{"id": 7, "title": dangerous}])
        self.assertIn(html.escape(dangerous), output)
        self.assertNotIn("<script>", output)
        self.assertIn('data-id="7"', output)

    def test_empty_view_is_a_valid_list(self):
        self.assertEqual(views.render([]), "<ul></ul>")


class APITests(unittest.TestCase):
    def test_validated_api_contract_and_view_integration(self):
        connection = storage.connect(":memory:")
        try:
            result = api.create(connection, "  Preserve title  ")
            self.assertEqual(result["title"], "  Preserve title  ")
            self.assertIsInstance(result["id"], int)
            self.assertEqual(api.list_notes(connection), [result])
            self.assertIn("  Preserve title  ", views.render(api.list_notes(connection)))
            for title in ("", " ", "\t\n"):
                with self.assertRaises(ValueError):
                    api.create(connection, title)
        finally:
            connection.close()


class SecurityTests(unittest.TestCase):
    def test_sql_like_title_stays_data_and_html_stays_text(self):
        connection = storage.connect(":memory:")
        try:
            malicious = "Robert'); DROP TABLE notes;-- <script>alert(1)</script>"
            api.create(connection, malicious)
            api.create(connection, "Still usable")
            notes = api.list_notes(connection)
            self.assertEqual([item["title"] for item in notes], [malicious, "Still usable"])
            self.assertNotIn("<script>", views.render(notes))
        finally:
            connection.close()


class WorkloadTests(unittest.TestCase):
    def test_local_500_insert_read_workload(self):
        connection = storage.connect(":memory:")
        started = time.monotonic()
        try:
            for number in range(500):
                api.create(connection, "Note " + str(number))
            self.assertEqual(len(api.list_notes(connection)), 500)
            elapsed = time.monotonic() - started
            print("LOCAL_WORKLOAD count=500 elapsed_seconds=" + str(round(elapsed, 6)))
            self.assertLess(elapsed, 10, "Declared local smoke threshold exceeded")
        finally:
            connection.close()
