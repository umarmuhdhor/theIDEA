from .appstore import AppStoreScraper
from .base import SocialPost, SocialScraper
from .discourse import DiscourseScraper
from .hn import HNScraper
from .lemmy import LemmyScraper
from .reddit import RedditScraper
from .signals import to_painpoint
from .stackexchange import StackExchangeScraper

SOCIAL_SCRAPERS = {
    "hn": HNScraper,
    "reddit": RedditScraper,
    "stackexchange": StackExchangeScraper,
    "appstore": AppStoreScraper,
    "discourse": DiscourseScraper,
    "lemmy": LemmyScraper,
}

__all__ = [
    "SocialPost", "SocialScraper", "SOCIAL_SCRAPERS", "to_painpoint",
    "HNScraper", "RedditScraper", "StackExchangeScraper", "AppStoreScraper",
    "DiscourseScraper", "LemmyScraper",
]
