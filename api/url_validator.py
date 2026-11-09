# START OF FILE api/url_validator.py

"""
URL Validation Module for SSRF Protection.

Provides validation functions to prevent Server-Side Request Forgery attacks
by validating URLs against a whitelist of allowed domains and blocking
private/internal IP addresses.
"""

import ipaddress
import logging
from typing import Tuple
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

# Whitelist of allowed forum domains for parsing
ALLOWED_DOMAINS = [
    'forum.gta5rp.com',
    'forum.majestic-rp.ru',
    'gta5rp.com',
    'majestic-rp.ru',
    'forum.rmrp.ru',
    'rmrp.ru',
    'forum.russia.online',
    'russia.online',
]

# Patterns for blocked private/reserved IP ranges
BLOCKED_IP_PATTERNS = [
    # IPv4 private/reserved
    '127.0.0.0/8',      # Loopback
    '10.0.0.0/8',       # Private Class A
    '172.16.0.0/12',    # Private Class B
    '192.168.0.0/16',   # Private Class C
    '169.254.0.0/16',   # Link-local
    '0.0.0.0/8',        # Reserved
    '224.0.0.0/4',      # Multicast
    '240.0.0.0/4',      # Reserved for future use
    # IPv6 private/reserved
    '::1/128',          # Loopback
    'fc00::/7',         # Unique local
    'fe80::/10',        # Link-local
]

# Precompile IP networks for faster checking
BLOCKED_NETWORKS = [ipaddress.ip_network(net) for net in BLOCKED_IP_PATTERNS]


def is_private_ip(ip_str: str) -> bool:
    """
    Check if an IP address is private/reserved.

    Args:
        ip_str: IP address string

    Returns:
        True if IP is private/reserved, False otherwise
    """
    try:
        ip = ipaddress.ip_address(ip_str)

        # Check against blocked networks
        for network in BLOCKED_NETWORKS:
            if ip in network:
                return True

        # Also use Python's built-in checks
        if ip.is_private or ip.is_loopback or ip.is_reserved or ip.is_multicast:
            return True

        return False
    except ValueError:
        # Not a valid IP address (probably a hostname)
        return False


def validate_forum_url(url: str, allow_any_https: bool = False) -> Tuple[bool, str]:
    """
    Validate a URL for forum parsing.

    Checks:
    1. Valid URL format
    2. HTTP/HTTPS protocol only
    3. Domain is in whitelist
    4. Not pointing to private/internal IP

    Args:
        url: URL to validate
        allow_any_https: If True, allow any HTTPS domain (not recommended)

    Returns:
        Tuple of (is_valid, error_message)
    """
    if not url:
        return False, "URL is required"

    try:
        parsed = urlparse(url)

        # Check protocol
        if parsed.scheme not in ('http', 'https'):
            return False, f"Only HTTP/HTTPS protocols are allowed, got: {parsed.scheme}"

        # Get hostname
        hostname = parsed.hostname
        if not hostname:
            return False, "URL must contain a hostname"

        hostname = hostname.lower()

        # Check for IP address
        if is_private_ip(hostname):
            logger.warning(f"[SSRF] Blocked attempt to access private IP: {hostname}")
            return False, "Private/internal IP addresses are not allowed"

        # Try to detect if hostname is an IP
        try:
            ip = ipaddress.ip_address(hostname)
            if ip.is_private or ip.is_loopback or ip.is_reserved:
                logger.warning(f"[SSRF] Blocked attempt to access reserved IP: {hostname}")
                return False, "Reserved IP addresses are not allowed"
        except ValueError:
            pass  # Not an IP address, continue with domain check

        # Check domain whitelist
        if not allow_any_https:
            is_allowed = any(
                hostname == domain or hostname.endswith('.' + domain)
                for domain in ALLOWED_DOMAINS
            )

            if not is_allowed:
                logger.warning(f"[SSRF] Blocked request to non-whitelisted domain: {hostname}")
                return False, f"Domain '{hostname}' is not in the allowed list"

        return True, ""

    except Exception as e:
        logger.error(f"[SSRF] URL validation error: {e}")
        return False, f"Invalid URL format: {str(e)}"


def sanitize_url(url: str) -> str:
    """
    Sanitize a URL by removing potentially dangerous components.

    Args:
        url: URL to sanitize

    Returns:
        Sanitized URL string
    """
    if not url:
        return ""

    try:
        parsed = urlparse(url)

        # Reconstruct URL without fragment and with cleaned components
        clean_url = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"

        if parsed.query:
            clean_url += f"?{parsed.query}"

        return clean_url
    except Exception:
        return ""


# Convenience function for quick validation
def is_safe_url(url: str) -> bool:
    """Quick check if URL is safe to fetch."""
    is_valid, _ = validate_forum_url(url)
    return is_valid
