"""Deterministic proposals outside the example workspace; no model claims."""

STORAGE = '''import sqlite3


def connect(path):
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE IF NOT EXISTS notes (id INTEGER PRIMARY KEY, title TEXT NOT NULL)")
    connection.commit()
    return connection


def add(connection, title):
    cursor = connection.execute("INSERT INTO notes(title) VALUES (?)", (title,))
    connection.commit()
    return cursor.lastrowid


def all_notes(connection):
    return connection.execute("SELECT id, title FROM notes ORDER BY id").fetchall()
'''

VIEWS = '''from html import escape


def render(notes):
    return "<ul>" + "".join('<li data-id="' + str(item["id"]) + '">' + escape(item["title"]) + "</li>" for item in notes) + "</ul>"
'''

API = '''import storage


def create(connection, title):
    if not isinstance(title, str) or not title.strip():
        raise ValueError("A nonempty title is required")
    return {"id": storage.add(connection, title), "title": title}


def list_notes(connection):
    return [{"id": item[0], "title": item[1]} for item in storage.all_notes(connection)]
'''

INCOMPATIBLE_API = API.replace('"title": item[1]', '"note_text": item[1]')
