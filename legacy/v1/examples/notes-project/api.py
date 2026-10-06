"""Data/API contract; deliberately unfinished evaluation baseline."""


def create(connection, title):
    raise NotImplementedError("Validate and create a note")


def list_notes(connection):
    raise NotImplementedError("Return the agreed note dictionaries")
