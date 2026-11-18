"""One runner per physical local SQLite file, independent of path aliases."""
import os
from pathlib import Path
from contextlib import contextmanager

from .models import DataError


@contextmanager
def runner_lock(path):
    path=Path(path).resolve()
    path.parent.mkdir(parents=True,exist_ok=True)
    with path.open('a+b') as stream:
        info=os.fstat(stream.fileno())
        if not info.st_ino or info.st_nlink!=1:
            raise DataError('Database identity ambiguous or hard-linked; use one canonical file')
        mutex=None
        if os.name=='nt':
            import ctypes
            from ctypes import wintypes
            kernel=ctypes.WinDLL('kernel32',use_last_error=True)
            kernel.CreateMutexW.argtypes=[wintypes.LPVOID,wintypes.BOOL,wintypes.LPCWSTR]
            kernel.CreateMutexW.restype=wintypes.HANDLE
            kernel.WaitForSingleObject.argtypes=[wintypes.HANDLE,wintypes.DWORD]
            kernel.ReleaseMutex.argtypes=[wintypes.HANDLE]
            kernel.CloseHandle.argtypes=[wintypes.HANDLE]
            mutex=kernel.CreateMutexW(None,False,f'Global\\GLDRUBF_DB_{info.st_dev}_{info.st_ino}')
            if not mutex:
                raise DataError('Cannot acquire physical database mutex')
            result=kernel.WaitForSingleObject(mutex,0)
            if result not in (0,0x80):  # Acquired, or abandoned by a crashed owner.
                kernel.CloseHandle(mutex)
                raise DataError('Another runner holds this physical database')
        else:
            import fcntl
            try:
                fcntl.flock(stream.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
            except OSError:
                raise DataError('Another runner holds this physical database') from None
        def check():
            try:current=path.stat()
            except OSError:
                raise DataError('Database disappeared while running; manual recovery required') from None
            if (current.st_dev,current.st_ino)!=(info.st_dev,info.st_ino) or current.st_nlink!=1:
                raise DataError('Database replaced/aliased while running; stop before writing')
        try:
            check()
            yield check
        finally:
            if mutex is not None:
                kernel.ReleaseMutex(mutex)
                kernel.CloseHandle(mutex)
            else:
                fcntl.flock(stream.fileno(),fcntl.LOCK_UN)
