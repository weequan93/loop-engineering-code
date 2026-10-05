"""Deliberately buggy sample used for local and live integration exercises."""


def slug(text: str) -> str:
    return "-".join(text.lower().split(" "))
