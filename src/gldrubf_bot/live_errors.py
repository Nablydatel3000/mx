"""Boundary certificate raised ONLY before the transport callable is entered."""
from .models import DataError


class DefinitelyNotSent(DataError):
    pass
