"""Challenge detection must catch real block pages/overlays without misfiring on normal pages."""
from __future__ import annotations

from stockwatch.adapters.base import looks_like_challenge

FILLER = "<div>" + ("product details " * 6000) + "</div>"  # ~100KB, like a real product page
SCRIPTS = ('<script>window.__CONFIG__={"GLOBAL_CAPTCHA_ENABLED":true}</script>'
           "<style>#px-captcha-modal { background-color: white !important; }</style>")


def page(extra: str = "") -> str:
    return f"<html><head>{SCRIPTS}</head><body><h1>Nintendo Switch 2</h1>{FILLER}{extra}</body></html>"


def test_normal_large_page_mentioning_captcha_in_scripts_is_not_a_challenge() -> None:
    assert not looks_like_challenge(200, page())


def test_perimeterx_modal_overlay_on_real_page_is_a_challenge() -> None:
    iframe = ('<iframe id="px-captcha-modal" style="display: block; position: fixed; top: 0; '
              'left: 0; width: 100%; height: 100%; border: none; z-index: 2147483647;">')
    assert looks_like_challenge(200, page(iframe))


def test_hidden_px_modal_is_not_a_challenge() -> None:
    assert not looks_like_challenge(200, page('<iframe id="px-captcha-modal" style="display: none;">'))


def test_visible_press_and_hold_text_on_large_page() -> None:
    assert looks_like_challenge(200, page("<p>Press &amp; Hold to confirm you are a human</p>"))


def test_challenge_words_only_inside_scripts_of_large_page_are_ignored() -> None:
    assert not looks_like_challenge(200, page('<script>var t="Press & Hold";</script>'))


def test_small_interstitials_and_status_codes() -> None:
    assert looks_like_challenge(200, "<html><div id='px-captcha'></div></html>")
    assert looks_like_challenge(200, "<html><title>Access Denied</title></html>")
    assert looks_like_challenge(403, None) and looks_like_challenge(429, "") and looks_like_challenge(435, "{}")
    assert not looks_like_challenge(200, None)


def test_cloudflare_background_script_on_normal_page_is_not_a_challenge() -> None:
    # GameStop product pages load this on every normal visit.
    script = '<script src="https://www.gamestop.com/cdn-cgi/challenge-platform/scripts/precursor/main.js?ray=x"></script>'
    assert not looks_like_challenge(200, page(script))


def test_cloudflare_interstitial_is_a_challenge() -> None:
    assert looks_like_challenge(200, "<html><head><title>Just a moment...</title></head>"
                                     "<body><div id='cf-chl-widget'></div></body></html>")
    assert looks_like_challenge(503, "<html><head><title>Just a moment...</title></head>" + FILLER + "</html>")
