"""ASGI entrypoint: uvicorn hvac_engine.main:app"""
from .api import create_app

app = create_app()
