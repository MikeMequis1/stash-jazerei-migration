"""Camada de automacao de navegador (Playwright), complementar ao HTTP."""

from src.browser.session import BrowserSession, BrowserStatus, BrowserUnavailable, profile_status

__all__ = ["BrowserSession", "BrowserStatus", "BrowserUnavailable", "profile_status"]
