from .base import BaseScraper, HttpClient
from .devfolio import DevfolioScraper
from .devpost import DevpostScraper
from .ethglobal import EthGlobalScraper
from .unstop import UnstopScraper
from .models import Prize, Project

SCRAPERS = {
    "devfolio": DevfolioScraper,
    "devpost": DevpostScraper,
    "ethglobal": EthGlobalScraper,
    "unstop": UnstopScraper,
}

__all__ = [
    "BaseScraper", "HttpClient", "Project", "Prize", "SCRAPERS",
    "DevfolioScraper", "DevpostScraper", "EthGlobalScraper", "UnstopScraper",
]
