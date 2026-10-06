"""Persistence interface; deliberately unfinished evaluation baseline."""


def connect(path):
    raise NotImplementedError("Implement SQLite persistence")


def add(connection, title):
    raise NotImplementedError("Implement a parameterized insert")


def all_notes(connection):
    raise NotImplementedError("Implement ordered notes")
