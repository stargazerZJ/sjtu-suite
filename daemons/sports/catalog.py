"""Sports venue catalog operations for the dashboard API."""

from __future__ import annotations

import logging
from dataclasses import asdict
from typing import Any


class SportsCatalogMixin:
    def list_venues(self, search: str = "") -> list[dict[str, Any]]:
        self._log(logging.DEBUG, "Listing venues for dashboard search.", search=search)
        client = self.create_client()
        venues, _ = client.list_all_venues(venue_name=search)
        self._log(
            logging.DEBUG,
            "Venue search completed.",
            search=search,
            venue_count=len(venues),
        )
        return [venue.raw for venue in venues]

    def get_venue_detail(self, venue_id: str) -> dict[str, Any]:
        self._log(logging.DEBUG, "Fetching venue detail.", venue_id=venue_id)
        client = self.create_client()
        detail = client.get_venue_detail(venue_id)
        self._log(
            logging.DEBUG,
            "Venue detail fetched.",
            venue_id=venue_id,
            motion_type_count=len(detail.motion_types),
        )
        return {
            "venue_id": detail.venue_id,
            "venue_name": detail.venue_name,
            "campus_name": detail.campus_name,
            "open_time": detail.open_time,
            "venue_mobile": detail.venue_mobile,
            "motion_types": [asdict(item) for item in detail.motion_types],
        }

    def get_availability(self, venue_id: str, motion: str) -> list[dict[str, Any]]:
        self._log(
            logging.DEBUG,
            "Fetching venue availability.",
            venue_id=venue_id,
            motion=motion,
        )
        client = self.create_client()
        results = client.list_availability(venue_id, motion)
        self._log(
            logging.DEBUG,
            "Venue availability fetched.",
            venue_id=venue_id,
            motion=motion,
            visible_dates=len(results),
        )
        return results
