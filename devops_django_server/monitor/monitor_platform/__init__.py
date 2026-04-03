import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from monitor.models import MonitorPlatform


class UnsupportedPlatformError(RuntimeError):
    pass


logger = logging.getLogger("monitor")


def _is_proxy_related_error(e: Exception) -> bool:
    s = str(e).lower()
    if "err_tunnel_connection_failed" in s:
        return True
    if "tunnel connection failed" in s:
        return True
    if "proxy setup failed" in s:
        return True
    if "proxy connect failed" in s:
        return True
    if "err_http2_protocol_error" in s or "http2_protocol_error" in s:
        return True
    if "proxy" in s and ("failed" in s or "error" in s):
        return True
    if "connection refused" in s or "timed out" in s or "timeout" in s:
        return True
    return False


def run_platform(
    platform: "MonitorPlatform",
    domain: str,
    *,
    proxy: str,
    headless: bool,
    screenshot_enabled: bool,
    screenshot_dir: str,
    nav_timeout_ms: int,
    action_timeout_ms: int,
):
    key = (platform.platform or "").strip().lower()
    url = (platform.website_url or "").strip().lower()

    if key in {"chinaz"} or "tool.chinaz.com/speedtest" in url:
        from .chinaz_cs import run as run_chinaz

        return run_chinaz(
            domain,
            base_url=platform.website_url,
            proxy=(proxy or "").strip(),
            headless=headless,
            screenshot_enabled=screenshot_enabled,
            screenshot_dir=screenshot_dir,
            nav_timeout_ms=nav_timeout_ms,
            action_timeout_ms=action_timeout_ms,
        )

    if key in {"17ce", "seventeen_ce"} or "17ce.com/get" in url:
        from .seventeen import run as run_17ce

        proxy_s = (proxy or "").strip()
        try:
            return run_17ce(
                domain,
                base_url=platform.website_url,
                proxy=proxy_s,
                headless=headless,
                screenshot_enabled=screenshot_enabled,
                screenshot_dir=screenshot_dir,
                nav_timeout_ms=nav_timeout_ms,
                action_timeout_ms=action_timeout_ms,
            )
        except Exception as e:
            if proxy_s and _is_proxy_related_error(e):
                logger.warning(
                    f"17ce failed with proxy, retry without proxy: domain={domain} error={type(e).__name__}: {e}"
                )
                return run_17ce(
                    domain,
                    base_url=platform.website_url,
                    proxy="",
                    headless=headless,
                    screenshot_enabled=screenshot_enabled,
                    screenshot_dir=screenshot_dir,
                    nav_timeout_ms=nav_timeout_ms,
                    action_timeout_ms=action_timeout_ms,
                )
            raise

    if key in {"itdog"} or "itdog.cn/http" in url:
        from .itdog import run as run_itdog

        proxy_s = (proxy or "").strip()
        try:
            return run_itdog(
                domain,
                base_url=platform.website_url,
                proxy=proxy_s,
                headless=headless,
                screenshot_enabled=screenshot_enabled,
                screenshot_dir=screenshot_dir,
                nav_timeout_ms=nav_timeout_ms,
                action_timeout_ms=action_timeout_ms,
            )
        except Exception as e:
            if proxy_s and _is_proxy_related_error(e):
                logger.warning(f"itdog failed with proxy, retry without proxy: domain={domain} error={type(e).__name__}: {e}")
                return run_itdog(
                    domain,
                    base_url=platform.website_url,
                    proxy="",
                    headless=headless,
                    screenshot_enabled=screenshot_enabled,
                    screenshot_dir=screenshot_dir,
                    nav_timeout_ms=nav_timeout_ms,
                    action_timeout_ms=action_timeout_ms,
                )
            raise

    raise UnsupportedPlatformError(f"unsupported platform: {platform.platform} ({platform.website_url})")
