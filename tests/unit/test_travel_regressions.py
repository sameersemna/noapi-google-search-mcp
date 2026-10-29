"""Regression tests for Google travel result parsing."""

from google_search_mcp.tools import travel


def test_hotel_result_filter_rejects_google_navigation_cards():
    assert not travel._is_hotel_listing_name("Sign in")
    assert not travel._is_hotel_listing_name("AI Mode")
    assert not travel._is_hotel_listing_name("View all hotels")
    assert travel._is_hotel_listing_name("Lord Elgin Hotel")