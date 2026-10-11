import os
from pathlib import Path

async def ls(path: str):
    pth = Path(path)
    errors = []
    try:
        os.listdir(path)
    except Exception as e:
        errors.append(e)
    


async def read(path: str):
    pass

async def write(path: str):
    pass

async def edit(path: str):
    pass