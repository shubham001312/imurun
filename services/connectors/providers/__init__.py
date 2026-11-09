"""Provider adapters. Add one file per service; the gate stays the same."""
from .canva import CanvaConnector
from .cloudflare import CloudflareConnector
from .github import GitHubConnector
from .gmail import GmailConnector
from .render import RenderConnector
from .slack import SlackConnector
from .supabase import SupabaseConnector
from .vercel import VercelConnector

__all__ = [
    "CanvaConnector",
    "CloudflareConnector",
    "GitHubConnector",
    "GmailConnector",
    "RenderConnector",
    "SlackConnector",
    "SupabaseConnector",
    "VercelConnector",
]
